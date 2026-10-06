"""The v5 layout end to end without Azure: supervisor workflow, eight agents played by a
scripted model, synthetic protocol and fake registries."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.services.ctml.agents.client import FUNCTION_INVOCATION
from app.services.ctml.config import Settings
from app.services.ctml.pipeline import RunOptions, run_pipeline
from app.services.ctml.qa import policy_errors
from app.services.ctml.schema.loader import validation_errors
from app.tests.ctml.fake_model import PolicyClient, last_lookup_id
from app.tests.ctml.synthetic import PAGES, build_layout, mock_transport
from app.tests.ctml.test_pipeline_offline import blank_pdf, design_step, metadata_step

REPAIR = "Repair request from the supervisor"


def criterion_of(first: str) -> tuple[str, str]:
    """(criterion ID, first line ID) from a criterion message."""
    criterion_id = re.search(r"Criterion (\S+) \(", first).group(1)
    line = re.search(r"Lines:\n(L\d+)", first).group(1)
    return criterion_id, line


def slot(slot_id: str, domain: str, concept: str, line: str, negated: bool = False) -> dict:
    return {
        "type": "slot",
        "slot_id": slot_id,
        "domain": domain,
        "concept": concept,
        "line_ids": [line],
        "negated": negated,
    }


HAS_IT = [
    {"name": "has the condition", "expect": "eligible", "has": ["S1"]},
    {"name": "lacks the condition", "expect": "not_eligible", "has": []},
]
EXCLUDED = [
    {"name": "free of the condition", "expect": "eligible", "has": []},
    {"name": "has the excluded condition", "expect": "not_eligible", "has": ["S1"]},
]


def structure(representable: str, logic: dict | None, category: str, examples=(), unmapped=()) -> list:
    submission = {
        "representable": representable,
        "logic": logic,
        "unmapped_items": list(unmapped),
        "context_category": category,
        "examples": list(examples),
    }
    return [("submit_structure", {"submission": submission})]


def eligibility_step(first: str, turn: int):
    criterion_id, line = criterion_of(first)
    if criterion_id == "EXC-1":
        if turn == 0:  # not negated: the check asks to confirm or change
            return structure(
                "full",
                slot("S1", "prior_treatment", "anti-PD-1 antibody", line),
                "prior-therapy context",
                HAS_IT,
            )
        if turn == 1:
            logic = slot("S1", "prior_treatment", "anti-PD-1 antibody", line, negated=True)
            return structure("full", logic, "prior-therapy context", EXCLUDED)
        return "done"
    if turn > 0:
        return "done"
    if criterion_id == "INC-1":
        return structure("full", slot("S1", "clinical", "18 years or older", line), "disease context", HAS_IT)
    if criterion_id == "INC-2":
        logic = slot("S1", "clinical", "advanced solid tumor", line)
        if REPAIR in first:  # the coverage checker listed "Histologically confirmed"
            kept = [
                {
                    "quote": "Histologically confirmed",
                    "reason": "how the diagnosis was made",
                    "line_ids": [line],
                }
            ]
            return structure("partial", logic, "disease context", HAS_IT, kept)
        return structure("full", logic, "disease context", HAS_IT)
    if criterion_id == "INC-3":
        return structure("full", slot("S1", "clinical", "melanoma", line), "disease context", HAS_IT)
    if criterion_id == "EXC-2":
        logic = slot("S1", "prior_treatment", "allogeneic stem cell transplant", line, negated=True)
        return structure("full", logic, "prior-therapy context", EXCLUDED)
    category = {"INC-4": "performance status", "EXC-3": "CNS disease"}[criterion_id]
    return structure("none", None, category)


COVERAGE = {
    "INC-1": [("Age 18 years or older", "requirement")],
    "INC-2": [("Histologically confirmed", "requirement"), ("advanced solid tumor", "requirement")],
    "INC-3": [("melanoma", "requirement")],
    "INC-4": [("ECOG performance status of 0 or 1", "requirement")],
    "EXC-1": [("anti-PD-1 antibody", "requirement")],
    "EXC-2": [("allogeneic stem cell transplant", "requirement")],
    "EXC-3": [("Known active CNS metastases", "requirement")],
}


def coverage_step(first: str, turn: int):
    criterion_id, _ = criterion_of(first)
    items = [{"quote": quote, "kind": kind} for quote, kind in COVERAGE[criterion_id]]
    if criterion_id == "EXC-3" and turn == 0:  # not the protocol's words: rejected
        items = [{"quote": "brain metastases", "kind": "requirement"}]
    elif turn > (1 if criterion_id == "EXC-3" else 0):
        return "done"
    return [("submit_coverage", {"submission": {"items": items}})]


def clinical_step(first: str, results: list[str], turn: int):
    criterion_id, _ = criterion_of(first)
    if criterion_id == "INC-3":
        if turn == 0:
            return [("search_diagnosis", {"text": "melanoma"})]
        if turn > 1:
            return "done"
        value, lookups = {"oncotree_primary_diagnosis": "Melanoma"}, [last_lookup_id(results)]
    else:
        if turn > 0:
            return "done"
        value = {"INC-1": {"age_expression": ">=18"}, "INC-2": {"oncotree_primary_diagnosis": "_SOLID_"}}[
            criterion_id
        ]
        lookups = []
    fill = {"slot_id": "S1", "status": "mapped", "clinical": [value], "lookup_ids": lookups}
    return [("submit_clinical", {"submission": {"fills": [fill]}})]


def prior_therapy_step(first: str, results: list[str], turn: int):
    criterion_id, _ = criterion_of(first)
    if criterion_id == "EXC-1":
        if turn == 0:
            return [("search_therapy", {"text": "anti-PD-1 antibody"})]
        if turn > 2:
            return "done"
        # The first fill writes the exclusion itself: rejected, the structure decides polarity.
        name = "!Anti-PD1 Monoclonal Antibody" if turn == 1 else "Anti-PD1 Monoclonal Antibody"
        value = {"treatment_category": "Medical Therapy", "agent_class": name}
        fill = {
            "slot_id": "S1",
            "status": "mapped",
            "prior_treatment": [value],
            "lookup_ids": [last_lookup_id(results)],
        }
    else:
        if turn > 0:
            return "done"
        value = {"treatment_category": "Stem Cell Transplant", "transplant_type": "Allogeneic"}
        fill = {"slot_id": "S1", "status": "mapped", "prior_treatment": [value]}
    return [("submit_prior_therapy", {"submission": {"fills": [fill]}})]


def resolver_step(first: str, turn: int):
    lines = dict(re.findall(r"^### (\S+) \(.*?lines (L\d+)-L\d+\)$", first, re.M))
    issues = [
        {
            "target": "EXC-3",
            "action": "review",
            "problem": "Active CNS metastases are kept as text; a person checks the wording.",
            "quotes": ["Known active CNS metastases"],
            "line_ids": [lines["EXC-3"]],
        },
        {
            "target": "INC-1",
            "action": "repair",
            "problem": "Check that the age rule states the protocol's limit.",
            "quotes": ["18 years or older"],
            "line_ids": [lines["INC-1"]],
        },
    ]
    if turn == 0:  # a quote that the cited lines do not hold: rejected
        wrong = [{**issues[0], "quotes": ["brain metastases"]}]
        return [("submit_review", {"submission": {"issues": wrong, "summary": "One problem."}})]
    if turn == 1:
        return [("submit_review", {"submission": {"issues": issues, "summary": "Two problems."}})]
    return "done"


def v5_policy(instructions: str, first: str, results: list[str], turn: int):
    if instructions.startswith("You extract the header fields"):
        return metadata_step(first, turn)
    if instructions.startswith("You describe the treatment arms"):
        return design_step(first, turn)
    if instructions.startswith("You are the coverage checker"):
        return coverage_step(first, turn)
    if instructions.startswith("You are the eligibility logic agent"):
        return eligibility_step(first, turn)
    if instructions.startswith("You are the clinical agent"):
        return clinical_step(first, results, turn)
    if instructions.startswith("You are the prior therapy agent"):
        return prior_therapy_step(first, results, turn)
    if instructions.startswith("You are the resolver and critic"):
        return resolver_step(first, turn)
    raise AssertionError(f"unexpected agent: {instructions[:60]}")


@pytest.fixture
def workspace(tmp_path: Path):
    pdf = blank_pdf(tmp_path / "Prot_SYNTHETIC.pdf", len(PAGES))
    layout = tmp_path / "layout.json"
    layout.write_text(json.dumps(build_layout()), encoding="utf-8")
    settings = Settings(
        runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache", foundry_model="fake", foundry_endpoint="x"
    )
    assert settings.agent_layout == "v5"
    return pdf, layout, settings


def calls_of(run_dir: Path) -> list[dict]:
    text = (run_dir / "tool_calls.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


async def test_v5_workflow_end_to_end(workspace):
    pdf, layout, settings = workspace
    client = PolicyClient(v5_policy, function_invocation_configuration=FUNCTION_INVOCATION)
    run_dir = settings.runs_dir / "run1"
    result = await run_pipeline(
        pdf,
        settings,
        RunOptions(di_json=layout, run_dir=run_dir),
        client=client,
        http_transport=mock_transport(),
    )
    assert result.ctml_path is not None
    ctml = json.loads(result.ctml_path.read_text(encoding="utf-8"))
    assert validation_errors(ctml) == []
    assert policy_errors(ctml) == []
    assert result.qa["status"] == "needs_review"
    assert result.qa["blocking"] == [], result.qa["blocking"]

    # The merged encodings: polarity comes from the structure, values from the domain agents.
    arms = {arm["arm_code"]: arm for arm in ctml["treatment_list"]["step"][0]["arm"]}
    part1 = json.dumps(arms["Part 1: Dose Escalation"]["match"])
    part2 = json.dumps(arms["Part 2: Dose Expansion"]["match"])
    assert "_SOLID_" in part1 and "Melanoma" not in part1
    assert '"oncotree_primary_diagnosis": "Melanoma"' in part2 and "_SOLID_" not in part2
    for match in (part1, part2):
        assert ">=18" in match and "!Anti-PD1 Monoclonal Antibody" in match and "!Allogeneic" in match
    part1_texts = [
        t["additional_criteria_requirement_name"]
        for t in arms["Part 1: Dose Escalation"]["arm_additional_criteria_not_captured"]
    ]
    assert part1_texts == ["Part 1: Histologically confirmed advanced solid tumor."]
    trial_texts = ctml["additional_criteria_requirements"]
    assert "ECOG performance status of 0 or 1." in trial_texts
    assert "Exclusion Criteria: Known active CNS metastases." in trial_texts
    assert not any("18 years" in t or "anti-PD-1" in t for t in trial_texts)

    # The structure gate sent INC-2 back once with the omitted words.
    evidence = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    by_id = {c["criterion_id"]: c for c in evidence["criteria"]}
    assert by_id["INC-2"]["representable"] == "partial"
    assert len(by_id["INC-2"]["workflow"]["structure_attempts"]) == 2
    assert by_id["INC-2"]["workflow"]["coverage_omissions"] == []
    agents = run_dir / "agents"
    for name in (
        "metadata.json",
        "design.json",
        "coverage/INC-2.json",
        "eligibility/INC-2.json",
        "eligibility/INC-2.attempt2.json",
        "clinical/INC-2.attempt2.json",
        "prior_therapy/EXC-1.json",
        "resolver.json",
        "resolver_view.md",
        # The resolver's repair request on INC-1 ran the eligibility and clinical agents again.
        "eligibility/INC-1.attempt2.json",
        "clinical/INC-1.attempt2.json",
    ):
        assert (agents / name).exists(), name
    assert not (agents / "genomics").exists()  # no genomic slots in this protocol

    issues = evidence["workflow"]["resolver"]["issues"]
    assert [(i["target"], i["routed_to"]) for i in issues] == [("EXC-3", "review"), ("INC-1", "repair")]
    review = result.qa["review_items"]
    assert any(item.startswith("resolver:EXC-3:") for item in review)
    # The agents checked the finding and gave the same encoding: listed as not adopted.
    assert any(item.startswith("resolver_finding_not_adopted:INC-1:") for item in review)
    assert not any(item.startswith("resolver_repaired:") for item in review)
    assert evidence["workflow"]["timeline"][-1]["event"] == "finished"

    calls = calls_of(run_dir)

    def first_result(tool: str, owner: str) -> str:
        return next(c["result"] for c in calls if c["tool"] == tool and c["owner"] == owner)

    assert "exclusion_without_negation" in first_result("submit_structure", "EXC-1/eligibility")
    assert "negation_in_value" in first_result("submit_prior_therapy", "EXC-1/prior_therapy")
    assert "quote_not_in_criterion" in first_result("submit_coverage", "EXC-3/coverage")
    assert "quote_not_in_cited_lines" in first_result("submit_review", "resolver")
    # Each domain agent searched its own registry; the eligibility logic agent never searches.
    searches = {(c["owner"], c["tool"]) for c in calls if c["tool"].startswith("search_")}
    assert searches == {("INC-3/clinical", "search_diagnosis"), ("EXC-1/prior_therapy", "search_therapy")}

    # Resume: every agent outcome is reused, so a model that must not be called is enough.
    def no_model(*_args):
        raise AssertionError("the model must not be called when resuming a finished run")

    again = await run_pipeline(
        pdf,
        settings,
        RunOptions(di_json=layout, run_dir=run_dir, resume=True),
        client=PolicyClient(no_model, function_invocation_configuration=FUNCTION_INVOCATION),
        http_transport=mock_transport(),
    )
    assert json.loads(again.ctml_path.read_text(encoding="utf-8")) == ctml


async def test_v5_without_resolver_and_repairs(workspace):
    pdf, layout, settings = workspace
    settings = settings.model_copy(update={"resolver": False, "max_repair_rounds": 0})
    client = PolicyClient(v5_policy, function_invocation_configuration=FUNCTION_INVOCATION)
    run_dir = settings.runs_dir / "run2"
    result = await run_pipeline(
        pdf,
        settings,
        RunOptions(di_json=layout, run_dir=run_dir),
        client=client,
        http_transport=mock_transport(),
    )
    assert result.qa["blocking"] == [], result.qa["blocking"]
    agents = run_dir / "agents"
    assert not (agents / "resolver.json").exists()
    assert not (agents / "eligibility" / "INC-2.attempt2.json").exists()
    # Without a repair round the omission stays visible for review; the structure is not changed.
    review = result.qa["review_items"]
    assert any(
        item.startswith("coverage_omission:INC-2:") and "Histologically confirmed" in item for item in review
    )
    evidence = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    by_id = {c["criterion_id"]: c for c in evidence["criteria"]}
    assert by_id["INC-2"]["representable"] == "full"
