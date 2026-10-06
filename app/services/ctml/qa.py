"""QA gate: schema and output-policy checks, blocking issues, the review list and the report.

The status is always "needs_review": a candidate is never approved by code. "blocked" means
the CTML has defects that must be fixed before review (schema or policy errors, criteria
without an encoding, arms without a match tree or with a contradictory one, missing required
metadata).
"""

from __future__ import annotations

import json
from collections import Counter

from app.services.ctml.schema.loader import schema_id, validation_errors

TOP_ORDER = [
    "trial_id",
    "long_title",
    "short_title",
    "protocol_no",
    "protocol_version_no",
    "protocol_version_date",
    "phase",
    "nct_purpose",
    "principal_investigator",
    "drug_list",
    "management_group_list",
    "site_list",
    "sponsor_list",
    "staff_list",
    "treatment_list",
    "additional_criteria_requirements",
]
ARM_ORDER = [
    "arm_code",
    "arm_suspended",
    "arm_description",
    "uuid",
    "dose_level",
    "match",
    "arm_additional_criteria_not_captured",
]
PRIOR_ORDER = [
    "treatment_category",
    "agent_class",
    "agent",
    "surgery_type",
    "radiation_site",
    "transplant_type",
]
OPTIONAL_TOP = {"protocol_version_no", "protocol_version_date"}
SHELLS = {
    "drug_list": {"drug": [{}]},
    "management_group_list": {"management_group": [{}]},
    "site_list": {"site": [{}]},
    "staff_list": {"protocol_staff": []},
}
BLOCKING_PREFIXES = (
    "criterion_not_encoded:",
    "arm_without_match:",
    "arm_contradiction:",
    "metadata_missing:",
    "scope_without_arm:",
    "design_not_accepted",
    # The criteria found by code may be incomplete: a reviewer checks document/inventory.md.
    "inventory:no_inclusion_criteria_found",
    "inventory:no_exclusion_criteria_found",
    "inventory:criterion_numbering_gap",
    "inventory:unnumbered_item",
    "inventory:repeated_number",
    "inventory:criterion_text_unfinished",
)


def _in_order(keys: list[str], order: list[str]) -> bool:
    ranks = [order.index(key) for key in keys if key in order]
    return ranks == sorted(ranks)


def _walk(node, path: str, problems: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if value is None:
                problems.append(f"{path}/{key}: null value")
            if (
                key == "prior_treatment"
                and isinstance(value, dict)
                and not _in_order(list(value), PRIOR_ORDER)
            ):
                problems.append(f"{path}/{key}: treatment_category must come first")
            _walk(value, f"{path}/{key}", problems)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk(item, f"{path}/{index}", problems)


def policy_errors(document: dict) -> list[str]:
    """Output rules JSON Schema cannot express (key order, shells, no status, no nulls)."""
    problems = []
    if not _in_order(list(document), TOP_ORDER):
        problems.append("/: top-level keys out of order")
    for key in TOP_ORDER:
        if key not in document and key not in OPTIONAL_TOP:
            problems.append(f"/{key}: missing")
    for key, shell in SHELLS.items():
        if key in document and document[key] != shell:
            problems.append(f"/{key}: must be the empty shell {json.dumps(shell)}")
    if document.get("principal_investigator", "") != "":
        problems.append('/principal_investigator: must be ""')
    if "status" in document:
        problems.append("/status: not written by the pipeline")
    for s, step in enumerate(document.get("treatment_list", {}).get("step", [])):
        for a, arm in enumerate(step.get("arm", [])):
            where = f"/treatment_list/step/{s}/arm/{a}"
            if not _in_order(list(arm), ARM_ORDER):
                problems.append(f"{where}: arm keys out of order")
            for key in ("arm_suspended", "uuid", "match", "arm_additional_criteria_not_captured"):
                if key not in arm:
                    problems.append(f"{where}/{key}: missing")
            if not arm.get("match"):
                problems.append(f"{where}/match: empty")
    _walk(document, "", problems)
    return problems


def duplicate_texts(document: dict) -> list[str]:
    texts = [" ".join(t.split()).casefold() for t in document.get("additional_criteria_requirements", [])]
    return [text for text, count in Counter(texts).items() if count > 1]


def build_qa(ctml: dict | None, evidence: dict, review: list[str], run_info: dict) -> dict:
    schema = validation_errors(ctml, limit=50) if ctml is not None else []
    policy = policy_errors(ctml) if ctml is not None else []
    blocking = [item for item in review if item.startswith(BLOCKING_PREFIXES)]
    if ctml is None:
        blocking.append("no_ctml_written")
    blocking += [f"schema:{e['path']}: {e['message']}" for e in schema]
    blocking += [f"policy:{p}" for p in policy]
    criteria = evidence.get("criteria", [])
    representable = Counter(c.get("representable", "not encoded") for c in criteria)
    placements = Counter(c.get("text_placement", "").split(" (")[0].split(":")[0] for c in criteria)
    label_differences = [
        {
            "criterion_id": c["criterion_id"],
            "path": leaf["path"],
            "leaf": leaf["leaf"],
            "adds": leaf["label_adds_words"],
        }
        for c in criteria
        for leaf in c.get("leaves", [])
        if leaf.get("label_adds_words")
    ]
    arms = (ctml or {}).get("treatment_list", {}).get("step", [{}])[0].get("arm", []) if ctml else []
    return {
        "status": "needs_review",
        "blocked": bool(blocking),
        "schema_id": schema_id(),
        "blocking": blocking,
        "review_items": [item for item in review if item not in blocking],
        "counts": {
            "criteria": len(criteria),
            "accepted": sum(1 for c in criteria if c.get("status") == "accepted"),
            "representable": dict(representable),
            "text_placement": dict(placements),
            "arms": len(arms),
            "trial_additional_criteria": len((ctml or {}).get("additional_criteria_requirements", [])),
            "arm_additional_criteria": [len(a.get("arm_additional_criteria_not_captured", [])) for a in arms],
            "duplicate_trial_texts": len(duplicate_texts(ctml)) if ctml else 0,
            "implied_conditions_removed": sum(
                len(arm["removed"]) for arm in evidence.get("implied_conditions_removed", [])
            ),
        },
        "label_differences": label_differences,
        "run": run_info,
    }


def _table(rows: list[list[str]], header: list[str]) -> str:
    def cell(value) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def qa_markdown(qa: dict, evidence: dict, metadata_log: list[dict]) -> str:
    run = qa["run"]
    parts = [
        f"# QA report: {run.get('trial_id') or run.get('pdf')}",
        "",
        f"Status: **{qa['status']}**{' (blocked)' if qa['blocked'] else ''}. "
        "This is a review candidate, not a clinically validated result.",
        "",
        "## Run",
        "",
        _table(
            [[key, value] for key, value in run.items() if not isinstance(value, (dict, list))],
            ["item", "value"],
        ),
        "",
        "## Blocking issues",
        "",
        "\n".join(f"- {item}" for item in qa["blocking"]) or "None.",
        "",
        "## Review items",
        "",
        "\n".join(f"- {item}" for item in qa["review_items"]) or "None.",
        "",
        "## Counts",
        "",
        "```json",
        json.dumps(qa["counts"], indent=2),
        "```",
        "",
        "## Metadata sources",
        "",
        _table(
            [
                [m["field"], m["source"], m.get("protocol") or "", m.get("registry") or ""]
                for m in metadata_log
            ],
            ["field", "used", "protocol", "ClinicalTrials.gov"],
        ),
        "",
        "## Criteria",
        "",
        _table(
            [
                [
                    c["criterion_id"],
                    c["kind"],
                    c.get("scope_label") or "all",
                    c.get("representable", "-"),
                    c.get("context_category", "-"),
                    c.get("text_placement", ""),
                    _submits(c.get("agent", {})),
                    len(c.get("confirmations", [])),
                ]
                for c in evidence.get("criteria", [])
            ],
            ["id", "kind", "scope", "encoded", "category", "text goes to", "submits", "confirmations"],
        ),
        "",
        "## Registry labels that add words to the source phrase",
        "",
        "Check that each label keeps the protocol's meaning.",
        "",
        _table(
            [
                [
                    d["criterion_id"],
                    json.dumps(d["leaf"]),
                    ", ".join(f"{k}: {v}" for k, v in d["adds"].items()),
                ]
                for d in qa["label_differences"]
            ],
            ["criterion", "leaf", "added words"],
        )
        if qa["label_differences"]
        else "None.",
        "",
    ]
    parts += _workflow_section(evidence.get("workflow"))
    return "\n".join(parts)


def _submits(agent: dict) -> str:
    """Submits of the criterion agent, or of each v5 agent of the criterion."""
    if "submits" in agent:
        return str(agent["submits"])
    return ", ".join(
        f"{role} {value['submits']}"
        for role, value in agent.items()
        if isinstance(value, dict) and "submits" in value
    )


def _workflow_section(workflow: dict | None) -> list[str]:
    """The v5 supervisor workflow: repairs and the resolver's findings."""
    if not workflow:
        return []
    timeline = workflow.get("timeline", [])
    parts = [
        "## Supervisor workflow (v5)",
        "",
        f"Finished after {timeline[-1]['seconds'] if timeline else '?'} s. The steps are in evidence.json "
        "(workflow.timeline).",
        "",
        "### Repairs",
        "",
    ]
    repairs = workflow.get("repairs", {})
    parts.append(
        _table(
            [
                [
                    cid,
                    ", ".join(
                        f"{a['tag']} ({'accepted' if a['accepted'] else 'not accepted'})" for a in attempts
                    ),
                ]
                for cid, attempts in repairs.items()
            ],
            ["criterion", "eligibility logic runs"],
        )
        if repairs
        else "None."
    )
    parts += ["", "### Resolver findings", ""]
    resolver = workflow.get("resolver")
    if resolver is None:
        parts.append("The resolver did not run.")
    elif not resolver.get("issues"):
        parts.append(resolver.get("summary") or "No findings.")
    else:
        parts.append(
            _table(
                [
                    [issue["target"], issue["action"], issue.get("routed_to", ""), issue["problem"]]
                    for issue in resolver["issues"]
                ],
                ["target", "action", "routed to", "problem"],
            )
        )
    parts.append("")
    return parts
