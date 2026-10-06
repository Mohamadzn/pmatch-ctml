"""Deterministic merge of the v5 agents' outputs into one criterion encoding.

The eligibility logic agent gives the structure: AND / OR groups of slots, each slot a
condition with its exact words and its polarity. The clinical, genomics and prior therapy
agents give the positive registry value of each slot of their domain. This module joins
them by slot ID into the CriterionSubmission the compiler already uses, and compares the
structure with the coverage agent's independent listing.

A slot that cannot be filled is never guessed. It becomes unmapped text and a review item:
- a condition removed from an AND makes the rule weaker (the text stays for the reviewer);
- alternatives of an OR that CTML can hold are kept and the others stay as text (UHN
  convention, C26): patients who qualify only through a text alternative are not matched
  automatically, and the review list says so;
- an OR of excluded conditions (an excluded combination, "X with Y") is never split: dropping
  one part would exclude more patients than the protocol does, so the whole OR goes to text.

A slot can carry several registry values when no single entry covers its words: any of them
satisfies the slot (an OR), and a negated slot excludes each of them (an AND of exclusions).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.services.ctml.contracts import (
    SENTINELS,
    Clinical,
    CoverageItem,
    CriterionSubmission,
    EligibilitySubmission,
    Genomic,
    GroupNode,
    LeafNode,
    LogicGroup,
    PriorTreatment,
    Slot,
    SlotFillBase,
    UnmappedItem,
)
from app.services.ctml.text import comparison_key

DOMAIN_FIELD = {"clinical": "clinical", "genomic": "genomic", "prior_treatment": "prior_treatment"}
DOMAIN_MODEL = {"clinical": Clinical, "genomic": Genomic, "prior_treatment": PriorTreatment}
_AGE = re.compile(r"^(>=|<=|>|<)(\d+(?:\.\d+)?)$")
_AGE_OPPOSITE = {">=": "<", "<": ">=", ">": "<=", "<=": ">"}
# A coverage item counts as covered when at least this share of its characters lies inside
# a slot concept or an unmapped quote of the structure.
COVERED_SHARE = 0.5


# --- walking the structure ----------------------------------------------------------------


def iter_slots(node: LogicGroup | Slot | None, path: str = "/logic") -> Iterator[tuple[str, Slot]]:
    if node is None:
        return
    if isinstance(node, Slot):
        yield path, node
        return
    for index, child in enumerate(node.items):
        yield from iter_slots(child, f"{path}/items/{index}")


def iter_logic_groups(
    node: LogicGroup | Slot | None, path: str = "/logic"
) -> Iterator[tuple[str, LogicGroup]]:
    if isinstance(node, LogicGroup):
        yield path, node
        for index, child in enumerate(node.items):
            yield from iter_logic_groups(child, f"{path}/items/{index}")


def slots_of(structure: EligibilitySubmission, domain: str | None = None) -> list[Slot]:
    return [slot for _, slot in iter_slots(structure.logic) if domain is None or slot.domain == domain]


def parent_group(node: LogicGroup | Slot | None, slot_id: str) -> LogicGroup | None:
    for _, group in iter_logic_groups(node):
        if any(isinstance(child, Slot) and child.slot_id == slot_id for child in group.items):
            return group
    return None


def evaluate_logic(node: LogicGroup | Slot, has: set[str]) -> bool:
    """Whether a patient who has exactly the conditions of the given slots meets the rule."""
    if isinstance(node, Slot):
        present = node.slot_id in has
        return not present if node.negated else present
    values = [evaluate_logic(child, has) for child in node.items]
    return all(values) if node.type == "and" else any(values)


def render_logic(
    node: LogicGroup | Slot | None, values: dict[str, str] | None = None, indent: int = 0
) -> str:
    """Readable form for agent messages and reviews."""
    pad = "  " * indent
    if node is None:
        return f"{pad}(no structure)"
    if isinstance(node, Slot):
        value = f" = {values[node.slot_id]}" if values and node.slot_id in values else ""
        sign = "NOT " if node.negated else ""
        return (
            f'{pad}{node.slot_id} [{node.domain}] {sign}"{node.concept}"{value} ({", ".join(node.line_ids)})'
        )
    lines = [f"{pad}{node.type.upper()}"]
    lines.extend(render_logic(child, values, indent + 1) for child in node.items)
    return "\n".join(lines)


# --- values and polarity --------------------------------------------------------------------


def negate_age(expression: str) -> str | None:
    match = _AGE.match(expression.replace(" ", ""))
    if match is None:
        return None
    return f"{_AGE_OPPOSITE[match.group(1)]}{match.group(2)}"


def fill_values(domain: str, fill: SlotFillBase) -> list[dict]:
    """The registry values of a fill, as leaf fields (several when no single entry covers the words)."""
    values = getattr(fill, DOMAIN_FIELD[domain], None) or []
    return [value.model_dump(exclude_none=True) for value in values if value is not None]


def apply_negation(domain: str, fields: dict) -> tuple[dict | None, str]:
    """(the leaf fields of the excluded condition, or None with the reason it cannot be stated)."""
    if domain == "clinical":
        keys = set(fields)
        if keys == {"oncotree_primary_diagnosis"}:
            value = str(fields["oncotree_primary_diagnosis"])
            if value in SENTINELS:
                return None, f"{value} cannot be excluded"
            return {"oncotree_primary_diagnosis": "!" + value}, ""
        if keys == {"age_expression"}:
            opposite = negate_age(str(fields["age_expression"]))
            if opposite is None:
                return None, "the excluded age cannot be stated as an age rule"
            return {"age_expression": opposite}, ""
        return None, "an excluded clinical condition can only be a diagnosis or an age"
    if domain == "genomic":
        # The CTML schema can exclude a mutation ("!Mutation") or a copy-number call ("!Gain"),
        # not a structural variant or a copy-number change without a call.
        negated = dict(fields)
        if fields.get("variant_category") == "Mutation":
            negated["variant_category"] = "!Mutation"
            return negated, ""
        if fields.get("variant_category") == "CNV" and fields.get("cnv_call"):
            negated["cnv_call"] = "!" + str(fields["cnv_call"])
            return negated, ""
        return None, "CTML can exclude a mutation or a copy-number call, not this alteration"
    named = [name for name in ("agent_class", "agent", "transplant_type") if fields.get(name)]
    if not named:
        return None, "a treatment category alone cannot be excluded"
    negated = dict(fields)
    for name in named:
        negated[name] = "!" + str(fields[name])
    return negated, ""


# --- merge ----------------------------------------------------------------------------------


@dataclass
class MergeOutcome:
    submission: CriterionSubmission
    # {slot_id, concept, reason, line_ids, alternative}; alternative: the slot belongs to an OR
    # alternative that was left out while the other alternatives were kept.
    dropped: list[dict] = field(default_factory=list)

    @property
    def leaves(self) -> int:
        return 0 if self.submission.tree is None else _count_leaves(self.submission.tree)


def _count_leaves(node: GroupNode | LeafNode) -> int:
    if isinstance(node, LeafNode):
        return 1
    return sum(_count_leaves(child) for child in node.items)


def merge_criterion(structure: EligibilitySubmission, fills: dict[str, SlotFillBase]) -> MergeOutcome:
    """Join the structure with the slot values. fills: slot_id -> the domain agent's fill
    (absent when that agent did not finish)."""
    dropped: list[dict] = []

    def is_dropped(slot: Slot) -> bool:
        return any(item["slot_id"] == slot.slot_id for item in dropped)

    def drop(slot: Slot, reason: str) -> None:
        if not is_dropped(slot):
            dropped.append(
                {
                    "slot_id": slot.slot_id,
                    "concept": slot.concept,
                    "reason": reason,
                    "line_ids": slot.line_ids,
                    "alternative": False,
                }
            )

    def leaf(slot: Slot, fields: dict, lookup_ids: list[str]) -> LeafNode:
        payload = {DOMAIN_FIELD[slot.domain]: DOMAIN_MODEL[slot.domain].model_validate(fields)}
        return LeafNode(
            type="leaf",
            **payload,
            source_concept=slot.concept,
            line_ids=list(slot.line_ids),
            lookup_ids=list(lookup_ids),
        )

    def build_slot(slot: Slot) -> GroupNode | LeafNode | None:
        fill = fills.get(slot.slot_id)
        if fill is None:
            drop(slot, f"the {slot.domain} agent gave no accepted value")
            return None
        if fill.status != "mapped":
            drop(slot, fill.reason or "no registry value fits the words")
            return None
        values = fill_values(slot.domain, fill)
        if not values:
            drop(slot, "the value is empty")
            return None
        leaves = []
        for fields in values:
            if slot.negated:
                fields, reason = apply_negation(slot.domain, fields)
                if fields is None:
                    drop(slot, reason)
                    return None
            leaves.append(leaf(slot, fields, fill.lookup_ids))
        if len(leaves) == 1:
            return leaves[0]
        # Several registry entries state the words: any of them satisfies the slot; an excluded
        # slot excludes each of them.
        return GroupNode(type="and" if slot.negated else "or", items=leaves)

    def build(node: LogicGroup | Slot) -> GroupNode | LeafNode | None:
        if isinstance(node, Slot):
            return build_slot(node)
        children = [build(child) for child in node.items]
        if node.type == "or" and any(child is None for child in children):
            excluded_combination = any(isinstance(child, Slot) and child.negated for child in node.items)
            if excluded_combination or all(child is None for child in children):
                reason = (
                    "another part of the same excluded combination cannot be encoded"
                    if excluded_combination
                    else "no alternative of this choice can be encoded"
                )
                for _, slot in iter_slots(node):
                    drop(slot, reason)
                return None
            # Alternatives CTML can hold are kept; the others stay as text (C26).
            for logic, child in zip(node.items, children, strict=True):
                if child is None:
                    for _, slot in iter_slots(logic):
                        drop(slot, "this alternative cannot be encoded")
                        for item in dropped:
                            if item["slot_id"] == slot.slot_id:
                                item["alternative"] = True
        kept = [child for child in children if child is not None]
        if not kept:
            return None
        if len(kept) == 1 and isinstance(kept[0], GroupNode) and kept[0].type == node.type:
            return kept[0]
        return GroupNode(type=node.type, items=kept)

    tree = build(structure.logic) if structure.logic is not None else None
    unmapped = list(structure.unmapped_items)
    for item in dropped:
        unmapped.append(UnmappedItem(quote=item["concept"], reason=item["reason"], line_ids=item["line_ids"]))
    if structure.logic is None:
        representable = structure.representable
    elif tree is None:
        representable = "none"
    elif dropped:
        representable = "partial"
    else:
        representable = structure.representable
    notes = list(structure.review_notes)
    submission = CriterionSubmission(
        representable=representable,
        tree=tree,
        unmapped_items=unmapped,
        context_category=structure.context_category,
        review_notes=notes,
    )
    return MergeOutcome(submission=submission, dropped=dropped)


# --- coverage -------------------------------------------------------------------------------


def _spans(text_key: str, phrase: str) -> list[tuple[int, int]]:
    key = comparison_key(phrase)
    if not key:
        return []
    spans, start = [], text_key.find(key)
    while start != -1:
        spans.append((start, start + len(key)))
        start = text_key.find(key, start + 1)
    return spans


def coverage_omissions(
    source: str, structure: EligibilitySubmission, items: list[CoverageItem]
) -> list[CoverageItem]:
    """Requirements and exceptions of the coverage listing that no slot concept and no
    unmapped quote of the structure covers. A criterion kept entirely as text has none."""
    if structure.representable == "none" or structure.logic is None:
        return []
    text_key = comparison_key(source)
    covered: set[int] = set()
    phrases = [slot.concept for slot in slots_of(structure)] + [
        item.quote for item in structure.unmapped_items
    ]
    for phrase in phrases:
        for start, end in _spans(text_key, phrase):
            covered.update(range(start, end))
    missing = []
    for item in items:
        if item.kind not in ("requirement", "exception"):
            continue
        spans = _spans(text_key, item.quote)
        if not spans:
            continue  # not exact words: nothing to compare
        start, end = spans[0]
        share = sum(1 for position in range(start, end) if position in covered) / max(1, end - start)
        if share < COVERED_SHARE:
            missing.append(item)
    return missing
