"""Checks run by submit_criterion. None names a protocol, drug or disease."""

from __future__ import annotations

import re

from app.services.ctml.checks.common import (
    apply_confirmations,
    closest_span,
    confirm,
    line_texts,
    reject,
    unknown_lines,
)
from app.services.ctml.contracts import SENTINELS, Confirmation, CriterionSubmission, GroupNode, LeafNode
from app.services.ctml.document.inventory import Criterion
from app.services.ctml.matching import compile_tree, iter_groups, iter_leaves, leaf_payload, tree_hash
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.schema.loader import validation_errors
from app.services.ctml.text import contains_phrase

REGISTRY_FIELDS = {
    ("clinical", "oncotree_primary_diagnosis"): "oncotree",
    ("genomic", "hugo_symbol"): "hgnc",
    ("genomic", "fusion_partner_hugo_symbol"): "hgnc",
    ("prior_treatment", "agent"): "ncit",
    ("prior_treatment", "agent_class"): "ncit",
}
# English words that introduce an exception to an exclusion ("unless ...", "except ...",
# "... is allowed"). Language, not medical terms.
EXCEPTION_WORDS = re.compile(
    r"\b(unless|except|exception|other than|apart from|excluding"
    r"|(?:is|are) (?:allowed|permitted|eligible))\b",
    re.I,
)
# Words of a waiver granted case by case ("may be eligible after consultation with the
# Sponsor"): the requirement or exclusion stands by default. Language, not medical terms.
CASE_BY_CASE_WORDS = re.compile(
    r"\b(sponsor|medical monitor|case[- ]by[- ]case|(?:upon|after|following) (?:consultation|discussion)"
    r"|with (?:the )?approval)\b",
    re.I,
)
OPTIONAL_SUBSTUDY = "optional sub-study"
NEGATABLE = {
    "clinical": ("oncotree_primary_diagnosis",),
    "genomic": (
        "hugo_symbol",
        "variant_category",
        "protein_change",
        "wildcard_protein_change",
        "cnv_call",
        "fusion_partner_hugo_symbol",
    ),
    "prior_treatment": ("agent", "agent_class", "transplant_type"),
}


def criterion_lines(context: RunContext, criterion: Criterion) -> set[str]:
    """The criterion's own lines plus the lines of the tables it refers to."""
    lines = set(criterion.line_ids)
    for table_id in criterion.table_ids:
        table = context.document.tables.get(table_id)
        if table is not None:
            lines.add(table.line_id)
    return lines


def criterion_source(context: RunContext, criterion: Criterion, state: AgentState | None) -> str:
    """Everything a source_concept may come from; without a state (confirmation quotes), only
    the criterion's own text, stem, tables and abbreviation definitions."""
    parts = [criterion.text, criterion.stem]
    parts.append(
        line_texts(
            context.document,
            [context.document.tables[t].line_id for t in criterion.table_ids if t in context.document.tables],
        )
    )
    parts.extend(d.definition for d in context.glossary.for_text(criterion.text))
    if state is not None:
        parts.append(line_texts(context.document, sorted(state.seen_line_ids)))
    return "\n".join(part for part in parts if part)


def _is_negated(leaf: LeafNode) -> bool:
    kind = leaf.kind()
    if kind is None:
        return False
    fields = getattr(leaf, kind)
    return any(str(getattr(fields, name, "") or "").startswith("!") for name in NEGATABLE[kind])


def _registry_problems(path: str, leaf: LeafNode, context: RunContext, state: AgentState) -> list[dict]:
    problems = []
    records = []
    for lookup_id in leaf.lookup_ids:
        record = context.ledger.get(lookup_id)
        if record is None or record.owner != state.owner:
            problems.append(
                reject(
                    "unknown_lookup_id",
                    f"{path}/lookup_ids",
                    f"{lookup_id} is not a lookup made for this criterion.",
                )
            )
        else:
            records.append(record)
    kind = leaf.kind()
    fields = getattr(leaf, kind) if kind else None
    for (leaf_kind, name), registry in REGISTRY_FIELDS.items():
        if kind != leaf_kind or fields is None or not getattr(fields, name, None):
            continue
        raw = str(getattr(fields, name))
        negated, value = raw.startswith("!"), raw.lstrip("!").strip()
        field_path = f"{path}/{kind}/{name}"
        if name == "oncotree_primary_diagnosis" and value in SENTINELS:
            if negated:
                problems.append(reject("negated_sentinel", field_path, f"!{value} is not allowed."))
            continue
        matching = [r for r in records if r.registry == registry]
        candidates = [c for r in matching for c in r.candidates]
        names = {str(c.get("symbol") or c.get("name") or "") for c in candidates}
        names |= {str(p.get("name", "")) for c in candidates for p in c.get("parents") or []}
        exact = next((n for n in names if n.casefold() == value.casefold()), None)
        if exact is not None and exact != value:
            problems.append(
                reject("name_not_exact", field_path, f"Copy the registry name exactly: '{exact}'.")
            )
            continue
        if registry == "hgnc":
            ok = any(c.get("symbol") == value for c in candidates)
        elif registry == "oncotree":
            ok = any(c.get("name") == value for c in candidates)
        else:
            by_name = [c for c in candidates if c.get("name") == value]
            parent_names = {str(p.get("name", "")) for c in candidates for p in c.get("parents") or []}
            if name == "agent":
                ok = any(c.get("kind") == "drug" for c in by_name)
                if not ok and (by_name or value in parent_names):
                    problems.append(
                        reject(
                            "agent_is_class",
                            field_path,
                            f"'{value}' is a class. Use agent_class for a class.",
                        )
                    )
                    continue
            else:
                ok = any(c.get("kind") == "class" for c in by_name) or value in parent_names
                if not ok and by_name:
                    drug = any(c.get("kind") == "drug" for c in by_name)
                    code, hint = (
                        ("agent_class_is_drug", f"'{value}' is a drug. Use agent for a single drug.")
                        if drug
                        else (
                            "agent_class_not_a_drug_class",
                            f"'{value}' is not a drug class (see semantic_types).",
                        )
                    )
                    problems.append(reject(code, field_path, hint))
                    continue
        if not ok:
            problems.append(
                reject(
                    "value_not_from_lookup",
                    field_path,
                    f"'{value}' was not returned by a {registry} search listed in this leaf's lookup_ids. "
                    "Search, then copy the name exactly and add the lookup_id.",
                )
            )
    return problems


def _leaf_problems(
    path: str,
    leaf: LeafNode,
    criterion: Criterion,
    source: str,
    context: RunContext,
    state: AgentState,
    representable: str = "full",
) -> list[dict]:
    problems = []
    if leaf.kind() is None:
        return [
            reject("leaf_kind", path, "A leaf needs exactly one of clinical, genomic or prior_treatment.")
        ]
    fields = getattr(leaf, leaf.kind())
    if not fields.model_dump(exclude_none=True):
        problems.append(reject("empty_leaf", path, "The leaf has no field values."))
    if not leaf.source_concept.strip() or not contains_phrase(source, leaf.source_concept):
        problems.append(
            reject(
                "source_concept_not_in_source",
                f"{path}/source_concept",
                "Use exact words of the criterion. Closest: " + closest_span(source, leaf.source_concept),
            )
        )
    missing = unknown_lines(context.document, leaf.line_ids)
    if missing:
        problems.append(
            reject("unknown_line_id", f"{path}/line_ids", "Unknown line IDs: " + ", ".join(missing))
        )
    elif not set(leaf.line_ids) & criterion_lines(context, criterion):
        problems.append(
            reject(
                "leaf_not_in_criterion",
                f"{path}/line_ids",
                "Cite at least one line of this criterion (or of a table it refers to).",
            )
        )
    for error in validation_errors(leaf_payload(leaf), "CTMLMatchLeaf", limit=5):
        problems.append(reject("leaf_schema", f"{path}{error['path']}", error["message"]))
    problems.extend(_registry_problems(path, leaf, context, state))
    if leaf.prior_treatment is not None and set(leaf.prior_treatment.model_dump(exclude_none=True)) == {
        "treatment_category"
    }:
        if representable == "partial":
            problems.append(
                reject(
                    "category_only_in_partial",
                    path,
                    "A leaf with only treatment_category requires any treatment of that category from "
                    "every patient. With parts of this criterion left as text (counts of prior lines, "
                    "alternatives, exceptions, cohort limits), it is not faithful: remove it and keep "
                    "the text.",
                )
            )
        else:
            problems.append(
                confirm(
                    "category_only_leaf",
                    path,
                    "A leaf with only treatment_category matches any treatment of that category. Confirm the "
                    "criterion requires any prior treatment of that category and names nothing more "
                    "specific, or name the drug or class.",
                )
            )
    diagnosis = leaf.clinical.oncotree_primary_diagnosis if leaf.clinical else None
    if diagnosis and not diagnosis.startswith("!") and diagnosis not in SENTINELS and context.oncotree:
        code = context.oncotree.code_of(diagnosis)
        if code is not None and context.oncotree.nodes[code].get("level") == 1:
            problems.append(
                confirm(
                    "tissue_level_diagnosis",
                    f"{path}/clinical/oncotree_primary_diagnosis",
                    f"'{diagnosis}' is a tissue-level OncoTree node: it covers every tumor of that tissue, "
                    "including in situ and benign ones. Use the most specific node that covers the whole "
                    "population, or confirm with the criterion's words.",
                )
            )
    return problems


def _coverage_problems(submission: CriterionSubmission, criterion: Criterion) -> list[dict]:
    if submission.representable == "none" or not criterion.item_line_ids:
        return []
    cited: set[str] = set()
    if submission.tree is not None:
        for _, leaf in iter_leaves(submission.tree):
            cited.update(leaf.line_ids)
    for item in submission.unmapped_items:
        cited.update(item.line_ids)
    missing = [line_id for line_id in criterion.item_line_ids if line_id not in cited]
    if not missing:
        return []
    return [
        reject(
            "list_item_not_covered",
            "/tree",
            "Each listed item must be cited by a leaf or by an unmapped item: " + ", ".join(missing),
        )
    ]


def _test_problems(submission: CriterionSubmission, state: AgentState) -> list[dict]:
    if submission.tree is None:
        return []
    try:
        digest = tree_hash(compile_tree(submission.tree))
    except ValueError:
        return []  # reported as leaf_kind
    test = state.tests.get(digest)
    if test is None:
        return [
            reject(
                "tree_not_tested",
                "/tree",
                "Call test_tree with this exact tree and at least one eligible and one not_eligible patient.",
            )
        ]
    problems = []
    if test["disagreements"]:
        problems.append(
            reject(
                "test_disagreement",
                "/tree",
                "test_tree results disagree: " + "; ".join(test["disagreements"][:3]),
            )
        )
    if not (test["eligible_agreed"] and test["not_eligible_agreed"]):
        problems.append(
            reject(
                "test_incomplete",
                "/tree",
                "test_tree needs one evaluable eligible patient and one evaluable not_eligible "
                "patient that agree.",
            )
        )
    return problems


def _exception_problems(submission: CriterionSubmission, criterion: Criterion) -> list[dict]:
    """An exclusion with an exception: a negated leaf also excludes the patients the exception
    keeps, unless the exception is encoded or the registry hierarchy keeps them out."""
    if criterion.kind != "exclusion" or submission.tree is None:
        return []
    negated = [(path, leaf) for path, leaf in iter_leaves(submission.tree) if _is_negated(leaf)]
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
        for path, leaf in negated:
            if path not in rejected and set(leaf.line_ids) & set(item.line_ids):
                rejected.add(path)
                problems.append(
                    reject(
                        "negation_with_open_exception",
                        path,
                        f"The exception '{item.quote[:120]}' stays unmapped, so this negated value would "
                        "also exclude the patients the exception keeps. Remove this leaf and keep the "
                        "criterion as text, or encode the exception.",
                    )
                )
    if EXCEPTION_WORDS.search(criterion.text):
        for path, _ in negated:
            if path not in rejected:
                problems.append(
                    confirm(
                        "exclusion_exception",
                        path,
                        "This exclusion states an exception. A negated value also excludes the patients the "
                        "exception keeps, unless the registry hierarchy keeps them out of it (for example, "
                        "the allowed drugs are not members of the excluded class). Confirm with the words "
                        "that exclude this item, or keep the criterion as text.",
                    )
                )
    return problems


def _pattern_problems(submission: CriterionSubmission, criterion: Criterion) -> list[dict]:
    """Confirm checks: patterns that are usually wrong but can be right."""
    tree = submission.tree
    if tree is None:
        return []
    problems = []
    leaves = list(iter_leaves(tree))
    exclusion = criterion.kind == "exclusion"
    if exclusion and not any(_is_negated(leaf) for _, leaf in leaves):
        problems.append(
            confirm(
                "exclusion_without_negation",
                "/tree",
                "This exclusion has no negated value. Eligible patients must NOT have the "
                "excluded condition.",
            )
        )
    if not exclusion:
        for path, leaf in leaves:
            if _is_negated(leaf):
                problems.append(
                    confirm("inclusion_with_negation", path, "An inclusion criterion with a negated value.")
                )
    for path, group in iter_groups(tree):
        children = group.items
        child_leaves = [child for child in children if isinstance(child, LeafNode)]
        diagnoses = [
            leaf.clinical.oncotree_primary_diagnosis
            for leaf in child_leaves
            if leaf.clinical and leaf.clinical.oncotree_primary_diagnosis
        ]
        if group.type == "or" and any(d.lstrip("!") in SENTINELS for d in diagnoses) and len(diagnoses) > 1:
            problems.append(
                confirm(
                    "sentinel_beside_diagnoses",
                    path,
                    "_SOLID_ or _LIQUID_ in an OR with specific diagnoses admits every solid or "
                    "liquid tumor.",
                )
            )
        all_negated = all(isinstance(child, LeafNode) and _is_negated(child) for child in children)
        if exclusion and group.type == "or" and len(children) > 1 and all_negated:
            problems.append(
                confirm(
                    "exclusion_or_of_negations",
                    path,
                    "OR of negations excludes only patients who have ALL the items (a combination). "
                    "Excluding ANY of the items is AND of negations.",
                )
            )
        if not exclusion and group.type == "or":
            negated = [d for d in diagnoses if d.startswith("!")]
            if negated and len(negated) < len(diagnoses):
                problems.append(
                    confirm(
                        "inclusion_or_mixed_diagnosis",
                        path,
                        "An OR mixing a negated diagnosis with positive ones admits almost everyone.",
                    )
                )
    return problems


def check_criterion(
    submission: CriterionSubmission,
    confirmations: list[Confirmation],
    criterion: Criterion,
    context: RunContext,
    state: AgentState,
) -> tuple[list[dict], list[dict]]:
    """(problems left to fix, accepted confirmations)."""
    problems: list[dict] = []
    source = criterion_source(context, criterion, state)
    if submission.representable == "none" and submission.tree is not None:
        problems.append(reject("tree_with_none", "/tree", "representable is none: remove the tree."))
    if submission.representable != "none" and submission.tree is None:
        problems.append(
            reject("missing_tree", "/tree", f"representable is {submission.representable}: add a tree.")
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
    if submission.context_category == OPTIONAL_SUBSTUDY and submission.tree is not None:
        problems.append(
            reject(
                "tree_for_optional_substudy",
                "/tree",
                "Criteria of an optional sub-study do not decide trial eligibility: use representable "
                "none and no tree.",
            )
        )
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
    if submission.tree is not None:
        for path, leaf in iter_leaves(submission.tree):
            problems.extend(
                _leaf_problems(path, leaf, criterion, source, context, state, submission.representable)
            )
        if isinstance(submission.tree, GroupNode) or submission.tree.kind() is not None:
            problems.extend(_test_problems(submission, state))
    problems.extend(_coverage_problems(submission, criterion))
    problems.extend(_pattern_problems(submission, criterion))
    problems.extend(_exception_problems(submission, criterion))
    # A confirmation quotes the criterion itself (its text, list stem, tables or the protocol's
    # definitions of its abbreviations), not text read elsewhere.
    return apply_confirmations(problems, confirmations, criterion_source(context, criterion, None))
