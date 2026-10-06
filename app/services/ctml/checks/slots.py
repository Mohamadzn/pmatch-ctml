"""Checks run by submit_clinical, submit_genomics and submit_prior_therapy. None names a
protocol, drug or disease. Each value of a fill is checked as the CTML leaf it becomes, with
the registry checks of the compact layout: every registry value must come from a search this
agent made, copied exactly, of the right kind (drug or class)."""

from __future__ import annotations

from app.services.ctml.checks.common import apply_confirmations, confirm, reject
from app.services.ctml.checks.criterion import _leaf_problems, criterion_source
from app.services.ctml.contracts import SENTINELS, Confirmation, EligibilitySubmission, LeafNode, Slot
from app.services.ctml.document.inventory import Criterion
from app.services.ctml.merge import (
    DOMAIN_FIELD,
    DOMAIN_MODEL,
    apply_negation,
    parent_group,
    slots_of,
)
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.schema.loader import validation_errors

# Several values are for words no single registry entry covers; more than this is a list of
# examples, not one condition.
MAX_VALUES = 12
# The words and lines of a slot were checked when the structure was accepted.
STRUCTURE_CODES = {"source_concept_not_in_source", "unknown_line_id", "leaf_not_in_criterion"}
# A domain agent cannot remove a slot: where the shared leaf check says "remove it", the domain
# agent uses cannot_map.
DOMAIN_HINTS = {
    "category_only_in_partial": "A treatment category alone requires any treatment of that category from "
    "every patient. Parts of this criterion stay as text, so it is not faithful here: use cannot_map with "
    "that reason.",
}


def fill_leaf(slot: Slot, fields: dict, lookup_ids: list[str]) -> LeafNode:
    payload = {DOMAIN_FIELD[slot.domain]: DOMAIN_MODEL[slot.domain].model_validate(fields)}
    return LeafNode(
        type="leaf",
        **payload,
        source_concept=slot.concept,
        line_ids=list(slot.line_ids),
        lookup_ids=lookup_ids,
    )


def _has_bang(fields: dict) -> bool:
    return any(isinstance(value, str) and value.strip().startswith("!") for value in fields.values())


def _value_problems(
    domain: str, slot: Slot, fields: dict, path: str, structure: EligibilitySubmission
) -> list[dict]:
    """Polarity and sentinel checks of one value of a fill."""
    if _has_bang(fields):
        return [
            reject(
                "negation_in_value",
                path,
                "Give the positive value without '!': the structure decides whether it is excluded.",
            )
        ]
    problems = []
    if slot.negated:
        negated, reason = apply_negation(domain, fields)
        if negated is None:
            problems.append(
                reject(
                    "cannot_exclude",
                    path,
                    f"The structure excludes this slot, but {reason}. Use cannot_map with that reason.",
                )
            )
        else:
            for error in validation_errors({DOMAIN_FIELD[domain]: negated}, "CTMLMatchLeaf", limit=3):
                problems.append(reject("leaf_schema", f"{path}{error['path']}", error["message"]))
    diagnosis = fields.get("oncotree_primary_diagnosis") if domain == "clinical" else None
    if diagnosis in SENTINELS:
        group = parent_group(structure.logic, slot.slot_id)
        siblings = [
            child
            for child in (group.items if group is not None else [])
            if isinstance(child, Slot) and child.slot_id != slot.slot_id and child.domain == "clinical"
        ]
        if group is not None and group.type == "or" and siblings:
            problems.append(
                confirm(
                    "sentinel_beside_diagnoses",
                    f"{path}/oncotree_primary_diagnosis",
                    f"{diagnosis} in an OR with specific diagnoses admits every solid or liquid tumor.",
                )
            )
    return problems


def check_fills(
    domain: str,
    fills: list,
    confirmations: list[Confirmation],
    criterion: Criterion,
    structure: EligibilitySubmission,
    context: RunContext,
    state: AgentState,
) -> tuple[list[dict], list[dict]]:
    """(problems left to fix, accepted confirmations) for one domain agent's fills."""
    problems: list[dict] = []
    slots = {slot.slot_id: slot for slot in slots_of(structure, domain)}
    source = criterion_source(context, criterion, state)
    seen: set[str] = set()
    for index, fill in enumerate(fills):
        path = f"/fills/{index}"
        slot = slots.get(fill.slot_id)
        if slot is None:
            problems.append(
                reject(
                    "unknown_slot",
                    f"{path}/slot_id",
                    f"{fill.slot_id} is not one of your slots: " + ", ".join(sorted(slots)),
                )
            )
            continue
        if fill.slot_id in seen:
            problems.append(reject("duplicate_fill", f"{path}/slot_id", f"{fill.slot_id} has two fills."))
            continue
        seen.add(fill.slot_id)
        field = DOMAIN_FIELD[domain]
        values = [value for value in getattr(fill, field, None) or [] if value is not None]
        if fill.status == "cannot_map":
            if not fill.reason.strip():
                problems.append(reject("cannot_map_without_reason", f"{path}/reason", "Give the reason."))
            if values:
                problems.append(
                    reject(
                        "value_with_cannot_map", path, "cannot_map has no value: remove it, or use mapped."
                    )
                )
            continue
        if not values:
            problems.append(reject("mapped_without_value", path, f"mapped needs the {field} values."))
            continue
        if len(values) > MAX_VALUES:
            problems.append(
                reject(
                    "too_many_values",
                    f"{path}/{field}",
                    f"Give at most {MAX_VALUES} values: several values are only for words that no single "
                    "registry entry covers.",
                )
            )
            continue
        for number, value in enumerate(values):
            value_path = f"{path}/{field}/{number}"
            fields = value.model_dump(exclude_none=True)
            problems.extend(_value_problems(domain, slot, fields, value_path, structure))
            if _has_bang(fields):
                continue
            leaf = fill_leaf(slot, fields, list(fill.lookup_ids))
            for problem in _leaf_problems(
                value_path, leaf, criterion, source, context, state, structure.representable
            ):
                if problem["code"] not in STRUCTURE_CODES:
                    problems.append(
                        {
                            **problem,
                            "path": problem["path"].replace(f"{value_path}/{field}/", f"{value_path}/"),
                            "hint": DOMAIN_HINTS.get(problem["code"], problem["hint"]),
                        }
                    )
    for slot_id in sorted(set(slots) - seen):
        problems.append(reject("slot_without_fill", "/fills", f"Give a fill for {slot_id}."))
    return apply_confirmations(problems, confirmations, criterion_source(context, criterion, None))
