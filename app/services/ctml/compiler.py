"""Build the CTML document from the accepted agent outputs. Deterministic; no model calls.

Placement of criterion text (additional criteria):
- A criterion scoped to a population (for example "Part 2") always appears in the
  additional criteria of every arm that has that scope, whether or not it is encoded.
- A trial-wide criterion appears in the top-level additional_criteria_requirements when it
  is not fully encoded, unless its category is in the omission policy.
- A criterion without an accepted encoding is never dropped: its text is kept and a review
  item is raised.
Exclusion texts start with "Exclusion Criteria: ". Values are never rewritten.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from app.services.ctml.contracts import Arm, CriterionSubmission, DesignSubmission
from app.services.ctml.document.glossary import Glossary
from app.services.ctml.document.inventory import Criterion
from app.services.ctml.matching import compile_tree, iter_leaves, leaf_payload
from app.services.ctml.registries.oncotree import OncoTreeIndex, words

EXCLUSION_PREFIX = "Exclusion Criteria: "
REGISTRY_FIELDS = {
    "oncotree_primary_diagnosis",
    "hugo_symbol",
    "fusion_partner_hugo_symbol",
    "agent",
    "agent_class",
}


@dataclass
class CriterionResult:
    criterion: Criterion
    status: str  # "accepted" or "failed"
    submission: CriterionSubmission | None = None
    confirmations: list[dict] = field(default_factory=list)
    last_errors: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    # v5 layout: the structure, coverage listing, slot values and repairs behind the encoding.
    details: dict = field(default_factory=dict)

    @property
    def compiled(self) -> dict | None:
        if self.submission is None or self.submission.tree is None:
            return None
        return compile_tree(self.submission.tree)

    @property
    def fully_encoded(self) -> bool:
        return (
            self.status == "accepted"
            and self.submission is not None
            and self.submission.representable == "full"
        )


def residual_text(criterion: Criterion) -> str:
    text = " ".join(criterion.text.split())
    return f"{EXCLUSION_PREFIX}{text}" if criterion.kind == "exclusion" else text


def applies_to(criterion: Criterion, arm: Arm) -> bool:
    return criterion.scope_label is None or criterion.scope_label in arm.scope_labels


def arm_uuid(trial_id: str, arm_code: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{trial_id}:{arm_code}"))


def _unique(items: list) -> list:
    seen: set[str] = set()
    result = []
    for item in items:
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _display_rank(item: dict) -> int:
    """Gold leaf order, for readability only: diagnosis, age, other clinical, genomic,
    therapy exclusions, transplant, required therapy, groups, diagnosis groups last."""
    if "clinical" in item:
        fields = item["clinical"]
        return 0 if "oncotree_primary_diagnosis" in fields else 1 if "age_expression" in fields else 2
    if "genomic" in item:
        return 3
    if "prior_treatment" in item:
        fields = item["prior_treatment"]
        if "transplant_type" in fields:
            return 5
        return 4 if any(str(v).startswith("!") for v in fields.values()) else 6
    return 8 if "oncotree_primary_diagnosis" in json.dumps(item) else 7


# --- implied conditions -------------------------------------------------------------------
# Criteria are encoded one at a time, so an arm's AND can hold a condition that another
# condition of the same arm already guarantees (for example "any prior Medical Therapy" beside
# a required drug class, or a diagnosis beside an OR of its OncoTree descendants). Removing
# such a condition never changes which patients match.


def _therapy_categories(item: dict) -> set[str]:
    """Treatment categories that every patient matching the condition has received (a named,
    not negated drug, class or transplant type)."""
    if "prior_treatment" in item:
        fields = item["prior_treatment"]
        named = [fields[k] for k in ("agent", "agent_class", "transplant_type") if fields.get(k)]
        if named and not any(str(value).startswith("!") for value in named):
            return {str(fields.get("treatment_category"))}
        return set()
    if "and" in item:
        return set().union(*(_therapy_categories(child) for child in item["and"]))
    if "or" in item:
        sets = [_therapy_categories(child) for child in item["or"]]
        return set.intersection(*sets) if sets else set()
    return set()


def _diagnosis_options(item: dict) -> list[list[str]]:
    """Lists of positive diagnoses: a patient matching the condition has one diagnosis of each list."""
    if "clinical" in item:
        value = str(item["clinical"].get("oncotree_primary_diagnosis") or "")
        return [[value]] if value and not value.startswith("!") else []
    if "and" in item:
        return [options for child in item["and"] for options in _diagnosis_options(child)]
    if "or" in item:
        branches = [_diagnosis_options(child) for child in item["or"]]
        if not branches or any(not branch for branch in branches):
            return []
        return [[value for branch in branches for value in branch[0]]]
    return []


def _covers(options: list[str], target: str, oncotree: OncoTreeIndex | None) -> bool:
    """Every option is the target diagnosis or one of its descendants."""
    if target == "_SOLID_" or target == "_LIQUID_":
        if oncotree is None or any(o in ("_SOLID_", "_LIQUID_") for o in options):
            return all(o == target for o in options)
        liquid = [oncotree.is_liquid(o) for o in options]
        return all(value is (target == "_LIQUID_") for value in liquid)
    if oncotree is None:
        return all(o == target for o in options)
    return all(oncotree.is_under(o, target) is True for o in options)


def _excludes(options: list[str], target: str, oncotree: OncoTreeIndex | None) -> bool:
    """No option overlaps the target diagnosis (neither contains the other)."""
    if oncotree is None or any(o.startswith("_") for o in options):
        return False
    return all(
        oncotree.is_under(o, target) is False and oncotree.is_under(target, o) is False for o in options
    )


def _implied_by(item: dict, others: list[dict], oncotree: OncoTreeIndex | None) -> dict | None:
    """The other condition that guarantees this leaf, or None."""
    fields = item.get("prior_treatment")
    if fields is not None and set(fields) == {"treatment_category"}:
        category = str(fields["treatment_category"])
        return next((other for other in others if category in _therapy_categories(other)), None)
    clinical = item.get("clinical")
    if clinical is None or set(clinical) != {"oncotree_primary_diagnosis"}:
        return None
    value = str(clinical["oncotree_primary_diagnosis"])
    negated, target = value.startswith("!"), value.lstrip("!")
    for other in others:
        for options in _diagnosis_options(other):
            if (negated and _excludes(options, target, oncotree)) or (
                not negated and _covers(options, target, oncotree)
            ):
                return other
    return None


def prune_implied(items: list[dict], oncotree: OncoTreeIndex | None) -> tuple[list[dict], list[dict]]:
    """(kept conditions, removed ones with the condition that implies each). One at a time, so
    two conditions never justify each other's removal."""
    kept = list(items)
    removed = []
    for item in list(items):
        others = [other for other in kept if other is not item]
        reason = _implied_by(item, others, oncotree)
        if reason is not None:
            kept.remove(item)
            removed.append({"leaf": item, "implied_by": reason})
    return kept, removed


def contradictions(items: list[dict], oncotree: OncoTreeIndex | None) -> list[tuple[str, str]]:
    """(excluded diagnosis, required diagnoses) pairs of one arm's AND that no patient can meet:
    every diagnosis a condition allows is the excluded one or lies under it."""
    excluded = [
        str(item["clinical"]["oncotree_primary_diagnosis"])[1:]
        for item in items
        if "clinical" in item
        and str(item["clinical"].get("oncotree_primary_diagnosis") or "").startswith("!")
    ]
    found = []
    for target in excluded:
        for item in items:
            for options in _diagnosis_options(item):
                if options and all(
                    option == target or (oncotree is not None and oncotree.is_under(option, target) is True)
                    for option in options
                ):
                    found.append((target, " or ".join(options)))
    return found


def names_diagnosis(item: dict) -> bool:
    """Whether a rule requires a diagnosis somewhere (a positive OncoTree value or _SOLID_/_LIQUID_)."""
    if "clinical" in item:
        value = str(item["clinical"].get("oncotree_primary_diagnosis") or "")
        return bool(value) and not value.startswith("!")
    return any(names_diagnosis(child) for key in ("and", "or") for child in item.get(key, []))


def arm_match(trees: list[dict], oncotree: OncoTreeIndex | None = None) -> tuple[list[dict], list[dict]]:
    """(the arm's match list, removed implied conditions): one AND of every applicable
    criterion tree, flattened, without conditions another one guarantees."""
    items: list[dict] = []
    for tree in trees:
        items.extend(tree["and"] if list(tree) == ["and"] else [tree])
    items, removed = prune_implied(_unique(items), oncotree)
    items = sorted(items, key=_display_rank)
    return ([{"and": items}] if items else []), removed


def added_words(label: str, source_concept: str, glossary: Glossary) -> list[str]:
    """Words of a registry label that the source phrase (or the protocol's definition of its
    abbreviations) does not contain. Reported for review, never changed."""
    allowed = words(source_concept)
    for definition in glossary.for_text(source_concept):
        allowed |= words(definition.definition)
    return sorted(words(label.lstrip("!")) - allowed)


def criterion_evidence(result: CriterionResult, placement: str, glossary: Glossary) -> dict:
    criterion = result.criterion
    entry = {
        "criterion_id": criterion.criterion_id,
        "kind": criterion.kind,
        "scope_label": criterion.scope_label,
        "section": criterion.section_title,
        "pages": criterion.pages,
        "line_ids": criterion.line_ids,
        "text": " ".join(criterion.text.split()),
        "status": result.status,
        "text_placement": placement,
        "agent": result.summary,
    }
    if result.submission is not None:
        submission = result.submission
        leaves = []
        if submission.tree is not None:
            for path, leaf in iter_leaves(submission.tree):
                payload = leaf_payload(leaf) if leaf.kind() else {}
                kind = next(iter(payload), "")
                extra: dict[str, list[str]] = {}
                for name, value in payload.get(kind, {}).items():
                    if name in REGISTRY_FIELDS and not str(value).lstrip("!").startswith("_"):
                        found = added_words(str(value), leaf.source_concept, glossary)
                        if found:
                            extra[name] = found
                leaves.append(
                    {
                        "path": path,
                        "leaf": payload,
                        "source_concept": leaf.source_concept,
                        "line_ids": leaf.line_ids,
                        "lookup_ids": leaf.lookup_ids,
                        **({"label_adds_words": extra} if extra else {}),
                    }
                )
        entry.update(
            {
                "representable": submission.representable,
                "context_category": submission.context_category,
                "compiled": result.compiled,
                "leaves": leaves,
                "unmapped_items": [item.model_dump() for item in submission.unmapped_items],
                "review_notes": submission.review_notes,
                "confirmations": result.confirmations,
            }
        )
    else:
        entry["last_errors"] = result.last_errors
    if result.details:
        entry["workflow"] = result.details
    return entry


def compile_ctml(
    fields: dict,
    design: DesignSubmission,
    results: list[CriterionResult],
    omit_categories: tuple[str, ...],
    glossary: Glossary,
    oncotree: OncoTreeIndex | None = None,
) -> tuple[dict, dict, list[str]]:
    """(CTML document, evidence, review items)."""
    review: list[str] = []
    trial_id = fields.get("trial_id", "")
    omit = {category.casefold() for category in omit_categories}

    arms_out = []
    implied: list[dict] = []
    for arm in design.arms:
        applicable = [r for r in results if applies_to(r.criterion, arm)]
        trees = [r.compiled for r in applicable if r.status == "accepted" and r.compiled]
        match, removed = arm_match(trees, oncotree)
        if removed:
            implied.append({"arm_code": arm.arm_code, "removed": removed})
        if not match:
            review.append(f"arm_without_match:{arm.arm_code}")
        for excluded, required in contradictions(match[0]["and"] if match else [], oncotree):
            review.append(
                f"arm_contradiction:{arm.arm_code}: the rule requires {required} and excludes {excluded}, "
                "which covers it; no patient can match"
            )
        if match and not names_diagnosis(match[0]):
            review.append(
                f"arm_without_diagnosis:{arm.arm_code}: the arm's rule requires no diagnosis; check that the "
                "criteria that define its population are encoded"
            )
        texts = [residual_text(r.criterion) for r in applicable if r.criterion.scope_label]
        arms_out.append(
            {
                "arm_code": arm.arm_code,
                "arm_suspended": "N",
                "arm_description": arm.arm_description,
                "uuid": arm_uuid(trial_id, arm.arm_code),
                "dose_level": [
                    {"level_code": level.level_code, "level_description": level.level_description}
                    for level in arm.dose_levels
                ],
                "match": match,
                "arm_additional_criteria_not_captured": [
                    {"additional_criteria_requirement_name": text} for text in _unique(texts)
                ],
            }
        )

    trial_texts: list[str] = []
    evidence_criteria = []
    for result in results:
        criterion = result.criterion
        category = result.submission.context_category if result.submission else ""
        if criterion.scope_label:
            arms = [a.arm_code for a in design.arms if applies_to(criterion, a)]
            placement = "arms: " + "; ".join(arms) if arms else "none (no arm has this scope)"
            if not arms:
                review.append(f"scope_without_arm:{criterion.criterion_id}:{criterion.scope_label}")
        elif result.status != "accepted":
            trial_texts.append(residual_text(criterion))
            placement = "trial (no accepted encoding)"
        elif result.fully_encoded:
            placement = "none (fully encoded)"
        elif category.casefold() in omit:
            placement = f"omitted by policy ({category})"
        else:
            trial_texts.append(residual_text(criterion))
            placement = "trial"
        if result.status != "accepted":
            review.append(f"criterion_not_encoded:{criterion.criterion_id}")
        for confirmation in result.confirmations:
            review.append(f"confirmed_pattern:{criterion.criterion_id}:{confirmation['code']}")
        if result.submission:
            for note in result.submission.review_notes:
                review.append(f"agent_note:{criterion.criterion_id}:{note[:200]}")
        evidence_criteria.append(criterion_evidence(result, placement, glossary))

    document: dict = {
        "trial_id": trial_id,
        "long_title": fields.get("long_title", ""),
        "short_title": fields.get("short_title", ""),
        "protocol_no": fields.get("protocol_no", ""),
    }
    for optional in ("protocol_version_no", "protocol_version_date"):
        if fields.get(optional):
            document[optional] = fields[optional]
    document.update(
        {
            "phase": fields.get("phase", ""),
            "nct_purpose": fields.get("nct_purpose", ""),
            "principal_investigator": "",
            "drug_list": {"drug": [{}]},
            "management_group_list": {"management_group": [{}]},
            "site_list": {"site": [{}]},
            "sponsor_list": {
                "sponsor": [{"sponsor_name": fields.get("sponsor_name", ""), "is_principal_sponsor": "Y"}]
            },
            "staff_list": {"protocol_staff": []},
            "treatment_list": {"step": [{"arm": arms_out}]},
            "additional_criteria_requirements": _unique(trial_texts),
        }
    )
    evidence = {
        "design": design.model_dump(),
        "criteria": evidence_criteria,
        "implied_conditions_removed": implied,
    }
    return document, evidence, review
