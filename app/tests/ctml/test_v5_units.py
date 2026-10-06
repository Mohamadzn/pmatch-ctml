"""v5 layout: the merge of structure and slot values, and the submit checks of the eligibility
logic, domain, coverage and resolver agents. Synthetic text only."""

from __future__ import annotations

from app.services.ctml.checks.coverage import check_coverage
from app.services.ctml.checks.eligibility import check_structure
from app.services.ctml.checks.resolver import check_review
from app.services.ctml.checks.slots import check_fills
from app.services.ctml.config import Settings
from app.services.ctml.contracts import (
    ClinicalFill,
    CoverageItem,
    CoverageSubmission,
    EligibilitySubmission,
    GenomicFill,
    PriorTreatmentFill,
    PriorTreatmentSubmission,
    ResolverSubmission,
)
from app.services.ctml.matching import compile_tree
from app.services.ctml.merge import coverage_omissions, evaluate_logic, merge_criterion
from app.services.ctml.runtime import AgentState, RunContext
from app.tests.ctml.test_checks import EXCEPTION_PAGES, context_with, line_of, lookup

CLASS_CANDIDATE = {"name": "Class A", "kind": "class", "parents": []}


def slot(slot_id: str, domain: str, concept: str, line: str = "L1", negated: bool = False) -> dict:
    return {
        "type": "slot",
        "slot_id": slot_id,
        "domain": domain,
        "concept": concept,
        "line_ids": [line],
        "negated": negated,
    }


def structure(logic: dict | None, representable: str = "full", examples=(), unmapped=(), category="other"):
    return EligibilitySubmission.model_validate(
        {
            "representable": representable,
            "logic": logic,
            "unmapped_items": list(unmapped),
            "context_category": category,
            "examples": list(examples),
        }
    )


def mapped(model, slot_id: str, field: str, *values: dict):
    return model.model_validate({"slot_id": slot_id, "status": "mapped", field: list(values)})


def therapy(slot_id: str, **fields) -> PriorTreatmentFill:
    return mapped(
        PriorTreatmentFill, slot_id, "prior_treatment", {"treatment_category": "Medical Therapy", **fields}
    )


def diagnosis(slot_id: str, *names: str) -> ClinicalFill:
    return mapped(
        ClinicalFill, slot_id, "clinical", *({"oncotree_primary_diagnosis": name} for name in names)
    )


def cannot(slot_id: str, model=PriorTreatmentFill):
    return model.model_validate(
        {"slot_id": slot_id, "status": "cannot_map", "reason": "no registry value fits"}
    )


# --- merge ----------------------------------------------------------------------------------


def test_an_and_keeps_the_conditions_that_can_be_encoded():
    logic = {
        "type": "and",
        "items": [slot("S1", "prior_treatment", "drug A"), slot("S2", "prior_treatment", "drug B")],
    }
    merged = merge_criterion(structure(logic), {"S1": therapy("S1", agent="Drug A"), "S2": cannot("S2")})
    assert compile_tree(merged.submission.tree) == {
        "prior_treatment": {"treatment_category": "Medical Therapy", "agent": "Drug A"}
    }
    assert merged.submission.representable == "partial"
    assert [item.quote for item in merged.submission.unmapped_items] == ["drug B"]


def test_an_or_keeps_the_alternatives_that_can_be_encoded():
    """C26 (UHN convention): the alternatives CTML can hold are kept; the others stay as text."""
    cohorts = {
        "type": "or",
        "items": [
            slot("S1", "clinical", "disease A"),
            {
                "type": "and",
                "items": [slot("S2", "clinical", "disease B"), slot("S3", "genomic", "marker C")],
            },
            slot("S4", "clinical", "tumor with marker D of any histology"),
        ],
    }
    fills = {
        "S1": diagnosis("S1", "Disease A"),
        "S2": diagnosis("S2", "Disease B"),
        "S3": mapped(GenomicFill, "S3", "genomic", {"hugo_symbol": "GENE3", "variant_category": "Mutation"}),
        "S4": cannot("S4", ClinicalFill),
    }
    merged = merge_criterion(structure(cohorts), fills)
    assert compile_tree(merged.submission.tree) == {
        "or": [
            {"clinical": {"oncotree_primary_diagnosis": "Disease A"}},
            {
                "and": [
                    {"clinical": {"oncotree_primary_diagnosis": "Disease B"}},
                    {"genomic": {"hugo_symbol": "GENE3", "variant_category": "Mutation"}},
                ]
            },
        ]
    }
    assert merged.submission.representable == "partial"
    assert [(item["slot_id"], item["alternative"]) for item in merged.dropped] == [("S4", True)]
    assert "tumor with marker D of any histology" in [item.quote for item in merged.submission.unmapped_items]


def test_an_excluded_combination_is_never_split():
    """NOT (X with Y): dropping Y would exclude every patient with X, more than the protocol does."""
    combination = {
        "type": "or",
        "items": [
            slot("S1", "prior_treatment", "drug A", negated=True),
            slot("S2", "prior_treatment", "with a severe reaction", negated=True),
        ],
    }
    merged = merge_criterion(
        structure(combination), {"S1": therapy("S1", agent="Drug A"), "S2": cannot("S2")}
    )
    assert merged.submission.tree is None and merged.submission.representable == "none"
    assert {item["slot_id"] for item in merged.dropped} == {"S1", "S2"}
    assert not any(item["alternative"] for item in merged.dropped)


def test_a_slot_can_carry_several_registry_values():
    """Words no single registry entry covers: any value satisfies the slot; NOT excludes each."""
    sites = slot("S1", "clinical", "carcinoma of organ A or organ B")
    targets = slot("S2", "prior_treatment", "an inhibitor of X or Y", negated=True)
    logic = {"type": "and", "items": [sites, targets]}
    fills = {
        "S1": diagnosis("S1", "Carcinoma A", "Carcinoma B"),
        "S2": mapped(
            PriorTreatmentFill,
            "S2",
            "prior_treatment",
            {"treatment_category": "Medical Therapy", "agent_class": "X Inhibitor"},
            {"treatment_category": "Medical Therapy", "agent_class": "Y Inhibitor"},
        ),
    }
    merged = merge_criterion(structure(logic), fills)
    assert merged.dropped == []
    assert compile_tree(merged.submission.tree) == {
        "and": [
            {
                "or": [
                    {"clinical": {"oncotree_primary_diagnosis": "Carcinoma A"}},
                    {"clinical": {"oncotree_primary_diagnosis": "Carcinoma B"}},
                ]
            },
            {"prior_treatment": {"treatment_category": "Medical Therapy", "agent_class": "!X Inhibitor"}},
            {"prior_treatment": {"treatment_category": "Medical Therapy", "agent_class": "!Y Inhibitor"}},
        ]
    }


def test_negation_comes_from_the_structure():
    logic = {
        "type": "and",
        "items": [
            slot("S1", "prior_treatment", "class A", negated=True),
            slot("S2", "clinical", "younger than 18 years", negated=True),
            slot("S3", "genomic", "GENE1 mutation", negated=True),
            slot("S4", "genomic", "GENE2 amplification", negated=True),
            slot("S5", "prior_treatment", "allogeneic transplant", negated=True),
        ],
    }
    fills = {
        "S1": therapy("S1", agent_class="Class A"),
        "S2": mapped(ClinicalFill, "S2", "clinical", {"age_expression": "<18"}),
        "S3": mapped(GenomicFill, "S3", "genomic", {"hugo_symbol": "GENE1", "variant_category": "Mutation"}),
        "S4": mapped(
            GenomicFill,
            "S4",
            "genomic",
            {"hugo_symbol": "GENE2", "variant_category": "CNV", "cnv_call": "High level amplification"},
        ),
        "S5": mapped(
            PriorTreatmentFill,
            "S5",
            "prior_treatment",
            {"treatment_category": "Stem Cell Transplant", "transplant_type": "Allogeneic"},
        ),
    }
    merged = merge_criterion(structure(logic), fills)
    assert merged.dropped == []
    assert compile_tree(merged.submission.tree) == {
        "and": [
            {"prior_treatment": {"treatment_category": "Medical Therapy", "agent_class": "!Class A"}},
            {"clinical": {"age_expression": ">=18"}},
            {"genomic": {"hugo_symbol": "GENE1", "variant_category": "!Mutation"}},
            {
                "genomic": {
                    "hugo_symbol": "GENE2",
                    "variant_category": "CNV",
                    "cnv_call": "!High level amplification",
                }
            },
            {
                "prior_treatment": {
                    "treatment_category": "Stem Cell Transplant",
                    "transplant_type": "!Allogeneic",
                }
            },
        ]
    }


def test_conditions_ctml_cannot_exclude_are_kept_as_text():
    logic = {
        "type": "and",
        "items": [
            slot("S1", "clinical", "solid tumors", negated=True),
            slot("S2", "genomic", "GENE1 fusion", negated=True),
            slot("S3", "prior_treatment", "any prior therapy", negated=True),
        ],
    }
    fills = {
        "S1": diagnosis("S1", "_SOLID_"),
        "S2": mapped(
            GenomicFill, "S2", "genomic", {"hugo_symbol": "GENE1", "variant_category": "Structural Variation"}
        ),
        "S3": mapped(PriorTreatmentFill, "S3", "prior_treatment", {"treatment_category": "Medical Therapy"}),
    }
    merged = merge_criterion(structure(logic), fills)
    assert merged.submission.tree is None and merged.submission.representable == "none"
    assert len(merged.dropped) == 3


def test_a_slot_without_an_accepted_value_is_kept_as_text():
    merged = merge_criterion(structure(slot("S1", "prior_treatment", "drug A")), {})
    assert merged.submission.tree is None
    assert "no accepted value" in merged.dropped[0]["reason"]


def test_examples_are_evaluated_on_the_logic():
    excluded_any = {
        "type": "and",
        "items": [slot("S1", "clinical", "a", negated=True), slot("S2", "clinical", "b", negated=True)],
    }
    assert evaluate_logic(structure(excluded_any).logic, set()) is True
    assert evaluate_logic(structure(excluded_any).logic, {"S1"}) is False


# --- coverage comparison --------------------------------------------------------------------


def test_coverage_omissions_find_requirements_no_slot_or_text_states():
    source = "Histologically confirmed advanced solid tumor, measurable disease per RECIST."
    logic = slot("S1", "clinical", "advanced solid tumor")
    items = [
        CoverageItem(quote="Histologically confirmed", kind="requirement"),
        CoverageItem(quote="advanced solid tumor", kind="requirement"),
        CoverageItem(quote="per RECIST", kind="note"),
    ]
    missing = coverage_omissions(source, structure(logic), items)
    assert [item.quote for item in missing] == ["Histologically confirmed"]
    kept = structure(
        logic,
        "partial",
        unmapped=[
            {"quote": "Histologically confirmed", "reason": "how it was diagnosed", "line_ids": ["L1"]}
        ],
    )
    assert coverage_omissions(source, kept, items) == []
    assert coverage_omissions(source, structure(None, "none"), items) == []


# --- eligibility logic checks ---------------------------------------------------------------


def run_structure(
    context: RunContext, criterion_id: str, submission: EligibilitySubmission, confirmations=()
):
    state = AgentState(owner=f"{criterion_id}/eligibility")
    criterion = context.inventory.by_id(criterion_id)
    state.seen_line_ids.update(criterion.line_ids)
    problems, _ = check_structure(submission, list(confirmations), criterion, context, state)
    return {p["code"] for p in problems}


EXCLUDED = [
    {"name": "free", "expect": "eligible", "has": []},
    {"name": "exposed", "expect": "not_eligible", "has": ["S1"]},
]


def test_a_negated_exclusion_slot_with_agreeing_examples_is_accepted(context):
    line = line_of(context, "EXC-1")
    logic = slot("S1", "prior_treatment", "anti-PD-1 antibody", line, negated=True)
    assert run_structure(context, "EXC-1", structure(logic, examples=EXCLUDED)) == set()


def test_examples_that_disagree_with_the_logic_are_rejected(context):
    line = line_of(context, "EXC-1")
    logic = slot("S1", "prior_treatment", "anti-PD-1 antibody", line, negated=True)
    wrong = [{**EXCLUDED[0], "has": ["S1"]}, {**EXCLUDED[1], "has": []}]
    codes = run_structure(context, "EXC-1", structure(logic, examples=wrong))
    assert "example_disagreement" in codes and "examples_incomplete" in codes


def test_slot_words_and_polarity_are_checked(context):
    line = line_of(context, "EXC-1")
    paraphrase = slot("S1", "prior_treatment", "prior immunotherapy", line, negated=True)
    assert "concept_not_in_source" in run_structure(
        context, "EXC-1", structure(paraphrase, examples=EXCLUDED)
    )
    positive = slot("S1", "prior_treatment", "anti-PD-1 antibody", line)
    has_it = [
        {"name": "a", "expect": "eligible", "has": ["S1"]},
        {"name": "b", "expect": "not_eligible", "has": []},
    ]
    assert "exclusion_without_negation" in run_structure(
        context, "EXC-1", structure(positive, examples=has_it)
    )
    assert "logic_with_none" in run_structure(context, "EXC-1", structure(positive, "none"))


def test_an_or_of_negated_exclusion_slots_needs_confirmation(context):
    line = line_of(context, "EXC-1")
    logic = {
        "type": "or",
        "items": [
            slot("S1", "prior_treatment", "anti-PD-1 antibody", line, negated=True),
            slot("S2", "prior_treatment", "Prior treatment", line, negated=True),
        ],
    }
    examples = [
        {"name": "free", "expect": "eligible", "has": []},
        {"name": "both", "expect": "not_eligible", "has": ["S1", "S2"]},
    ]
    assert "exclusion_or_of_negations" in run_structure(context, "EXC-1", structure(logic, examples=examples))


def test_a_negated_slot_with_an_unmapped_exception_is_rejected():
    context = context_with(EXCEPTION_PAGES)
    line = line_of(context, "EXC-1")
    logic = slot("S1", "prior_treatment", "Prior drugex therapy", line, negated=True)
    exception = {
        "quote": "unless it was given in the adjuvant setting",
        "reason": "setting",
        "line_ids": [line],
    }
    sub = structure(logic, "partial", examples=EXCLUDED, unmapped=[exception])
    assert "negation_with_open_exception" in run_structure(context, "EXC-1", sub)


# --- domain agent checks ----------------------------------------------------------------------


def run_fills(context: RunContext, criterion_id: str, logic: dict, fills: list[dict]):
    criterion = context.inventory.by_id(criterion_id)
    state = AgentState(owner=f"{criterion_id}/prior_therapy")
    state.seen_line_ids.update(criterion.line_ids)
    submission = PriorTreatmentSubmission.model_validate({"fills": fills})
    problems, _ = check_fills(
        "prior_treatment",
        submission.fills,
        [],
        criterion,
        structure(logic, examples=EXCLUDED),
        context,
        state,
    )
    return {p["code"] for p in problems}


def test_fills_give_positive_values_from_the_agents_own_lookups(context):
    line = line_of(context, "EXC-1")
    logic = slot("S1", "prior_treatment", "anti-PD-1 antibody", line, negated=True)
    own = lookup(context, "EXC-1/prior_therapy", [CLASS_CANDIDATE])
    value = {"treatment_category": "Medical Therapy", "agent_class": "Class A"}
    fill = {"slot_id": "S1", "status": "mapped", "prior_treatment": [value], "lookup_ids": [own]}
    assert run_fills(context, "EXC-1", logic, [fill]) == set()
    negated = {**fill, "prior_treatment": [{**value, "agent_class": "!Class A"}]}
    assert "negation_in_value" in run_fills(context, "EXC-1", logic, [negated])
    other = lookup(context, "EXC-1/eligibility", [CLASS_CANDIDATE])
    assert "unknown_lookup_id" in run_fills(context, "EXC-1", logic, [{**fill, "lookup_ids": [other]}])


def test_every_slot_needs_one_fill_and_a_reason_to_stay_unmapped(context):
    line = line_of(context, "EXC-1")
    logic = slot("S1", "prior_treatment", "anti-PD-1 antibody", line, negated=True)
    assert "slot_without_fill" in run_fills(
        context, "EXC-1", logic, [{"slot_id": "S9", "status": "cannot_map"}]
    )
    assert "cannot_map_without_reason" in run_fills(
        context, "EXC-1", logic, [{"slot_id": "S1", "status": "cannot_map"}]
    )
    category_only = {
        "slot_id": "S1",
        "status": "mapped",
        "prior_treatment": [{"treatment_category": "Medical Therapy"}],
    }
    assert "cannot_exclude" in run_fills(context, "EXC-1", logic, [category_only])
    assert "mapped_without_value" in run_fills(
        context, "EXC-1", logic, [{"slot_id": "S1", "status": "mapped", "prior_treatment": []}]
    )


def test_every_value_of_a_fill_is_checked(context):
    line = line_of(context, "EXC-1")
    logic = slot("S1", "prior_treatment", "anti-PD-1 antibody", line, negated=True)
    own = lookup(context, "EXC-1/prior_therapy", [CLASS_CANDIDATE, {"name": "Class B", "kind": "class"}])
    values = [
        {"treatment_category": "Medical Therapy", "agent_class": "Class A"},
        {"treatment_category": "Medical Therapy", "agent_class": "Class B"},
    ]
    fill = {"slot_id": "S1", "status": "mapped", "prior_treatment": values, "lookup_ids": [own]}
    assert run_fills(context, "EXC-1", logic, [fill]) == set()
    invented = [*values, {"treatment_category": "Medical Therapy", "agent_class": "Class Z"}]
    assert "value_not_from_lookup" in run_fills(
        context, "EXC-1", logic, [{**fill, "prior_treatment": invented}]
    )


# --- coverage and resolver checks -----------------------------------------------------------


def test_coverage_items_quote_the_criterion(context):
    criterion = context.inventory.by_id("EXC-1")
    ok = CoverageSubmission.model_validate(
        {"items": [{"quote": "anti-PD-1 antibody", "kind": "requirement"}]}
    )
    assert check_coverage(ok, criterion, context) == ([], [])
    wrong = CoverageSubmission.model_validate({"items": [{"quote": "prior immunotherapy", "kind": "note"}]})
    codes = {p["code"] for p in check_coverage(wrong, criterion, context)[0]}
    assert codes == {"quote_not_in_criterion", "no_requirement"}


def test_resolver_issues_quote_the_cited_lines(context):
    line = line_of(context, "EXC-1")
    issue = {
        "target": "EXC-1",
        "action": "repair",
        "problem": "The exclusion is encoded as a requirement.",
        "quotes": ["anti-PD-1 antibody"],
        "line_ids": [line],
    }
    ids = {c.criterion_id for c in context.inventory.criteria}
    assert check_review(ResolverSubmission.model_validate({"issues": [issue]}), context, ids) == ([], [])
    wrong = {**issue, "target": "EXC-9", "quotes": ["anti-PD-L1 antibody"]}
    codes = {
        p["code"]
        for p in check_review(ResolverSubmission.model_validate({"issues": [wrong]}), context, ids)[0]
    }
    assert codes == {"unknown_target", "quote_not_in_cited_lines"}


def test_layout_settings_from_the_environment(monkeypatch):
    assert (Settings.from_env().agent_layout, Settings.from_env().resolver) == ("v5", True)
    monkeypatch.setenv("PMATCH_AGENT_LAYOUT", "Compact")
    monkeypatch.setenv("PMATCH_RESOLVER", "off")
    monkeypatch.setenv("PMATCH_MAX_REPAIR_ROUNDS", "2")
    settings = Settings.from_env()
    assert (settings.agent_layout, settings.resolver, settings.max_repair_rounds) == ("compact", False, 2)


def test_quotes_can_run_across_table_cells():
    from app.services.ctml.text import contains_phrase

    assert contains_phrase(
        "Absolute neutrophil count (ANC) | ≥1.5 X 109/L", "Absolute neutrophil count (ANC) ≥1.5"
    )


def test_a_dose_escalation_level_states_its_starting_dose_only():
    from app.services.ctml.checks.design import doses_beside_starting_dose

    assert doses_beside_starting_dose("50 mg (starting dose)/100/150 mg PO QD")
    assert doses_beside_starting_dose("12.5 mg PO QD (starting dose); 300 mg PO QD (sub-study)")
    assert not doses_beside_starting_dose("25 mg (starting dose) PO BID, Days 1-21 of each 21-day cycle")
    assert not doses_beside_starting_dose("100/200/300 mg PO QD")


def test_rules_are_described_in_words_for_the_resolver():
    from app.services.ctml.workflow import render_match

    rule = {
        "and": [
            {"clinical": {"age_expression": ">=18"}},
            {
                "prior_treatment": {
                    "treatment_category": "Stem Cell Transplant",
                    "transplant_type": "!Allogeneic",
                }
            },
            {"or": [{"clinical": {"oncotree_primary_diagnosis": "Disease A", "her2_status": "Negative"}}]},
            {"genomic": {"hugo_symbol": "GENE1", "variant_category": "!Mutation"}},
        ]
    }
    text = render_match(rule)
    assert "ALL of:" in text and "ANY of:" in text
    assert "- NOT prior Stem Cell Transplant: Allogeneic" in text
    assert "- diagnosis Disease A, HER2 Negative" in text
    assert "- NOT GENE1 Mutation" in text and "- age >=18" in text


def test_an_arm_without_a_diagnosis_is_listed_for_review():
    from app.services.ctml.compiler import names_diagnosis

    assert not names_diagnosis({"and": [{"clinical": {"age_expression": ">=18"}}]})
    assert not names_diagnosis({"and": [{"clinical": {"oncotree_primary_diagnosis": "!Disease A"}}]})
    assert names_diagnosis({"and": [{"or": [{"clinical": {"oncotree_primary_diagnosis": "_SOLID_"}}]}]})


def test_an_arm_that_requires_and_excludes_the_same_diagnosis_is_blocked():
    from app.services.ctml.compiler import contradictions
    from app.services.ctml.registries.oncotree import OncoTreeIndex
    from app.tests.ctml.synthetic import ONCOTREE_NODES

    oncotree = OncoTreeIndex.from_nodes(ONCOTREE_NODES)
    required = {"clinical": {"oncotree_primary_diagnosis": "Melanoma"}}
    parent_excluded = {"clinical": {"oncotree_primary_diagnosis": "!Skin"}}
    assert contradictions([required, parent_excluded], oncotree) == [("Skin", "Melanoma")]
    other_excluded = {"clinical": {"oncotree_primary_diagnosis": "!Non-Small Cell Lung Cancer"}}
    assert contradictions([required, other_excluded], oncotree) == []
    either = {
        "or": [
            {"clinical": {"oncotree_primary_diagnosis": "Melanoma"}},
            {"clinical": {"oncotree_primary_diagnosis": "Non-Small Cell Lung Cancer"}},
        ]
    }
    assert contradictions([either, parent_excluded], oncotree) == []
