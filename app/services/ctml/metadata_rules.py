"""Metadata values for CTML: conversions and the merge with ClinicalTrials.gov.

The protocol PDF comes first. The registry fills a field only when the protocol gives none,
and every disagreement is reported. principal_investigator is never taken from the
registry (it is site-specific) and is always "".
"""

from __future__ import annotations

import difflib
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.services.ctml.checks.metadata import parse_date, phase_roman
from app.services.ctml.contracts import MetadataSubmission
from app.services.ctml.text import comparison_key

TORONTO = ZoneInfo("America/Toronto")
TITLE_AGREEMENT = 0.65
_REGISTRY_PHASE = {"EARLY_PHASE1": "I", "PHASE1": "I", "PHASE2": "II", "PHASE3": "III", "PHASE4": "IV"}
_ORDER = ["I", "II", "III", "IV"]


def ctml_date(value: date) -> str:
    """Midnight in Toronto, written in UTC as the gold files do ("2021-06-28T04:00:00.000Z")."""
    local = datetime(value.year, value.month, value.day, tzinfo=TORONTO)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def registry_phase(phases: list[str]) -> str | None:
    numerals = [_REGISTRY_PHASE[p] for p in phases if p in _REGISTRY_PHASE]
    return min(numerals, key=_ORDER.index) if numerals else None


def first_sentence(text: str | None) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    match = re.match(r"(.+?[.!?])(\s|$)", flat)
    return match.group(1) if match else flat


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, comparison_key(a), comparison_key(b)).ratio()


def resolve_metadata(
    submission: MetadataSubmission | None,
    registry: dict | None,
    nct_id: str | None,
) -> tuple[dict, list[dict], list[str]]:
    """(fields, field log, review items). Fields use CTML names; absent optional fields are left out."""
    pdf: dict[str, str | None] = {}
    if submission is not None:
        for name in MetadataSubmission.model_fields:
            value = getattr(submission, name)
            if hasattr(value, "value"):
                pdf[name] = " ".join(value.value.split())
    registry = registry or {}
    review: list[str] = []
    log: list[dict] = []
    fields: dict[str, str] = {}

    def choose(name: str, pdf_value: str | None, registry_value: str | None, required: bool = True) -> None:
        if pdf_value:
            fields[name] = pdf_value
            source = "protocol"
        elif registry_value:
            fields[name] = registry_value
            source = "clinicaltrials.gov"
            review.append(f"metadata_from_registry:{name}")
        else:
            source = "none"
            if required:
                review.append(f"metadata_missing:{name}")
        log.append({"field": name, "source": source, "protocol": pdf_value, "registry": registry_value})

    trial_id = (pdf.get("nct_id") or "").upper() or nct_id or registry.get("nct_id")
    choose("trial_id", trial_id, None)
    choose("long_title", pdf.get("long_title"), registry.get("long_title"))
    if pdf.get("short_title") or registry.get("short_title"):
        choose("short_title", pdf.get("short_title"), registry.get("short_title"))
    else:
        fields["short_title"] = fields.get("long_title", "")
        log.append({"field": "short_title", "source": "long_title", "protocol": None, "registry": None})
    choose("protocol_no", pdf.get("protocol_no"), registry.get("protocol_no"))
    choose("protocol_version_no", pdf.get("protocol_version_no"), None, required=False)
    printed_date = pdf.get("protocol_version_date")
    parsed = parse_date(printed_date) if printed_date else None
    if printed_date and parsed is None:
        review.append("metadata_date_unparsed:protocol_version_date")
    choose("protocol_version_date", ctml_date(parsed) if parsed else None, None, required=False)
    pdf_phase = phase_roman(pdf["phase"]) if pdf.get("phase") else None
    choose("phase", pdf_phase, registry_phase(registry.get("phases") or []))
    choose("nct_purpose", pdf.get("nct_purpose"), first_sentence(registry.get("brief_summary")))
    choose("sponsor_name", pdf.get("sponsor_name"), registry.get("sponsor_name"))

    review += registry_conflicts(pdf, registry, pdf_phase, trial_id)
    return fields, log, review


def registry_conflicts(pdf: dict, registry: dict, pdf_phase: str | None, trial_id: str | None) -> list[str]:
    """Fields where the protocol and ClinicalTrials.gov disagree (the protocol value is kept)."""
    if not registry:
        return []
    conflicts = []

    def both(name: str) -> bool:
        return bool(pdf.get(name) and registry.get(name))

    if both("long_title") and _similar(pdf["long_title"], registry["long_title"]) < TITLE_AGREEMENT:
        conflicts.append("registry_conflict:long_title")
    if both("protocol_no") and comparison_key(registry["protocol_no"]) not in comparison_key(
        pdf["protocol_no"]
    ):
        conflicts.append("registry_conflict:protocol_no")
    if pdf_phase and registry.get("phases") and registry_phase(registry["phases"]) != pdf_phase:
        conflicts.append("registry_conflict:phase")
    if both("sponsor_name") and _similar(pdf["sponsor_name"], registry["sponsor_name"]) < TITLE_AGREEMENT:
        conflicts.append("registry_conflict:sponsor_name")
    if trial_id and registry.get("nct_id") and registry["nct_id"].upper() != trial_id.upper():
        conflicts.append("registry_conflict:trial_id")
    return conflicts
