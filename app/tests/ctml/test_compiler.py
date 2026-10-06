"""Compiler placement rules, metadata merge and the QA gate."""

from __future__ import annotations

from datetime import date

from app.services.ctml.compiler import (
    CriterionResult,
    arm_match,
    arm_uuid,
    compile_ctml,
    prune_implied,
    residual_text,
)
from app.services.ctml.contracts import CriterionSubmission, DesignSubmission, MetadataSubmission
from app.services.ctml.metadata_rules import ctml_date, first_sentence, registry_phase, resolve_metadata
from app.services.ctml.pipeline import registry_citations
from app.services.ctml.qa import build_qa, policy_errors
from app.services.ctml.schema.loader import validation_errors

OMIT = ("consent", "laboratory value")


def design(context) -> DesignSubmission:
    line = context.document.lines[0].line_id
    level = {"level_code": "drugex", "level_description": "100 mg orally once daily", "line_ids": [line]}
    return DesignSubmission.model_validate(
        {
            "design_type": "multi_part",
            "design_quote": "two-part",
            "drugs": [{"name": "drugex", "role": "investigational", "line_ids": [line]}],
            "arms": [
                {
                    "arm_code": "Part 1: A",
                    "arm_description": "A.",
                    "scope_labels": ["Part 1"],
                    "dose_levels": [level],
                    "line_ids": [line],
                },
                {
                    "arm_code": "Part 2: B",
                    "arm_description": "B.",
                    "scope_labels": ["Part 2"],
                    "dose_levels": [level],
                    "line_ids": [line],
                },
            ],
        }
    )


def accepted(
    context, criterion_id: str, representable: str, category: str, tree: dict | None = None
) -> CriterionResult:
    criterion = context.inventory.by_id(criterion_id)
    data = {"representable": representable, "tree": tree, "context_category": category}
    if representable == "partial":
        data["unmapped_items"] = [{"quote": "x", "reason": "y"}]
    return CriterionResult(criterion, "accepted", CriterionSubmission.model_validate(data))


def age_tree(context) -> dict:
    line = context.inventory.by_id("INC-1").line_ids[0]
    return {
        "type": "leaf",
        "clinical": {"age_expression": ">=18"},
        "source_concept": "18 years",
        "line_ids": [line],
    }


FIELDS = {
    "trial_id": "NCT09999999",
    "long_title": "Title",
    "short_title": "Title",
    "protocol_no": "EXP-123",
    "phase": "II",
    "nct_purpose": "Purpose.",
    "sponsor_name": "Sponsor",
}


def test_placement_rules(context):
    results = [
        accepted(context, "INC-1", "full", "disease context", age_tree(context)),
        accepted(context, "INC-2", "none", "disease context"),  # scoped to Part 1
        accepted(context, "INC-4", "none", "performance status"),
        accepted(context, "EXC-3", "none", "laboratory value"),  # omitted by policy
        CriterionResult(context.inventory.by_id("EXC-2"), "failed", last_errors=[{"code": "x"}]),
    ]
    ctml, evidence, review = compile_ctml(FIELDS, design(context), results, OMIT, context.glossary)
    part1, part2 = ctml["treatment_list"]["step"][0]["arm"]
    assert part1["match"] == [{"and": [{"clinical": {"age_expression": ">=18"}}]}]
    assert part2["match"] == part1["match"]
    assert [
        t["additional_criteria_requirement_name"] for t in part1["arm_additional_criteria_not_captured"]
    ] == ["Part 1: Histologically confirmed advanced solid tumor."]
    assert part2["arm_additional_criteria_not_captured"] == []
    assert ctml["additional_criteria_requirements"] == [
        "ECOG performance status of 0 or 1.",
        "Exclusion Criteria: Prior allogeneic stem cell transplant.",
    ]
    assert "criterion_not_encoded:EXC-2" in review
    placements = {c["criterion_id"]: c["text_placement"] for c in evidence["criteria"]}
    assert placements["EXC-3"].startswith("omitted by policy")
    assert placements["INC-1"] == "none (fully encoded)"
    assert part1["uuid"] == arm_uuid("NCT09999999", "Part 1: A")
    assert validation_errors(ctml) == [] and policy_errors(ctml) == []


def test_residual_text_marks_exclusions(context):
    assert (
        residual_text(context.inventory.by_id("EXC-3")) == "Exclusion Criteria: Known active CNS metastases."
    )
    assert residual_text(context.inventory.by_id("INC-4")) == "ECOG performance status of 0 or 1."


def test_arm_without_match_is_blocking(context):
    results = [accepted(context, "INC-4", "none", "performance status")]
    ctml, evidence, review = compile_ctml(FIELDS, design(context), results, OMIT, context.glossary)
    qa = build_qa(ctml, evidence, review, {"pdf": "x.pdf"})
    assert qa["status"] == "needs_review" and qa["blocked"]
    assert any(item.startswith("arm_without_match:") for item in qa["blocking"])


def test_policy_errors_find_order_shells_and_nulls():
    document = {
        "long_title": "x",
        "trial_id": "y",
        "drug_list": {"drug": []},
        "status": "Recruiting",
        "phase": None,
    }
    problems = policy_errors(document)
    assert "/: top-level keys out of order" in problems
    assert any("drug_list" in p for p in problems)
    assert any("status" in p for p in problems)
    assert any("null value" in p for p in problems)


# --- metadata merge ---------------------------------------------------------------------


def sourced(value: str) -> dict:
    return {"value": value, "line_ids": ["L1"]}


def test_protocol_first_registry_fills_gaps_and_conflicts_are_reported():
    submission = MetadataSubmission.model_validate(
        {
            "long_title": sourced("A Phase 2 Study of Drugex"),
            "protocol_no": sourced("EXP-123"),
            "phase": sourced("Phase 1b/2"),
            "sponsor_name": sourced("Example Pharma Inc."),
            "protocol_version_date": sourced("12 January 2024"),
        }
    )
    registry = {
        "nct_id": "NCT09999999",
        "long_title": "A Completely Different Title About Something Else",
        "short_title": "Drugex study",
        "protocol_no": "EXP-123",
        "phases": ["PHASE2"],
        "sponsor_name": "Example Pharma Inc.",
        "brief_summary": "First sentence. Second sentence.",
    }
    fields, log, review = resolve_metadata(submission, registry, "NCT09999999")
    assert fields["phase"] == "I"
    assert fields["short_title"] == "Drugex study" and "metadata_from_registry:short_title" in review
    assert fields["nct_purpose"] == "First sentence." and "metadata_from_registry:nct_purpose" in review
    assert fields["protocol_version_date"] == "2024-01-12T05:00:00.000Z"  # winter: UTC-5
    assert {"registry_conflict:long_title", "registry_conflict:phase"} <= set(review)
    assert next(entry for entry in log if entry["field"] == "long_title")["source"] == "protocol"


def test_missing_metadata_is_reported():
    fields, _, review = resolve_metadata(None, None, None)
    assert "metadata_missing:trial_id" in review and "metadata_missing:protocol_no" in review
    assert "protocol_version_no" not in fields


def test_metadata_value_helpers():
    assert ctml_date(date(2021, 6, 28)) == "2021-06-28T04:00:00.000Z"
    assert registry_phase(["PHASE2", "PHASE3"]) == "II"
    assert registry_phase(["NA"]) is None
    assert first_sentence("One. Two.") == "One."


# --- pipeline review signals -------------------------------------------------------------


def test_low_confidence_pages_and_figure_citations(context):
    from app.services.ctml.pipeline import figure_citations, low_confidence_pages

    result = {
        "pages": [
            {"pageNumber": 1, "words": [{"confidence": 0.99}] * 19 + [{"confidence": 0.5}]},
            {"pageNumber": 2, "words": [{"confidence": 0.99}] * 8 + [{"confidence": 0.3}] * 2},
        ]
    }
    assert low_confidence_pages(result, None) == [
        "ocr_low_confidence: pages 2 have many uncertain words; check them"
    ]
    assert low_confidence_pages(result, (1, 1)) == []
    assert figure_citations(context.document, design(context)) == []


# --- implied conditions -------------------------------------------------------------------


def _dx(value: str) -> dict:
    return {"clinical": {"oncotree_primary_diagnosis": value}}


def _therapy(**fields) -> dict:
    return {"prior_treatment": {"treatment_category": "Medical Therapy", **fields}}


def test_conditions_implied_by_another_condition_are_removed(context):
    any_therapy = {"prior_treatment": {"treatment_category": "Medical Therapy"}}
    required = {"or": [_therapy(agent_class="PD1 Inhibitor"), _therapy(agent_class="PD-L1 Inhibitor")]}
    kept, removed = prune_implied([any_therapy, required], context.oncotree)
    assert kept == [required] and removed[0]["leaf"] == any_therapy
    # An excluded class does not guarantee any prior therapy.
    kept, removed = prune_implied([any_therapy, _therapy(agent_class="!PD1 Inhibitor")], context.oncotree)
    assert any_therapy in kept and removed == []


def test_diagnoses_implied_by_more_specific_ones_are_removed(context):
    subtypes = {"or": [_dx("Acral Melanoma"), _dx("Melanoma")]}
    kept, removed = prune_implied(
        [_dx("Skin"), _dx("!Non-Small Cell Lung Cancer"), subtypes], context.oncotree
    )
    assert kept == [subtypes] and len(removed) == 2
    # A negated subtype of the required diagnosis does real work and stays.
    kept, _ = prune_implied([_dx("Melanoma"), _dx("!Acral Melanoma")], context.oncotree)
    assert kept == [_dx("Melanoma"), _dx("!Acral Melanoma")]
    # Without OncoTree nothing is removed except exact matches.
    kept, removed = prune_implied([_dx("Skin"), subtypes], None)
    assert removed == []


def test_arm_match_reports_removed_conditions(context):
    match, removed = arm_match(
        [
            {"and": [_dx("Skin"), _dx("Melanoma")]},
            {"prior_treatment": {"treatment_category": "Medical Therapy"}},
        ],
        context.oncotree,
    )
    assert match == [
        {"and": [_dx("Melanoma"), {"prior_treatment": {"treatment_category": "Medical Therapy"}}]}
    ]
    assert removed == [{"leaf": _dx("Skin"), "implied_by": _dx("Melanoma")}]


def test_registry_doses_are_listed_for_review(context):
    sub = design(context)
    sub.arms[0].dose_levels[0].line_ids = ["R2"]
    items = registry_citations(sub, "ClinicalTrials.gov NCT09999999")
    assert items == [
        "registry_dose:Part 1: A:drugex: taken from ClinicalTrials.gov NCT09999999 (R2); "
        "the protocol does not state it"
    ]
