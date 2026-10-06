"""Tree compilation and three-valued evaluation."""

from __future__ import annotations

from app.services.ctml.contracts import GroupNode, LeafNode
from app.services.ctml.matching import Knowledge, compile_tree, evaluate, evaluate_arm, tree_hash
from app.services.ctml.registries.oncotree import OncoTreeIndex
from app.tests.ctml.synthetic import ONCOTREE_NODES

KNOWLEDGE = Knowledge(OncoTreeIndex.from_nodes(ONCOTREE_NODES))


def leaf(kind: str, **fields) -> LeafNode:
    return LeafNode.model_validate({"type": "leaf", kind: fields, "source_concept": "x", "line_ids": ["L1"]})


def group(op: str, *items) -> GroupNode:
    return GroupNode(type=op, items=list(items))


def test_compile_flattens_same_operator_and_removes_duplicates():
    age = leaf("clinical", age_expression=">=18")
    tree = group("and", age, group("and", age, leaf("clinical", oncotree_primary_diagnosis="Melanoma")))
    assert compile_tree(tree) == {
        "and": [
            {"clinical": {"age_expression": ">=18"}},
            {"clinical": {"oncotree_primary_diagnosis": "Melanoma"}},
        ]
    }
    assert compile_tree(group("or", age)) == {"clinical": {"age_expression": ">=18"}}
    assert tree_hash(compile_tree(tree)) == tree_hash(compile_tree(tree))


def test_prior_treatment_keys_keep_ctml_order():
    compiled = compile_tree(leaf("prior_treatment", agent="!Drug A", treatment_category="Medical Therapy"))
    assert list(compiled["prior_treatment"]) == ["treatment_category", "agent"]


def test_three_valued_logic():
    node = {"and": [{"clinical": {"age_expression": ">=18"}}, {"clinical": {"her2_status": "Positive"}}]}
    assert evaluate(node, {"age": 40, "her2_status": "Positive"}, KNOWLEDGE)[0] is True
    assert evaluate(node, {"age": 12}, KNOWLEDGE)[0] is False  # False wins over unknown in AND
    assert evaluate(node, {"age": 40}, KNOWLEDGE)[0] is None
    either = {"or": [{"clinical": {"age_expression": ">=18"}}, {"clinical": {"her2_status": "Positive"}}]}
    assert evaluate(either, {"age": 40}, KNOWLEDGE)[0] is True  # True wins over unknown in OR


def test_diagnosis_hierarchy_negation_and_sentinels():
    melanoma = {"clinical": {"oncotree_primary_diagnosis": "Melanoma"}}
    not_melanoma = {"clinical": {"oncotree_primary_diagnosis": "!Melanoma"}}
    solid = {"clinical": {"oncotree_primary_diagnosis": "_SOLID_"}}
    liquid = {"clinical": {"oncotree_primary_diagnosis": "_LIQUID_"}}
    assert evaluate(melanoma, {"diagnosis": "Acral Melanoma"}, KNOWLEDGE)[0] is True
    assert evaluate(not_melanoma, {"diagnosis": "Acral Melanoma"}, KNOWLEDGE)[0] is False
    assert evaluate(melanoma, {"diagnosis": "Not An OncoTree Name"}, KNOWLEDGE)[0] is None
    assert evaluate(solid, {"diagnosis": "Non-Small Cell Lung Cancer"}, KNOWLEDGE)[0] is True
    assert evaluate(solid, {"diagnosis": "Diffuse Large B-Cell Lymphoma, NOS"}, KNOWLEDGE)[0] is False
    assert evaluate(liquid, {"diagnosis": "Diffuse Large B-Cell Lymphoma, NOS"}, KNOWLEDGE)[0] is True


def test_genomic_conditions():
    braf = {"hugo_symbol": "BRAF", "variant_category": "Mutation"}
    patient = {
        "alterations": [{"hugo_symbol": "BRAF", "variant_category": "Mutation", "protein_change": "p.V600E"}]
    }
    assert evaluate({"genomic": {**braf, "protein_change": "p.V600E"}}, patient, KNOWLEDGE)[0] is True
    assert evaluate({"genomic": {**braf, "protein_change": "p.V600K"}}, patient, KNOWLEDGE)[0] is False
    assert evaluate({"genomic": {**braf, "wildcard_protein_change": "p.V600"}}, patient, KNOWLEDGE)[0] is True
    assert evaluate({"genomic": {**braf, "wildcard_protein_change": "p.V60"}}, patient, KNOWLEDGE)[0] is False
    not_v600e = {"genomic": {**braf, "protein_change": "!p.V600E"}}
    assert evaluate(not_v600e, patient, KNOWLEDGE)[0] is False
    negated = {"genomic": {"hugo_symbol": "BRAF", "variant_category": "!Mutation"}}
    assert evaluate(negated, patient, KNOWLEDGE)[0] is False
    assert evaluate(negated, {"alterations": []}, KNOWLEDGE)[0] is True
    assert evaluate(negated, {}, KNOWLEDGE)[0] is None


def test_prior_treatment_conditions():
    no_class = {"prior_treatment": {"treatment_category": "Medical Therapy", "agent_class": "!Class A"}}
    treated = {
        "prior_treatments": [
            {"treatment_category": "Medical Therapy", "agent": "Drug A", "agent_classes": ["Class A"]}
        ]
    }
    assert evaluate(no_class, treated, KNOWLEDGE)[0] is False
    assert evaluate(no_class, {"prior_treatments": []}, KNOWLEDGE)[0] is True
    assert evaluate(no_class, {}, KNOWLEDGE)[0] is None
    needs_drug = {"prior_treatment": {"treatment_category": "Medical Therapy", "agent": "Drug A"}}
    assert evaluate(needs_drug, treated, KNOWLEDGE)[0] is True
    no_allo = {
        "prior_treatment": {"treatment_category": "Stem Cell Transplant", "transplant_type": "!Allogeneic"}
    }
    auto = {
        "prior_treatments": [{"treatment_category": "Stem Cell Transplant", "transplant_type": "Autologous"}]
    }
    assert evaluate(no_allo, auto, KNOWLEDGE)[0] is True


def test_exclusion_of_any_versus_combination():
    """AND of negations excludes a patient with either item; OR of negations only one with both."""
    no_a = {"prior_treatment": {"treatment_category": "Medical Therapy", "agent": "!Drug A"}}
    no_b = {"prior_treatment": {"treatment_category": "Medical Therapy", "agent": "!Drug B"}}
    only_a = {"prior_treatments": [{"treatment_category": "Medical Therapy", "agent": "Drug A"}]}
    assert evaluate({"and": [no_a, no_b]}, only_a, KNOWLEDGE)[0] is False
    assert evaluate({"or": [no_a, no_b]}, only_a, KNOWLEDGE)[0] is True


def test_evaluate_arm_ands_the_match_list():
    match = [{"and": [{"clinical": {"age_expression": ">=18"}}]}, {"clinical": {"her2_status": "Negative"}}]
    assert evaluate_arm(match, {"age": 30, "her2_status": "Negative"}, KNOWLEDGE)[0] is True
    assert evaluate_arm([], {"age": 30}, KNOWLEDGE)[0] is None
