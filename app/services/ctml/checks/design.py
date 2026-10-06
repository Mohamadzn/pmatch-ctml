"""Checks run by submit_design."""

from __future__ import annotations

import re

from app.services.ctml.checks.common import (
    apply_confirmations,
    closest_span,
    confirm,
    line_texts,
    numbers_in,
    reject,
    unknown_lines,
)
from app.services.ctml.contracts import Confirmation, DesignSubmission
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.text import comparison_key, contains_phrase

MAX_LEVEL_DESCRIPTION = 160
PLACEBO = "placebo"
STARTING_DOSE = "(starting dose)"
# A dose amount: a number with a mass or volume unit, or a number joined to others by "/"
# ("100/200 mg").
_DOSE_AMOUNT = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:mg|g|µg|mcg|ug|ng|mL|ml|IU|units?)\b|\d+(?:[.,]\d+)?\s*/\s*\d", re.I
)


def doses_beside_starting_dose(description: str) -> bool:
    """A level description that marks a starting dose and states further dose amounts."""
    if STARTING_DOSE not in description.casefold():
        return False
    return len(_DOSE_AMOUNT.findall(description)) > 1


def historical_lines(context: RunContext) -> set[str]:
    """Lines inside history blocks (summary of changes, revision history)."""
    result: set[str] = set()
    for section in context.routing.sections:
        if section.historical:
            result.update(line.line_id for line in context.document.lines[section.start : section.end])
    return result


def _line_problems(
    path: str, line_ids: list[str], context: RunContext, historical: set[str], registry: bool = False
) -> list[dict]:
    """Unknown or only historical lines. With registry=True, ClinicalTrials.gov lines ("R1") count."""
    known = set(context.registry_lines) if registry else set()
    missing = [line_id for line_id in unknown_lines(context.document, line_ids) if line_id not in known]
    if missing:
        return [reject("unknown_line_id", f"{path}/line_ids", "Unknown line IDs: " + ", ".join(missing))]
    old = [line_id for line_id in line_ids if line_id in historical]
    if old and len(old) == len(line_ids):
        return [
            reject(
                "historical_line",
                f"{path}/line_ids",
                "These lines describe earlier protocol versions. Cite the current text: " + ", ".join(old),
            )
        ]
    return []


def check_design(
    submission: DesignSubmission,
    confirmations: list[Confirmation],
    context: RunContext,
    state: AgentState,
) -> tuple[list[dict], list[dict]]:
    problems: list[dict] = []
    document = context.document
    historical = historical_lines(context)
    body = line_texts(document, [line.line_id for line in document.lines if line.line_id not in historical])
    if not contains_phrase(body, submission.design_quote):
        problems.append(
            reject(
                "quote_not_in_source",
                "/design_quote",
                "Quote exact protocol words. Closest: " + closest_span(body, submission.design_quote),
            )
        )

    drug_names = {comparison_key(drug.name) for drug in submission.drugs}
    for index, drug in enumerate(submission.drugs):
        path = f"/drugs/{index}"
        found = _line_problems(path, drug.line_ids, context, historical)
        problems.extend(found)
        if not found and not contains_phrase(line_texts(document, drug.line_ids), drug.name):
            problems.append(
                reject("drug_not_in_cited_lines", f"{path}/name", f"'{drug.name}' is not in the cited lines.")
            )

    known_labels = set(context.inventory.scope_labels())
    assigned: set[str] = set()
    arm_codes: set[str] = set()
    for arm_index, arm in enumerate(submission.arms):
        path = f"/arms/{arm_index}"
        problems.extend(_line_problems(path, arm.line_ids, context, historical))
        key = comparison_key(arm.arm_code)
        if key in arm_codes:
            problems.append(reject("duplicate_arm_code", f"{path}/arm_code", "Arm codes must be unique."))
        arm_codes.add(key)
        for label in arm.scope_labels:
            if label not in known_labels:
                problems.append(
                    reject(
                        "unknown_scope_label",
                        f"{path}/scope_labels",
                        f"'{label}' is not one of the eligibility scope labels: {sorted(known_labels)}",
                    )
                )
            assigned.add(label)
        for level_index, level in enumerate(arm.dose_levels):
            level_path = f"{path}/dose_levels/{level_index}"
            found = _line_problems(level_path, level.line_ids, context, historical, registry=True)
            problems.extend(found)
            code = comparison_key(level.level_code)
            if code not in drug_names and code != PLACEBO:
                problems.append(
                    reject(
                        "level_code_not_a_drug",
                        f"{level_path}/level_code",
                        "level_code must be a drug name from drugs, or Placebo.",
                    )
                )
            if doses_beside_starting_dose(level.level_description):
                problems.append(
                    confirm(
                        "starting_dose_with_other_levels",
                        f"{level_path}/level_description",
                        "For dose escalation give only the starting dose, route and schedule, for example "
                        "'25 mg (starting dose) PO BID'. Later levels, dose reductions and sub-study doses "
                        "are not dose levels. Confirm only if the arm assigns these doses from the start.",
                    )
                )
            if len(level.level_description) > MAX_LEVEL_DESCRIPTION:
                problems.append(
                    reject(
                        "level_description_too_long",
                        f"{level_path}/level_description",
                        f"Keep it under {MAX_LEVEL_DESCRIPTION} characters: dose, unit, route, schedule.",
                    )
                )
            if not found:
                description = level.level_description
                for drug in submission.drugs:
                    description = description.replace(drug.name, " ")
                cited = (
                    line_texts(document, level.line_ids)
                    + "\n"
                    + "\n".join(context.registry_lines.get(line_id, "") for line_id in level.line_ids)
                )
                missing = numbers_in(description) - numbers_in(cited)
                if missing:
                    problems.append(
                        reject(
                            "dose_number_not_in_source",
                            f"{level_path}/level_description",
                            "Numbers not in the cited lines: " + ", ".join(sorted(missing)),
                        )
                    )

    for label in sorted(known_labels - assigned):
        problems.append(
            confirm(
                "scope_label_unassigned",
                "/arms",
                f"Eligibility criteria are scoped to '{label}'. Add it to the scope_labels of the arms "
                "it applies to. Confirm only if no arm of this study has that population.",
            )
        )

    seen_arms: dict[tuple, str] = {}
    for arm_index, arm in enumerate(submission.arms):
        doses = sorted(
            (comparison_key(level.level_code), comparison_key(level.level_description))
            for level in arm.dose_levels
        )
        key = (tuple(sorted(arm.scope_labels)), tuple(doses))
        if key in seen_arms:
            problems.append(
                confirm(
                    "arms_not_distinct",
                    f"/arms/{arm_index}",
                    f"This arm has the same population labels, drugs and doses as '{seen_arms[key]}'. "
                    "Cohorts that receive the same regimen are one arm (name the cohorts in "
                    "arm_description). Confirm only if the protocol enrols them as separate arms.",
                )
            )
        else:
            seen_arms[key] = arm.arm_code

    if submission.design_type == "randomized":
        groups: dict[tuple[str, ...], int] = {}
        for arm in submission.arms:
            key = tuple(sorted(arm.scope_labels))
            groups[key] = groups.get(key, 0) + 1
        if any(count > 1 for count in groups.values()):
            problems.append(
                confirm(
                    "randomized_multiple_arms",
                    "/arms",
                    "Randomized arms with the same eligibility are one arm "
                    "('Randomization Arm (A vs B): ...') with every drug, and placebo, as dose levels. "
                    "Confirm if the arms have different eligibility.",
                )
            )
    return apply_confirmations(problems, confirmations, body)
