"""Checks run by submit_structure (the eligibility logic agent). None names a protocol, drug or
disease. The structure carries polarity and Boolean logic, so the logic and polarity checks of
the compact layout run here; registry checks run on the slot values (checks/slots.py)."""

from __future__ import annotations

from app.services.ctml.checks.common import (
    apply_confirmations,
    closest_span,
    confirm,
    reject,
    unknown_lines,
)
from app.services.ctml.checks.criterion import (
    CASE_BY_CASE_WORDS,
    EXCEPTION_WORDS,
    OPTIONAL_SUBSTUDY,
    criterion_lines,
    criterion_source,
)
from app.services.ctml.contracts import Confirmation, EligibilitySubmission, Slot
from app.services.ctml.document.inventory import Criterion
from app.services.ctml.merge import evaluate_logic, iter_logic_groups, iter_slots
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.text import contains_phrase

MAX_CONCEPT_CHARS = 250
MAX_EXAMPLES = 12


def _structure_problems(submission: EligibilitySubmission) -> list[dict]:
    problems = []
    if submission.representable == "none" and submission.logic is not None:
        problems.append(reject("logic_with_none", "/logic", "representable is none: remove the logic."))
    if submission.representable != "none" and submission.logic is None:
        problems.append(
            reject("missing_logic", "/logic", f"representable is {submission.representable}: add the logic.")
        )
    if submission.representable == "full" and submission.unmapped_items:
        problems.append(
            reject(
                "full_with_unmapped",
                "/unmapped_items",
                "full means nothing is left: use partial, or remove items.",
            )
        )
    if submission.representable == "partial" and not submission.unmapped_items:
        problems.append(
            reject("partial_without_unmapped", "/unmapped_items", "partial needs the unmapped items.")
        )
    if submission.context_category == OPTIONAL_SUBSTUDY and submission.logic is not None:
        problems.append(
            reject(
                "logic_for_optional_substudy",
                "/logic",
                "Criteria of an optional sub-study do not decide trial eligibility: use representable none "
                "and no logic.",
            )
        )
    return problems


def _slot_problems(
    submission: EligibilitySubmission, criterion: Criterion, source: str, context: RunContext
) -> list[dict]:
    problems = []
    seen: set[str] = set()
    own_lines = criterion_lines(context, criterion)
    for path, slot in iter_slots(submission.logic):
        if slot.slot_id in seen:
            problems.append(reject("duplicate_slot_id", f"{path}/slot_id", f"{slot.slot_id} is used twice."))
        seen.add(slot.slot_id)
        if not slot.concept.strip() or not contains_phrase(source, slot.concept):
            problems.append(
                reject(
                    "concept_not_in_source",
                    f"{path}/concept",
                    "Use exact words of the criterion. Closest: " + closest_span(source, slot.concept),
                )
            )
        elif len(slot.concept) > MAX_CONCEPT_CHARS:
            problems.append(
                reject(
                    "concept_too_long",
                    f"{path}/concept",
                    "A slot names one condition: quote only the words of that condition.",
                )
            )
        missing = unknown_lines(context.document, slot.line_ids)
        if missing:
            problems.append(
                reject("unknown_line_id", f"{path}/line_ids", "Unknown line IDs: " + ", ".join(missing))
            )
        elif not set(slot.line_ids) & own_lines:
            problems.append(
                reject(
                    "slot_not_in_criterion",
                    f"{path}/line_ids",
                    "Cite at least one line of this criterion (or of a table it refers to).",
                )
            )
    return problems


def _example_problems(submission: EligibilitySubmission) -> list[dict]:
    if submission.logic is None:
        return []
    ids = {slot.slot_id for _, slot in iter_slots(submission.logic)}
    examples = submission.examples
    problems = []
    if len(examples) > MAX_EXAMPLES:
        problems.append(reject("too_many_examples", "/examples", f"Give at most {MAX_EXAMPLES} examples."))
    disagreements = []
    agreed = {"eligible": False, "not_eligible": False}
    for index, example in enumerate(examples):
        unknown = [slot_id for slot_id in example.has if slot_id not in ids]
        if unknown:
            problems.append(
                reject(
                    "unknown_slot_in_example",
                    f"/examples/{index}/has",
                    "Unknown slot IDs: " + ", ".join(unknown),
                )
            )
            continue
        eligible = evaluate_logic(submission.logic, set(example.has))
        result = "eligible" if eligible else "not_eligible"
        if result != example.expect:
            has = ", ".join(example.has) or "no slot"
            disagreements.append(
                f"'{example.name}' (has {has}) is {result} under the logic, expected {example.expect}"
            )
        else:
            agreed[result] = True
    if disagreements:
        problems.append(
            reject(
                "example_disagreement",
                "/logic",
                "The logic disagrees with your examples: " + "; ".join(disagreements[:3]) + ". Fix the logic "
                "(AND/OR, negated) or the example.",
            )
        )
    if not (agreed["eligible"] and agreed["not_eligible"]):
        problems.append(
            reject(
                "examples_incomplete",
                "/examples",
                "Give at least one eligible and one not_eligible example that agree with the logic.",
            )
        )
    return problems


def _coverage_problems(submission: EligibilitySubmission, criterion: Criterion) -> list[dict]:
    if submission.representable == "none" or not criterion.item_line_ids:
        return []
    cited: set[str] = set()
    for _, slot in iter_slots(submission.logic):
        cited.update(slot.line_ids)
    for item in submission.unmapped_items:
        cited.update(item.line_ids)
    missing = [line_id for line_id in criterion.item_line_ids if line_id not in cited]
    if not missing:
        return []
    return [
        reject(
            "list_item_not_covered",
            "/logic",
            "Each listed item must be cited by a slot or by an unmapped item: " + ", ".join(missing),
        )
    ]


def _pattern_problems(submission: EligibilitySubmission, criterion: Criterion) -> list[dict]:
    """Confirm checks on polarity and grouping: usually wrong, sometimes right."""
    if submission.logic is None:
        return []
    problems = []
    slots = list(iter_slots(submission.logic))
    exclusion = criterion.kind == "exclusion"
    if exclusion and not any(slot.negated for _, slot in slots):
        problems.append(
            confirm(
                "exclusion_without_negation",
                "/logic",
                "This exclusion has no negated slot. Eligible patients must NOT have the excluded condition.",
            )
        )
    if not exclusion:
        for path, slot in slots:
            if slot.negated:
                problems.append(
                    confirm("inclusion_with_negation", path, "An inclusion criterion with a negated slot.")
                )
    for path, group in iter_logic_groups(submission.logic):
        children = group.items
        if (
            exclusion
            and group.type == "or"
            and len(children) > 1
            and all(isinstance(child, Slot) and child.negated for child in children)
        ):
            problems.append(
                confirm(
                    "exclusion_or_of_negations",
                    path,
                    "OR of negated slots excludes only patients who have ALL the items (a combination). "
                    "Excluding ANY of the items is AND of negated slots.",
                )
            )
        clinical = [child for child in children if isinstance(child, Slot) and child.domain == "clinical"]
        if not exclusion and group.type == "or" and clinical:
            negated = [slot for slot in clinical if slot.negated]
            if negated and len(negated) < len(clinical):
                problems.append(
                    confirm(
                        "inclusion_or_mixed_negation",
                        path,
                        "An OR mixing a negated clinical slot with positive ones admits almost everyone.",
                    )
                )
    return problems


def _exception_problems(submission: EligibilitySubmission, criterion: Criterion) -> list[dict]:
    """An exclusion with an exception: a negated slot also excludes the patients the exception
    keeps, unless the exception is encoded or the class hierarchy keeps them out."""
    if criterion.kind != "exclusion" or submission.logic is None:
        return []
    negated = [(path, slot) for path, slot in iter_slots(submission.logic) if slot.negated]
    if not negated:
        return []
    problems = []
    open_exceptions = [
        item
        for item in submission.unmapped_items
        if EXCEPTION_WORDS.search(item.quote) and not CASE_BY_CASE_WORDS.search(item.quote)
    ]
    rejected: set[str] = set()
    for item in open_exceptions:
        for path, slot in negated:
            if path not in rejected and set(slot.line_ids) & set(item.line_ids):
                rejected.add(path)
                problems.append(
                    reject(
                        "negation_with_open_exception",
                        path,
                        f"The exception '{item.quote[:120]}' stays unmapped, so this negated slot would also "
                        "exclude the patients the exception keeps. Remove the slot and keep the criterion as "
                        "text, or encode the exception.",
                    )
                )
    if EXCEPTION_WORDS.search(criterion.text):
        for path, _ in negated:
            if path not in rejected:
                problems.append(
                    confirm(
                        "exclusion_exception",
                        path,
                        "This exclusion states an exception. A negated slot also excludes the patients the "
                        "exception keeps, unless they are outside the excluded class (for example, the "
                        "allowed drugs act on other targets). Confirm with the words that exclude this item, "
                        "or keep the criterion as text.",
                    )
                )
    return problems


def check_structure(
    submission: EligibilitySubmission,
    confirmations: list[Confirmation],
    criterion: Criterion,
    context: RunContext,
    state: AgentState,
) -> tuple[list[dict], list[dict]]:
    """(problems left to fix, accepted confirmations)."""
    source = criterion_source(context, criterion, state)
    problems = _structure_problems(submission)
    for index, item in enumerate(submission.unmapped_items):
        if not contains_phrase(source, item.quote):
            problems.append(
                reject(
                    "unmapped_quote_not_in_source",
                    f"/unmapped_items/{index}/quote",
                    "Quote exact words. Closest: " + closest_span(source, item.quote),
                )
            )
        missing = unknown_lines(context.document, item.line_ids)
        if missing:
            problems.append(
                reject(
                    "unknown_line_id", f"/unmapped_items/{index}/line_ids", "Unknown: " + ", ".join(missing)
                )
            )
    problems.extend(_slot_problems(submission, criterion, source, context))
    problems.extend(_example_problems(submission))
    problems.extend(_coverage_problems(submission, criterion))
    problems.extend(_pattern_problems(submission, criterion))
    problems.extend(_exception_problems(submission, criterion))
    return apply_confirmations(problems, confirmations, criterion_source(context, criterion, None))
