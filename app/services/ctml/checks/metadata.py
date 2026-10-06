"""Checks run by submit_metadata, and the value conversions they share with the compiler."""

from __future__ import annotations

import re
from datetime import date

from app.services.ctml.checks.common import closest_span, line_texts, reject, unknown_lines
from app.services.ctml.contracts import MetadataSubmission, SourcedText
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.text import contains_phrase

_ROMAN = {"1": "I", "2": "II", "3": "III", "4": "IV", "i": "I", "ii": "II", "iii": "III", "iv": "IV"}
_ROMAN_ORDER = ["I", "II", "III", "IV"]
_PHASE = re.compile(
    r"\bphase\s*[:\-]?\s*((?:[1-4]|iv|iii|ii|i)[a-c]?(?:\s*(?:/|-|and|to)\s*(?:[1-4]|iv|iii|ii|i)[a-c]?)*)\b",
    re.I,
)
_MONTHS = {
    name: index
    for index, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}
_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z .()/&'-]{0,40}?)\s*:\s*\S")
# "Amendment 07", "Version 3.0", "v2" or "v2.1" alone; not "V940-001".
_VERSION_WORDS = re.compile(r"^\s*(?:(?:amendment|version|revision)\b|rev\.?\s|v\d+(?:\.\d+)*\s*$)", re.I)
# The nouns a field label contains ("Protocol No.:", "Official Title:"). A leading word
# before ":" without one of them is part of the value ("ASCENT: A Randomized Phase 3 ...").
LABEL_NOUNS = {
    "long_title": {"title"},
    "short_title": {"title"},
    "protocol_no": {"protocol", "number", "no", "id", "identifier", "code", "study"},
    "protocol_version_no": {"version", "amendment", "number", "no", "revision"},
    "protocol_version_date": {"date", "dated", "effective", "version", "amendment"},
    "phase": {"phase"},
    "sponsor_name": {"sponsor", "name", "company"},
    "nct_purpose": {"purpose", "objective", "objectives", "rationale", "aim"},
    "nct_id": {"nct", "number", "no", "id", "identifier", "clinicaltrials", "registry", "gov"},
}
_NCT = re.compile(r"^NCT\d{8}$")


def phase_roman(text: str) -> str | None:
    """The lower phase in Roman numerals ("Phase 1b/2" -> "I"), or None."""
    match = _PHASE.search(text)
    if not match:
        return None
    numerals = []
    for token in re.findall(r"iv|iii|ii|i|[1-4]", match.group(1).casefold()):
        numerals.append(_ROMAN[token])
    return min(numerals, key=_ROMAN_ORDER.index) if numerals else None


def parse_date(text: str) -> date | None:
    """A printed date: "28 June 2021", "June 28, 2021", "28-Jun-2021", "2021-06-28" or "28JUN2021".
    All-number dates are read only when unambiguous (day above 12, or year first)."""
    value = " ".join(text.replace(",", " ").split()).strip()
    iso = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", value)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    day_month = re.search(r"\b(\d{1,2})[\s\-/.]*([A-Za-z]{3,9})\.?[\s\-/.]*(\d{4})\b", value)
    if day_month and day_month.group(2).casefold() in _MONTHS:
        return _safe_date(
            int(day_month.group(3)), _MONTHS[day_month.group(2).casefold()], int(day_month.group(1))
        )
    month_day = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})\s+(\d{4})\b", value)
    if month_day and month_day.group(1).casefold() in _MONTHS:
        return _safe_date(
            int(month_day.group(3)), _MONTHS[month_day.group(1).casefold()], int(month_day.group(2))
        )
    numeric = re.search(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b", value)
    if numeric:
        first, second, year = int(numeric.group(1)), int(numeric.group(2)), int(numeric.group(3))
        if first > 12 >= second:
            return _safe_date(year, second, first)
        if second > 12 >= first:
            return _safe_date(year, first, second)
    return None


MAX_PURPOSE_SENTENCES = 2
# A sentence ends with . ! or ? followed by a capital letter; "e.g." and "(ie," do not end one.
_SENTENCE_END = re.compile(r"(?<!\be\.g)(?<!\bi\.e)[.!?]\s+(?=[A-Z])")


def sentence_count(text: str) -> int:
    return len(_SENTENCE_END.findall(text.strip())) + 1 if text.strip() else 0


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def starts_with_label(value: str, field: str | None = None) -> bool:
    """True for "Protocol Number: 123"; False for "STUDY-123: A Phase 3 Trial" (digits before the
    colon) and for "ASCENT: A Randomized Trial" (no label noun of the field)."""
    match = _LABEL.match(value)
    if not match:
        return False
    label = match.group(1)
    if len(label.split()) > 4 or any(ch.isdigit() for ch in label):
        return False
    if field is None:
        return True
    words = set(re.findall(r"[a-z]+", label.casefold()))
    return bool(words & LABEL_NOUNS.get(field, set()))


def _field_problems(name: str, field: SourcedText, context: RunContext) -> list[dict]:
    path = f"/{name}"
    missing = unknown_lines(context.document, field.line_ids)
    if missing:
        return [reject("unknown_line_id", f"{path}/line_ids", "Unknown line IDs: " + ", ".join(missing))]
    cited = line_texts(context.document, field.line_ids)
    joined = " ".join(cited.split())
    problems = []
    if not field.value.strip() or not contains_phrase(joined, field.value):
        problems.append(
            reject(
                "value_not_in_cited_lines",
                f"{path}/value",
                "Copy exact words from the cited lines. Closest: " + closest_span(cited, field.value),
            )
        )
    if starts_with_label(field.value, name):
        problems.append(
            reject("value_starts_with_label", f"{path}/value", "Remove the field label before ':'.")
        )
    return problems


def check_metadata(
    submission: MetadataSubmission,
    confirmations: list,
    context: RunContext,
    state: AgentState,
) -> tuple[list[dict], list[dict]]:
    problems: list[dict] = []
    for name in MetadataSubmission.model_fields:
        value = getattr(submission, name)
        if isinstance(value, SourcedText):
            problems.extend(_field_problems(name, value, context))
    protocol_no = submission.protocol_no.value.strip()
    version = submission.protocol_version_no.value.strip() if submission.protocol_version_no else ""
    if version and protocol_no.casefold() == version.casefold():
        problems.append(
            reject("protocol_no_equals_version", "/protocol_no", "The protocol number is not the version.")
        )
    if _VERSION_WORDS.match(protocol_no):
        problems.append(
            reject(
                "protocol_no_is_version_label",
                "/protocol_no/value",
                "This looks like a version or amendment label.",
            )
        )
    if submission.protocol_version_date and parse_date(submission.protocol_version_date.value) is None:
        problems.append(
            reject(
                "date_unparsed",
                "/protocol_version_date/value",
                "Give the date as printed, for example '28 June 2021' or '2021-06-28'. "
                "An all-number date must be unambiguous.",
            )
        )
    if phase_roman(submission.phase.value) is None:
        problems.append(
            reject("phase_unparsed", "/phase/value", "Quote the words that state the phase, e.g. 'Phase 1b'.")
        )
    purpose = submission.nct_purpose.value if submission.nct_purpose else ""
    if sentence_count(purpose) > MAX_PURPOSE_SENTENCES:
        problems.append(
            reject(
                "nct_purpose_too_long",
                "/nct_purpose/value",
                "Use the one sentence (or two adjacent sentences) that state the drugs, the participants' "
                "cancer and the objective. Leave out eligibility details and design lists.",
            )
        )
    if submission.nct_id and not _NCT.match(submission.nct_id.value.strip().upper()):
        problems.append(
            reject("nct_id_format", "/nct_id/value", "An NCT number is NCT followed by 8 digits.")
        )
    return problems, []
