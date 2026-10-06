"""Submit checks: every rule has a passing and a failing case, on synthetic text."""

from __future__ import annotations

from app.services.ctml.checks.criterion import check_criterion
from app.services.ctml.checks.design import check_design
from app.services.ctml.checks.metadata import (
    check_metadata,
    parse_date,
    phase_roman,
    sentence_count,
    starts_with_label,
)
from app.services.ctml.contracts import (
    Confirmation,
    CriterionSubmission,
    DesignSubmission,
    MetadataSubmission,
)
from app.services.ctml.document.glossary import build_glossary
from app.services.ctml.document.inventory import build_inventory
from app.services.ctml.document.model import build_document
from app.services.ctml.document.sections import route
from app.services.ctml.matching import compile_tree, tree_hash
from app.services.ctml.registries.ledger import Ledger
from app.services.ctml.runtime import AgentState, RunContext
from app.tests.ctml.synthetic import build_layout

CLASS_CANDIDATE = {"name": "Class A", "kind": "class", "parents": []}
DRUG_CANDIDATE = {"name": "Drug A", "kind": "drug", "parents": [{"name": "Class A"}]}


def line_of(context: RunContext, criterion_id: str) -> str:
    return context.inventory.by_id(criterion_id).line_ids[0]


def treatment_leaf(line: str, lookup: str, source: str = "anti-PD-1 antibody", **fields) -> dict:
    return {
        "type": "leaf",
        "prior_treatment": {"treatment_category": "Medical Therapy", **fields},
        "source_concept": source,
        "line_ids": [line],
        "lookup_ids": [lookup],
    }


def submission(tree: dict | None, representable: str = "full", **extra) -> CriterionSubmission:
    data = {
        "representable": representable,
        "tree": tree,
        "context_category": "prior-therapy context",
        **extra,
    }
    return CriterionSubmission.model_validate(data)


def mark_tested(state: AgentState, sub: CriterionSubmission) -> None:
    state.tests[tree_hash(compile_tree(sub.tree))] = {
        "eligible_agreed": True,
        "not_eligible_agreed": True,
        "disagreements": [],
    }


def run(context: RunContext, criterion_id: str, sub: CriterionSubmission, confirmations=(), tested=True):
    state = AgentState(owner=criterion_id)
    if tested and sub.tree is not None:
        mark_tested(state, sub)
    problems, accepted = check_criterion(
        sub,
        [Confirmation.model_validate(c) for c in confirmations],
        context.inventory.by_id(criterion_id),
        context,
        state,
    )
    return {p["code"] for p in problems}, accepted


def lookup(context: RunContext, owner: str, candidates: list[dict], registry: str = "ncit") -> str:
    return context.ledger.add(registry, "query", owner, "ok", candidates).lookup_id


# --- criterion: accepted and rejected ----------------------------------------------------


def test_negated_class_from_own_lookup_is_accepted(context):
    lk = lookup(context, "EXC-1", [CLASS_CANDIDATE])
    sub = submission(treatment_leaf(line_of(context, "EXC-1"), lk, agent_class="!Class A"))
    assert run(context, "EXC-1", sub) == (set(), [])


def test_source_concept_must_be_source_words(context):
    lk = lookup(context, "EXC-1", [CLASS_CANDIDATE])
    sub = submission(
        treatment_leaf(line_of(context, "EXC-1"), lk, source="prior immunotherapy", agent_class="!Class A")
    )
    assert "source_concept_not_in_source" in run(context, "EXC-1", sub)[0]


def test_registry_values_need_this_criterions_lookup(context):
    line = line_of(context, "EXC-1")
    other = lookup(context, "EXC-2", [CLASS_CANDIDATE])
    codes, _ = run(context, "EXC-1", submission(treatment_leaf(line, other, agent_class="!Class A")))
    assert {"unknown_lookup_id", "value_not_from_lookup"} <= codes
    own = lookup(context, "EXC-1", [CLASS_CANDIDATE])
    assert (
        "value_not_from_lookup"
        in run(context, "EXC-1", submission(treatment_leaf(line, own, agent_class="!Class B")))[0]
    )


def test_agent_and_agent_class_must_fit_the_concept_kind(context):
    line = line_of(context, "EXC-1")
    lk = lookup(context, "EXC-1", [CLASS_CANDIDATE, DRUG_CANDIDATE])
    assert (
        "agent_is_class" in run(context, "EXC-1", submission(treatment_leaf(line, lk, agent="!Class A")))[0]
    )
    assert (
        "agent_class_is_drug"
        in run(context, "EXC-1", submission(treatment_leaf(line, lk, agent_class="!Drug A")))[0]
    )
    assert run(context, "EXC-1", submission(treatment_leaf(line, lk, agent="!Drug A")))[0] == set()
    protein = lookup(context, "EXC-1", [{"name": "Protein A", "kind": "other", "parents": []}])
    codes, _ = run(context, "EXC-1", submission(treatment_leaf(line, protein, agent_class="!Protein A")))
    assert "agent_class_not_a_drug_class" in codes


def test_registry_names_are_copied_exactly(context):
    lk = lookup(context, "EXC-1", [CLASS_CANDIDATE])
    sub = submission(treatment_leaf(line_of(context, "EXC-1"), lk, agent_class="!class a"))
    assert "name_not_exact" in run(context, "EXC-1", sub)[0]


def test_leaf_lines_must_belong_to_the_criterion(context):
    lk = lookup(context, "EXC-1", [CLASS_CANDIDATE])
    elsewhere = submission(treatment_leaf(line_of(context, "INC-1"), lk, agent_class="!Class A"))
    assert "leaf_not_in_criterion" in run(context, "EXC-1", elsewhere)[0]
    unknown = submission(treatment_leaf("L9999", lk, agent_class="!Class A"))
    assert "unknown_line_id" in run(context, "EXC-1", unknown)[0]


def test_tree_must_be_tested(context):
    lk = lookup(context, "EXC-1", [CLASS_CANDIDATE])
    sub = submission(treatment_leaf(line_of(context, "EXC-1"), lk, agent_class="!Class A"))
    assert "tree_not_tested" in run(context, "EXC-1", sub, tested=False)[0]
    state = AgentState(owner="EXC-1")
    state.tests[tree_hash(compile_tree(sub.tree))] = {
        "eligible_agreed": True,
        "not_eligible_agreed": False,
        "disagreements": ["p1: expected eligible, got not_eligible"],
    }
    problems, _ = check_criterion(sub, [], context.inventory.by_id("EXC-1"), context, state)
    assert {"test_disagreement", "test_incomplete"} <= {p["code"] for p in problems}


def test_representable_and_unmapped_items_agree(context):
    line = line_of(context, "INC-4")
    assert "missing_tree" in run(context, "INC-4", submission(None, "full"))[0]
    item = {"quote": "ECOG performance status of 0 or 1", "reason": "no CTML field", "line_ids": [line]}
    assert run(context, "INC-4", submission(None, "none", unmapped_items=[item]))[0] == set()
    bad_quote = {**item, "quote": "performance status 0-2"}
    assert (
        "unmapped_quote_not_in_source"
        in run(context, "INC-4", submission(None, "none", unmapped_items=[bad_quote]))[0]
    )
    age = {
        "type": "leaf",
        "clinical": {"age_expression": ">=18"},
        "source_concept": "18 years",
        "line_ids": [line],
    }
    assert "partial_without_unmapped" in run(context, "INC-4", submission(age, "partial"))[0]
    assert "full_with_unmapped" in run(context, "INC-4", submission(age, "full", unmapped_items=[item]))[0]
    assert "tree_with_none" in run(context, "INC-4", submission(age, "none"))[0]


def test_leaf_schema_and_sentinel_rules(context):
    line = line_of(context, "INC-1")
    bad_age = {
        "type": "leaf",
        "clinical": {"age_expression": "18+"},
        "source_concept": "18 years",
        "line_ids": [line],
    }
    assert "leaf_schema" in run(context, "INC-1", submission(bad_age))[0]
    sentinel = {
        "type": "leaf",
        "clinical": {"oncotree_primary_diagnosis": "!_SOLID_"},
        "source_concept": "18 years",
        "line_ids": [line],
    }
    assert "negated_sentinel" in run(context, "INC-1", submission(sentinel))[0]
    genomic = {
        "type": "leaf",
        "genomic": {"hugo_symbol": "BRAF", "variant_category": "Mutation", "cnv_call": "Gain"},
        "source_concept": "18 years",
        "line_ids": [line],
        "lookup_ids": [lookup(context, "INC-1", [{"symbol": "BRAF"}], "hgnc")],
    }
    assert "leaf_schema" in run(context, "INC-1", submission(genomic))[0]


def test_listed_items_must_be_covered():
    pages = [
        [
            "# 5 POPULATION",
            "## 5.2 Exclusion Criteria",
            "1\\. Any of the following conditions:",
            "• Condition one",
            "• Condition two",
        ]
    ]
    document = build_document(build_layout(pages), "sha")
    routing = route(document)
    inventory = build_inventory(document, routing)
    context = RunContext(
        settings=None,
        document=document,
        routing=routing,
        inventory=inventory,
        glossary=build_glossary(document, routing),
        ledger=Ledger(),
    )
    criterion = inventory.by_id("EXC-1")
    first, second = criterion.item_line_ids
    lk = context.ledger.add("ncit", "q", "EXC-1", "ok", [CLASS_CANDIDATE]).lookup_id
    tree = {
        "type": "leaf",
        "prior_treatment": {"treatment_category": "Medical Therapy", "agent_class": "!Class A"},
        "source_concept": "Condition one",
        "line_ids": [first],
        "lookup_ids": [lk],
    }
    sub = submission(tree, "full")
    assert "list_item_not_covered" in run(context, "EXC-1", sub)[0]
    unmapped = [{"quote": "Condition two", "reason": "no CTML field", "line_ids": [second]}]
    assert run(context, "EXC-1", submission(tree, "partial", unmapped_items=unmapped))[0] == set()


# --- criterion: confirm checks -----------------------------------------------------------


def test_exclusion_without_negation_needs_confirmation(context):
    lk = lookup(context, "EXC-1", [CLASS_CANDIDATE])
    sub = submission(treatment_leaf(line_of(context, "EXC-1"), lk, agent_class="Class A"))
    assert "exclusion_without_negation" in run(context, "EXC-1", sub)[0]
    good = {"code": "exclusion_without_negation", "path": "/tree", "quote": "Prior treatment with"}
    codes, accepted = run(context, "EXC-1", sub, [good])
    assert codes == set() and accepted[0]["quote"] == "Prior treatment with"
    invented = {**good, "quote": "patients must have had this"}
    assert "confirmation_quote_not_in_source" in run(context, "EXC-1", sub, [invented])[0]
    too_short = {**good, "quote": "Prior"}
    assert "confirmation_quote_too_short" in run(context, "EXC-1", sub, [too_short])[0]


def test_exclusion_or_of_negations_needs_confirmation(context):
    line = line_of(context, "EXC-1")
    lk = lookup(context, "EXC-1", [CLASS_CANDIDATE, {"name": "Class B", "kind": "class"}])
    tree = {
        "type": "or",
        "items": [
            treatment_leaf(line, lk, agent_class="!Class A"),
            treatment_leaf(line, lk, agent_class="!Class B"),
        ],
    }
    assert "exclusion_or_of_negations" in run(context, "EXC-1", submission(tree))[0]
    tree["type"] = "and"
    assert run(context, "EXC-1", submission(tree))[0] == set()


def test_inclusion_patterns_need_confirmation(context):
    line = line_of(context, "INC-3")
    lk = lookup(context, "INC-3", [{"name": "Melanoma"}, {"name": "Acral Melanoma"}], "oncotree")

    def diagnosis(value: str) -> dict:
        return {
            "type": "leaf",
            "clinical": {"oncotree_primary_diagnosis": value},
            "source_concept": "melanoma",
            "line_ids": [line],
            "lookup_ids": [lk],
        }

    codes, _ = run(
        context,
        "INC-3",
        submission({"type": "or", "items": [diagnosis("Melanoma"), diagnosis("!Acral Melanoma")]}),
    )
    assert {"inclusion_with_negation", "inclusion_or_mixed_diagnosis"} <= codes
    codes, _ = run(
        context, "INC-3", submission({"type": "or", "items": [diagnosis("Melanoma"), diagnosis("_SOLID_")]})
    )
    assert "sentinel_beside_diagnoses" in codes


def test_category_only_leaf_needs_confirmation(context):
    line = line_of(context, "EXC-1")
    tree = {
        "type": "leaf",
        "prior_treatment": {"treatment_category": "Medical Therapy"},
        "source_concept": "Prior treatment",
        "line_ids": [line],
    }
    codes, _ = run(context, "EXC-1", submission(tree))
    assert "category_only_leaf" in codes


def test_category_only_leaf_in_a_partial_encoding_is_rejected(context):
    line = line_of(context, "EXC-1")
    tree = {
        "type": "leaf",
        "prior_treatment": {"treatment_category": "Medical Therapy"},
        "source_concept": "Prior treatment",
        "line_ids": [line],
    }
    item = {"quote": "anti-PD-1 antibody", "reason": "count of prior lines", "line_ids": [line]}
    codes, _ = run(context, "EXC-1", submission(tree, "partial", unmapped_items=[item]))
    assert "category_only_in_partial" in codes and "category_only_leaf" not in codes


def context_with(pages) -> RunContext:
    document = build_document(build_layout(pages), "sha")
    routing = route(document)
    return RunContext(
        settings=None,
        document=document,
        routing=routing,
        inventory=build_inventory(document, routing),
        glossary=build_glossary(document, routing),
        ledger=Ledger(),
    )


EXCEPTION_PAGES = [
    [
        "# 5 POPULATION",
        "## 5.1 Inclusion Criteria",
        "1\\. Adults.",
        "## 5.2 Exclusion Criteria",
        "1\\. Prior drugex therapy (unless it was given in the adjuvant setting)",
        "2\\. Prior therapy with Class A; prior Drug B is allowed.",
        "3\\. Prior drugex therapy, unless approved after consultation with the Sponsor.",
    ]
]


def test_negation_with_an_unmapped_exception_is_rejected():
    context = context_with(EXCEPTION_PAGES)
    line = line_of(context, "EXC-1")
    lk = lookup(context, "EXC-1", [{"name": "Drugex", "kind": "drug", "parents": []}])
    leaf = treatment_leaf(line, lk, source="Prior drugex therapy", agent="!Drugex")
    exception = {
        "quote": "unless it was given in the adjuvant setting",
        "reason": "treatment setting is not a CTML field",
        "line_ids": [line],
    }
    codes, _ = run(context, "EXC-1", submission(leaf, "partial", unmapped_items=[exception]))
    assert "negation_with_open_exception" in codes
    text_only = submission(None, "none", unmapped_items=[{**exception, "quote": "Prior drugex therapy"}])
    assert run(context, "EXC-1", text_only)[0] == set()


def test_a_case_by_case_waiver_does_not_block_the_negation():
    context = context_with(EXCEPTION_PAGES)
    line = line_of(context, "EXC-3")
    lk = lookup(context, "EXC-3", [{"name": "Drugex", "kind": "drug", "parents": []}])
    leaf = treatment_leaf(line, lk, source="Prior drugex therapy", agent="!Drugex")
    waiver = {
        "quote": "unless approved after consultation with the Sponsor",
        "reason": "a case-by-case waiver",
        "line_ids": [line],
    }
    codes, _ = run(context, "EXC-3", submission(leaf, "partial", unmapped_items=[waiver]))
    assert "negation_with_open_exception" not in codes and "exclusion_exception" in codes


def test_exclusion_with_an_exception_needs_confirmation():
    context = context_with(EXCEPTION_PAGES)
    line = line_of(context, "EXC-2")
    lk = lookup(context, "EXC-2", [CLASS_CANDIDATE])
    sub = submission(treatment_leaf(line, lk, source="Prior therapy with Class A", agent_class="!Class A"))
    codes, _ = run(context, "EXC-2", sub)
    assert codes == {"exclusion_exception"}
    ok = {"code": "exclusion_exception", "path": "/tree", "quote": "Prior therapy with Class A"}
    assert run(context, "EXC-2", sub, [ok])[0] == set()


def test_tissue_level_diagnosis_needs_confirmation(context):
    line = line_of(context, "INC-3")
    lk = lookup(context, "INC-3", [{"name": "Skin"}, {"name": "Melanoma"}], "oncotree")
    leaf = {
        "type": "leaf",
        "clinical": {"oncotree_primary_diagnosis": "Skin"},
        "source_concept": "melanoma",
        "line_ids": [line],
        "lookup_ids": [lk],
    }
    assert "tissue_level_diagnosis" in run(context, "INC-3", submission(leaf))[0]
    leaf["clinical"]["oncotree_primary_diagnosis"] = "Melanoma"
    assert run(context, "INC-3", submission(leaf))[0] == set()


def test_optional_substudy_criteria_take_no_tree(context):
    lk = lookup(context, "EXC-1", [CLASS_CANDIDATE])
    sub = submission(
        treatment_leaf(line_of(context, "EXC-1"), lk, agent_class="!Class A"),
        context_category="optional sub-study",
    )
    assert "tree_for_optional_substudy" in run(context, "EXC-1", sub)[0]


# --- design ------------------------------------------------------------------------------


def design(context: RunContext, **changes) -> DesignSubmission:
    lines = {line.text: line.line_id for line in context.document.lines}
    part1 = next(i for t, i in lines.items() if t.startswith("Part 1 is dose escalation"))
    part2 = next(i for t, i in lines.items() if t.startswith("Part 2 is dose expansion"))
    data = {
        "design_type": "multi_part",
        "design_quote": "This is an open-label, two-part study.",
        "drugs": [{"name": "drugex", "role": "investigational", "line_ids": [part1]}],
        "arms": [
            {
                "arm_code": "Part 1: Dose Escalation",
                "arm_description": "Dose escalation.",
                "scope_labels": ["Part 1"],
                "dose_levels": [
                    {
                        "level_code": "drugex",
                        "level_description": "100 mg (starting dose) orally once daily",
                        "line_ids": [part1],
                    }
                ],
                "line_ids": [part1],
            },
            {
                "arm_code": "Part 2: Dose Expansion",
                "arm_description": "Dose expansion.",
                "scope_labels": ["Part 2"],
                "dose_levels": [
                    {
                        "level_code": "drugex",
                        "level_description": "200 mg orally once daily",
                        "line_ids": [part2],
                    }
                ],
                "line_ids": [part2],
            },
        ],
    }
    for path, value in changes.items():
        target = data
        keys = path.split("__")
        for key in keys[:-1]:
            target = target[int(key)] if key.isdigit() else target[key]
        target[keys[-1]] = value
    return DesignSubmission.model_validate(data)


def design_codes(context: RunContext, sub: DesignSubmission, confirmations=()) -> set[str]:
    problems, _ = check_design(
        sub, [Confirmation.model_validate(c) for c in confirmations], context, AgentState("design")
    )
    return {p["code"] for p in problems}


def test_design_accepted(context):
    assert design_codes(context, design(context)) == set()


def test_design_rejections(context):
    assert "dose_number_not_in_source" in design_codes(
        context, design(context, arms__0__dose_levels__0__level_description="150 mg orally once daily")
    )
    assert "level_code_not_a_drug" in design_codes(
        context, design(context, arms__0__dose_levels__0__level_code="otherdrug")
    )
    assert "scope_label_unassigned" in design_codes(context, design(context, arms__1__scope_labels=[]))
    assert "unknown_scope_label" in design_codes(
        context, design(context, arms__1__scope_labels=["Part 2", "Cohort Z"])
    )
    assert "duplicate_arm_code" in design_codes(
        context, design(context, arms__1__arm_code="Part 1: Dose Escalation")
    )
    assert "quote_not_in_source" in design_codes(context, design(context, design_quote="A randomized study."))
    assert "drug_not_in_cited_lines" in design_codes(context, design(context, drugs__0__name="otherdrug"))


def test_randomized_arms_with_same_scope_need_confirmation(context):
    sub = design(
        context,
        design_type="randomized",
        arms__1__scope_labels=["Part 1", "Part 2"],
        arms__0__scope_labels=["Part 1", "Part 2"],
    )
    assert "randomized_multiple_arms" in design_codes(context, sub)
    confirmation = {
        "code": "randomized_multiple_arms",
        "path": "/arms",
        "quote": "This is an open-label, two-part study.",
    }
    assert design_codes(context, sub, [confirmation]) == set()


def test_arms_with_the_same_regimen_and_population_need_confirmation(context):
    same = design(
        context,
        arms__1__scope_labels=["Part 1"],
        arms__1__dose_levels=design(context).arms[0].model_dump()["dose_levels"],
    )
    assert "arms_not_distinct" in design_codes(context, same)
    assert "arms_not_distinct" not in design_codes(context, design(context))


def test_a_redacted_dose_may_come_from_the_registry_record(context):
    registry_dose = {"level_code": "drugex", "level_description": "400 mg IV Q3W", "line_ids": ["R1"]}
    sub = design(context, arms__0__dose_levels=[registry_dose])
    assert "unknown_line_id" in design_codes(context, sub)
    context.registry_lines = {"R1": "Arm 'Drugex' (EXPERIMENTAL): drugex 400 mg IV every three weeks (Q3W)"}
    assert design_codes(context, sub) == set()
    wrong = design(context, arms__0__dose_levels=[{**registry_dose, "level_description": "500 mg IV Q3W"}])
    assert "dose_number_not_in_source" in design_codes(context, wrong)
    # Drugs and arms cite the protocol, never the registry.
    drugs = design(context, drugs=[{"name": "drugex", "role": "investigational", "line_ids": ["R1"]}])
    assert "unknown_line_id" in design_codes(context, drugs)


# --- metadata ----------------------------------------------------------------------------


def metadata(context: RunContext, **values) -> MetadataSubmission:
    lines = {line.text: line.line_id for line in context.document.lines}

    def cite(fragment: str) -> str:
        return next(i for t, i in lines.items() if fragment in t)

    data = {
        "long_title": {
            "value": "A Phase 2 Study of Drugex in Adults with Advanced Solid Tumors",
            "line_ids": [cite("A Phase 2")],
        },
        "protocol_no": {"value": "EXP-123", "line_ids": [cite("Protocol Number")]},
        "protocol_version_no": {"value": "Amendment 3", "line_ids": [cite("Amendment 3")]},
        "protocol_version_date": {"value": "12 March 2024", "line_ids": [cite("Date:")]},
        "phase": {"value": "Phase 2", "line_ids": [cite("A Phase 2")]},
        "sponsor_name": {"value": "Example Pharma Inc.", "line_ids": [cite("Sponsor:")]},
    }
    for name, value in values.items():
        data[name] = {**data.get(name, {"line_ids": [cite("Protocol Number")]}), "value": value}
    return MetadataSubmission.model_validate(data)


def metadata_codes(context: RunContext, sub: MetadataSubmission) -> set[str]:
    return {p["code"] for p in check_metadata(sub, [], context, AgentState("metadata"))[0]}


def test_metadata_accepted(context):
    assert metadata_codes(context, metadata(context)) == set()


def test_metadata_rejections(context):
    assert "value_starts_with_label" in metadata_codes(
        context, metadata(context, protocol_no="Protocol Number: EXP-123")
    )
    assert "value_not_in_cited_lines" in metadata_codes(context, metadata(context, protocol_no="EXP-999"))
    assert "protocol_no_is_version_label" in metadata_codes(
        context, metadata(context, protocol_no="Amendment 3")
    )
    assert "phase_unparsed" in metadata_codes(context, metadata(context, phase="A Phase"))
    assert "protocol_no_is_version_label" not in metadata_codes(
        context, metadata(context, protocol_no="V940-001")
    )


def test_purpose_is_one_or_two_sentences(context):
    assert sentence_count("This study will evaluate drugex. It enrols adults.") == 2
    assert sentence_count("Drugs (e.g. drugex) are given. Adults (ie, over 18) are eligible.") == 2
    long_purpose = "One. Two sentences. Three sentences. Four sentences."
    assert "nct_purpose_too_long" in metadata_codes(context, metadata(context, nct_purpose=long_purpose))
    assert "nct_purpose_too_long" not in metadata_codes(context, metadata(context, nct_purpose="One. Two."))


def test_date_phase_and_label_rules():
    assert str(parse_date("28-Jun-2021")) == "2021-06-28"
    assert str(parse_date("June 28, 2021")) == "2021-06-28"
    assert str(parse_date("2021-06-28")) == "2021-06-28"
    assert str(parse_date("28JUN2021")) == "2021-06-28"
    assert str(parse_date("28/06/2021")) == "2021-06-28"
    assert parse_date("06/07/2021") is None  # day and month cannot be told apart
    assert phase_roman("Phase 1b/2") == "I"
    assert phase_roman("PHASE III") == "III"
    assert phase_roman("no phase here") is None
    assert starts_with_label("Protocol No.: 123-45", "protocol_no")
    assert not starts_with_label("STUDY-123: A Phase 3 Trial", "long_title")
    assert not starts_with_label("ASCENT: A Randomized Phase 3 Trial", "long_title")
    assert starts_with_label("Official Title: A Randomized Phase 3 Trial", "long_title")
