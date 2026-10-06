"""ClinicalTrials.gov v2: the registry record of a trial, used to fill and cross-check metadata."""

from __future__ import annotations

import re

from app.services.ctml.registries.http import RegistryHttp

CTGOV_URL = "https://clinicaltrials.gov/api/v2/studies"
# Not \b: file names such as "Prot_NCT01234567.pdf" put an underscore before the ID.
NCT_PATTERN = re.compile(r"(?<![A-Za-z0-9])NCT\d{8}(?!\d)", re.I)


def find_nct_id(*texts: str) -> str | None:
    for text in texts:
        match = NCT_PATTERN.search(text or "")
        if match:
            return match.group(0).upper()
    return None


def registry_fields(record: dict) -> dict:
    """The metadata fields the registry holds, in pipeline names."""
    protocol = record.get("protocolSection") or {}
    identification = protocol.get("identificationModule") or {}
    description = protocol.get("descriptionModule") or {}
    design = protocol.get("designModule") or {}
    arms_module = protocol.get("armsInterventionsModule") or {}
    sponsor = (protocol.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}
    return {
        "nct_id": identification.get("nctId"),
        "long_title": identification.get("officialTitle"),
        "short_title": identification.get("briefTitle"),
        "protocol_no": (identification.get("orgStudyIdInfo") or {}).get("id"),
        "phases": list(design.get("phases") or []),
        "sponsor_name": sponsor.get("name"),
        "brief_summary": description.get("briefSummary"),
        "arm_groups": [
            {
                "label": arm.get("label"),
                "type": arm.get("type"),
                "description": arm.get("description"),
                "interventions": list(arm.get("interventionNames") or []),
            }
            for arm in arms_module.get("armGroups") or []
        ],
        "interventions": [
            {
                "name": item.get("name"),
                "type": item.get("type"),
                "description": item.get("description"),
                "arm_groups": list(item.get("armGroupLabels") or []),
            }
            for item in arms_module.get("interventions") or []
        ],
    }


def registry_lines(fields: dict | None) -> dict[str, str]:
    """The registry's arm and intervention descriptions as citeable lines "R1", "R2", ... Only a
    value the protocol redacts or does not state may be taken from them."""
    if not fields:
        return {}
    texts = []
    for arm in fields.get("arm_groups") or []:
        interventions = ", ".join(arm.get("interventions") or [])
        texts.append(
            f"Arm '{arm.get('label')}' ({arm.get('type')}; {interventions}): {arm.get('description') or ''}"
        )
    for item in fields.get("interventions") or []:
        texts.append(
            f"Intervention '{item.get('name')}' ({item.get('type')}): {item.get('description') or ''}"
        )
    return {f"R{index}": " ".join(text.split()) for index, text in enumerate(texts, start=1)}


async def fetch_study(http: RegistryHttp, nct_id: str) -> tuple[dict | None, str]:
    data, source = await http.get_json(f"{CTGOV_URL}/{nct_id}", params={"format": "json"}, not_found_ok=True)
    return (registry_fields(data) if isinstance(data, dict) else None), source
