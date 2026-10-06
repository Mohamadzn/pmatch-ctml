"""Eligibility criteria as numbered items, found from the protocol's own structure.

Handles the layouts seen across sponsors: flat numbered lists, lists nested under short
unnumbered subheadings, numbering that restarts at 1 or continues, margin numbers on
their own line, a first item printed without a number, and lettered alternatives that
each carry their own population ("a. Part 1: ...", "b. Part 2: ...").

A group title must stand on its own. A line inside a paragraph, or a line that finishes an
unfinished sentence ("... contraception (see" followed by "Section 6.3.1)"), stays in its
criterion, even when it is short, capitalised, or marked as a heading by Document
Intelligence.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from app.services.ctml.document.model import Document, Line
from app.services.ctml.document.sections import EligibilityRegion, Routing

_NUMBERED = re.compile(r"^(\d{1,2})[.)](?:\s+(.*))?$")
_GLUED = re.compile(r"^(\d{1,2})\s+([A-Z(\[].*)$")
_BARE = re.compile(r"^(\d{1,2})$")
_KIND_SWITCH = re.compile(
    r"^(?:key\s+|subject\s+|participant\s+|patient\s+)?(inclusion|exclusion)\s+criteria\b[\s:.]*$", re.I
)
_SUB_ITEM = re.compile(r"^(?:[•·▪◦○\-–*]|o(?=\s)|\.(?=\s)|[a-z][.)]|\(?[ivx]{1,4}[.)])\s*")
_LETTERED = re.compile(r"^[a-z][.)]\s")
_SCOPE = re.compile(
    r"^(?:[•·▪\-*]|[a-z][.)])?\s*(?:for\s+|in\s+)?(?:subjects\s+|patients\s+|participants\s+)?(?:in\s+)?"
    r"(part|cohort|arm|group|stage|module)\s+([A-Za-z0-9][\w\-]*)(?:\s+(?:only|optional))?\s*:",
    re.I,
)
_SCOPE_IN_HEADING = re.compile(r"\b(part|cohort|arm|group|stage|module)\s+([A-Za-z0-9][\w\-]*)", re.I)
_TABLE_REFERENCE = re.compile(r"\btable\s+(\d{1,3}(?:\.\d{1,2})?)\b", re.I)
# A list marker at the start of a line ("a)", "(1)", "iv.") is not a bracket to balance.
_LEADING_MARKER = re.compile(r"^\(?[A-Za-z0-9]{1,4}[.)]\s+")
# Text that cannot end a sentence: a hyphen, comma, slash or open bracket, or an English
# function word ("see", "and", "of" ...). Language, not medical terms.
_OPEN_END = re.compile(
    r"(?:[-\u2010,/(\[]|\b(?:a|an|and|as|at|by|for|from|in|of|on|or|per|see|than|the|to|with))$", re.I
)


@dataclass(frozen=True)
class Criterion:
    criterion_id: str
    kind: str  # "inclusion" or "exclusion"
    number: str
    scope_label: str | None
    text: str
    line_ids: list[str]
    item_line_ids: list[str]
    table_ids: list[str]
    pages: list[int]
    section_title: str
    stem: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Inventory:
    criteria: list[Criterion]
    issues: list[str] = field(default_factory=list)
    # Lines read as group titles or headings inside the criteria: they belong to no criterion.
    group_titles: list[dict] = field(default_factory=list)

    def by_id(self, criterion_id: str) -> Criterion:
        return next(c for c in self.criteria if c.criterion_id == criterion_id)

    def scope_labels(self) -> list[str]:
        return sorted({c.scope_label for c in self.criteria if c.scope_label})


@dataclass
class _Item:
    kind: str
    number: str
    implicit: bool
    lines: list[Line] = field(default_factory=list)
    first_text: str = ""
    tables: list[str] = field(default_factory=list)
    heading_scope: str | None = None
    bullet: bool = False  # numbered by position in a list printed with bullets


def normalize_scope(label: str) -> str:
    """ "PART 2" and "part 2" read as "Part 2"."""
    word, _, rest = label.strip().partition(" ")
    return f"{word.capitalize()} {rest.strip()}".strip()


def scope_from_text(text: str) -> str | None:
    match = _SCOPE.match(text.strip())
    return normalize_scope(f"{match.group(1)} {match.group(2)}") if match else None


def _scope_from_heading(text: str) -> str | None:
    matches = _SCOPE_IN_HEADING.findall(text)
    labels = {normalize_scope(f"{word} {token}") for word, token in matches}
    return labels.pop() if len(labels) == 1 else None


def _heading_kind(text: str) -> str | None:
    """ "inclusion" or "exclusion" for a heading that names only one of them."""
    inclusion = bool(re.search(r"\binclusion\b", text, re.I))
    exclusion = bool(re.search(r"\bexclusion\b", text, re.I))
    if inclusion != exclusion:
        return "inclusion" if inclusion else "exclusion"
    return None


def _bracket_balance(text: str) -> int:
    """Opening minus closing brackets, without a leading list marker such as "a)" or "(1)"."""
    body = _LEADING_MARKER.sub("", text.strip(), count=1)
    return body.count("(") + body.count("[") - body.count(")") - body.count("]")


def _ends_open(texts: list[str]) -> bool:
    """True when text stops in the middle of a sentence: a bracket is left open, or the last
    line ends with a hyphen, comma, slash or a word such as "see", "and" or "of"."""
    texts = [t for t in texts if t.strip()]
    if not texts:
        return False
    return sum(_bracket_balance(t) for t in texts) > 0 or bool(_OPEN_END.search(texts[-1].rstrip()))


def _item_texts(item: _Item) -> list[str]:
    """The item's text lines, with the first line's list marker removed."""
    return [item.first_text] + [line.text for line in item.lines[1:]]


def _continues(item: _Item | None, text: str) -> bool:
    """True when a line finishes the open item's sentence instead of starting a new group:
    the item stops mid-sentence, or the line closes a bracket it never opened
    ("... contraception (see" followed by "Section 6.3.1)")."""
    if item is None or not item.lines:
        return False
    return _ends_open(_item_texts(item)) or _bracket_balance(text) < 0


def _is_subheading(text: str, next_marker: str | None, next_is_sub_item: bool, inside_list: bool) -> bool:
    """A short unnumbered title line ("Medical Conditions") that starts a group of criteria.

    It must be followed by a numbered item, or by a bullet when no list is open. Inside an
    open list ("any of the following:") a title line labels a group of that list's bullets
    and stays in the criterion.
    """
    words = text.split()
    if not 0 < len(words) <= 4 or text.rstrip().endswith((".", ":", ";", ",", "?", "!")):
        return False
    if re.match(r"[\d•·▪\-*(]", text) or not text[0].isupper() or (text.isupper() and len(words) == 1):
        return False
    if next_marker is not None:
        return True
    return next_is_sub_item and not inside_list


def _inside_list(item: _Item | None) -> bool:
    """True when the item opened a list: a line ending in ":" or a bullet line."""
    if item is None:
        return False
    texts = [item.first_text] + [line.text for line in item.lines]
    return any(t.rstrip().endswith(":") for t in texts) or any(_SUB_ITEM.match(t) for t in texts[1:])


def _top_bullet(lines: list[Line]) -> str | None:
    """The bullet glyph of a criteria list printed without numbers, or None."""
    texts = [line.text.strip() for line in lines if line.kind != "table"]
    if any(_NUMBERED.match(t) or _GLUED.match(t) for t in texts):
        return None
    for text in texts:
        match = re.match(r"^([•·▪◦○\-–*])\s+\S", text)
        if match:
            return match.group(1)
    return None


def _looks_like_title(text: str) -> bool:
    """A short line of capitalised words with no end punctuation ("Prior Therapy")."""
    words = text.split()
    return (
        0 < len(words) <= 4
        and not text.rstrip().endswith((".", ":", ";", ","))
        and all(word[:1].isupper() or len(word) <= 3 for word in words)
    )


def _marker(text: str, expected: int | None, bullet: str | None = None) -> tuple[str, str, str] | None:
    """(number, rest, form) for a list marker line, or None. In a list printed with bullets
    instead of numbers, the top-level bullet is the marker and items are numbered in order."""
    if bullet is not None:
        if text.startswith(bullet) and text[len(bullet) : len(bullet) + 1].isspace():
            return str(expected or 1), text[len(bullet) :].strip(), "bullet"
        return None
    numbered = _NUMBERED.match(text)
    if numbered:
        return numbered.group(1), numbered.group(2) or "", "numbered"
    for pattern, form in ((_GLUED, "glued"), (_BARE, "bare")):
        match = pattern.match(text)
        if match:
            value = int(match.group(1))
            if value == 1 or (expected is not None and value == expected):
                rest = match.group(2) if form == "glued" else ""
                return match.group(1), rest, form
            return None
    return None


def _table_ids_for(document: Document, text: str) -> list[str]:
    wanted = {number for number in _TABLE_REFERENCE.findall(text)}
    found = []
    for table in document.tables.values():
        caption = re.match(r"\s*table\s+(\d{1,3}(?:\.\d{1,2})?)\b", table.caption, re.I)
        if caption and caption.group(1) in wanted:
            found.append(table.table_id)
    return found


def _scan_region(document: Document, region: EligibilityRegion) -> tuple[list[_Item], list[str], list[Line]]:
    lines = document.lines[region.section.start + 1 : region.section.end]
    kind = None if region.kind == "mixed" else region.kind
    items: list[_Item] = []
    stem: list[str] = []
    titles: list[Line] = []
    current: _Item | None = None
    expected: int | None = None
    pending_first = True
    heading_scope: str | None = None

    def finish() -> None:
        nonlocal current
        if current is not None and (current.first_text or current.lines):
            items.append(current)
        current = None

    lines = [line for line in lines if line.kind != "running"]
    bullet = _top_bullet(lines)
    scope_level = 0  # heading level that set heading_scope; plain subheadings count as 99

    def set_scope(text: str, level: int) -> None:
        """A heading naming a population sets the scope; a heading at the same or a higher
        level that names none ends it; a deeper heading keeps it."""
        nonlocal heading_scope, scope_level
        found = _scope_from_heading(text)
        if found:
            heading_scope, scope_level = found, level
        elif heading_scope is not None and level <= scope_level:
            heading_scope, scope_level = None, 0

    for position, line in enumerate(lines):
        text = line.text.strip()
        next_text = lines[position + 1].text.strip() if position + 1 < len(lines) else ""
        switch = _KIND_SWITCH.match(text)
        if switch:
            finish()
            kind = switch.group(1).lower()
            pending_first, expected, heading_scope, scope_level = True, None, None, 0
            continue
        if kind is None:
            continue
        marker = _marker(text, expected, bullet)
        if line.kind == "heading" and not marker:
            if _continues(current, text):
                current.lines.append(line)  # a line marked as a heading that ends the item's sentence
                continue
            titles.append(line)
            finish()
            heading_kind = _heading_kind(text)
            if heading_kind and heading_kind != kind:
                kind, expected, heading_scope, scope_level = heading_kind, None, None, 0
            set_scope(text, line.heading_level or 1)
            pending_first = True
            continue
        if line.kind == "table":
            if current is not None:
                current.tables.append(line.table_id or "")
            continue
        next_marker = _marker(next_text, expected, bullet)
        if (
            current is None
            and next_marker is not None
            and next_marker[0] != "1"
            and next_marker[2] != "bullet"
        ):
            next_marker = None  # an unnumbered line before item N (N > 1) is item N - 1, not a title
        next_is_sub_item = bool(_SUB_ITEM.match(next_text)) and next_marker is None
        if (
            not marker
            and line.starts_paragraph
            and not _continues(current, text)
            and _is_subheading(text, next_marker and next_marker[0], next_is_sub_item, _inside_list(current))
        ):
            titles.append(line)
            finish()
            set_scope(text, 99)
            pending_first = True
            continue
        if marker:
            number, rest, form = marker
            pulled: Line | None = None
            if (
                form == "bare"
                and current is not None
                and current.lines
                and next_text[:1].islower()
                and not _looks_like_title(current.lines[-1].text)
            ):
                # A margin number printed between the two halves of its own first sentence.
                pulled = current.lines.pop()
            if current is not None and current.implicit and number == "1":
                current = None  # the unnumbered text was a preamble, not item 1
            finish()
            current = _Item(
                kind, number, implicit=False, heading_scope=heading_scope, bullet=form == "bullet"
            )
            if pulled is not None:
                current.lines.append(pulled)
                current.first_text = pulled.text
            elif rest:
                current.lines.append(line)
                current.first_text = rest
            expected = int(number) + 1
            pending_first = False
            continue
        if _BARE.match(text):
            continue  # stray digits (page numbers, footnote marks)
        if current is None:
            if pending_first and not text.endswith(":"):
                number = str(expected) if expected else str(sum(1 for i in items if i.kind == kind) + 1)
                current = _Item(kind, number, implicit=True, heading_scope=heading_scope, bullet=bool(bullet))
                current.lines.append(line)
                current.first_text = text
                expected = int(number) + 1
                pending_first = False
            else:
                stem.append(text)
            continue
        if not current.lines:
            current.first_text = text
        current.lines.append(line)
    finish()
    return items, stem, titles


def _segments(item: _Item) -> list[tuple[str | None, list[Line]]]:
    """Split lettered alternatives that each start with their own population label."""
    positions = [i for i, line in enumerate(item.lines) if i > 0 and _LETTERED.match(line.text)]
    if len(positions) < 2:
        return []
    labels = [scope_from_text(item.lines[i].text) for i in positions]
    if any(label is None for label in labels):
        return []
    stem = item.lines[: positions[0]]
    groups = []
    for index, start in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(item.lines)
        groups.append((labels[index], stem + item.lines[start:end]))
    return groups


def _criterion_text(first_text: str, lines: list[Line], first_line: Line | None) -> str:
    texts = []
    for line in lines:
        if first_line is not None and line is first_line:
            texts.append(first_text)
        else:
            texts.append(line.text)
    return "\n".join(t for t in texts if t)


def _make_criteria(
    document: Document, items: list[_Item], region: EligibilityRegion, stem: str
) -> list[Criterion]:
    result = []
    for item in items:
        prefix = "INC" if item.kind == "inclusion" else "EXC"
        first_line = item.lines[0] if item.lines else None
        segments = _segments(item) or [(None, item.lines)]
        for index, (segment_scope, segment_lines) in enumerate(segments):
            text = _criterion_text(item.first_text, segment_lines, first_line)
            scope = segment_scope or scope_from_text(item.first_text) or item.heading_scope
            item_lines = [
                line.line_id
                for line in segment_lines
                if line is not first_line
                and _SUB_ITEM.match(line.text)
                and not line.text.lower().startswith("note")
            ]
            tables = list(dict.fromkeys([t for t in item.tables if t] + _table_ids_for(document, text)))
            suffix = f".{index + 1}" if len(segments) > 1 else ""
            result.append(
                Criterion(
                    criterion_id=f"{prefix}-{item.number}{suffix}",
                    kind=item.kind,
                    number=item.number,
                    scope_label=scope,
                    text=text,
                    line_ids=[line.line_id for line in segment_lines],
                    item_line_ids=item_lines,
                    table_ids=tables,
                    pages=sorted({line.page for line in segment_lines}),
                    section_title=region.section.label(),
                    stem=stem,
                )
            )
    return result


def _numbering_issues(criteria: list[Criterion]) -> list[str]:
    issues = []
    for kind in ("inclusion", "exclusion"):
        numbers = sorted({int(c.number) for c in criteria if c.kind == kind and c.number.isdigit()})
        if numbers:
            missing = [n for n in range(1, numbers[-1] + 1) if n not in set(numbers)]
            if missing:
                issues.append(f"criterion_numbering_gap:{kind}:" + ",".join(map(str, missing)))
    return issues


def build_inventory(document: Document, routing: Routing) -> Inventory:
    criteria: list[Criterion] = []
    issues: list[str] = []
    titles: list[dict] = []
    seen_ids: dict[str, int] = {}
    seen_texts: dict[tuple[str, str], str] = {}
    for region in routing.eligibility:
        items, stem_lines, region_titles = _scan_region(document, region)
        titles += [
            {"line_id": line.line_id, "page": line.page, "text": line.text, "section": region.section.label()}
            for line in region_titles
        ]
        if not items:
            issues.append(f"eligibility_section_without_items:{region.section.label()}")
            continue
        if any(item.bullet for item in items):
            issues.append(f"bullet_list:{region.section.label()}")
        for item in items:
            if item.implicit:
                prefix = "INC" if item.kind == "inclusion" else "EXC"
                issues.append(f"unnumbered_item:{prefix}-{item.number}:{region.section.label()}")
        for criterion in _make_criteria(document, items, region, " ".join(stem_lines)):
            key = (criterion.kind, " ".join(criterion.text.split()).casefold())
            if key in seen_texts:
                issues.append(f"duplicate_criterion:{criterion.criterion_id}:same_text_as:{seen_texts[key]}")
                continue
            count = seen_ids.get(criterion.criterion_id, 0)
            seen_ids[criterion.criterion_id] = count + 1
            if count:
                criterion = Criterion(
                    **{**criterion.as_dict(), "criterion_id": f"{criterion.criterion_id}@{count + 1}"}
                )
                issues.append(f"repeated_number:{criterion.criterion_id}")
            seen_texts[key] = criterion.criterion_id
            criteria.append(criterion)
            if _ends_open(criterion.text.split("\n")):
                issues.append(f"criterion_text_unfinished:{criterion.criterion_id}")
    issues.extend(_numbering_issues(criteria))
    for kind in ("inclusion", "exclusion"):
        if not any(c.kind == kind for c in criteria):
            issues.append(f"no_{kind}_criteria_found")
    return Inventory(criteria, issues, titles)
