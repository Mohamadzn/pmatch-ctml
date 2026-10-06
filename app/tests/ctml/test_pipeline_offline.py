"""End to end without Azure: synthetic protocol, fake registries, a scripted model."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.services.ctml.agents.client import FUNCTION_INVOCATION
from app.services.ctml.config import Settings
from app.services.ctml.pipeline import RunOptions, run_pipeline
from app.services.ctml.qa import policy_errors
from app.services.ctml.schema.loader import validation_errors
from app.tests.ctml.fake_model import PolicyClient, last_lookup_id, line_id_of
from app.tests.ctml.synthetic import PAGES, build_layout, mock_transport


def leaf(kind: str, fields: dict, source: str, line: str, lookups: list[str] | None = None) -> dict:
    return {
        "type": "leaf",
        kind: fields,
        "source_concept": source,
        "line_ids": [line],
        "lookup_ids": lookups or [],
    }


PURPOSE = "This study will evaluate the safety and efficacy of drugex in adults with advanced solid tumors."


def metadata_step(first: str, turn: int):
    if turn > 1:
        return "done"
    protocol_no = "Protocol Number: EXP-123" if turn == 0 else "EXP-123"  # first try keeps the label

    def sourced(value: str, fragment: str) -> dict:
        return {"value": value, "line_ids": [line_id_of(first, fragment)]}

    submission = {
        "long_title": sourced(
            "A Phase 2 Study of Drugex in Adults with Advanced Solid Tumors", "A Phase 2 Study"
        ),
        "protocol_no": sourced(protocol_no, "Protocol Number"),
        "protocol_version_no": sourced("Amendment 3", "Amendment 3"),
        "protocol_version_date": sourced("12 March 2024", "Date:"),
        "phase": sourced("Phase 2", "A Phase 2 Study"),
        "sponsor_name": sourced("Example Pharma Inc.", "Sponsor:"),
        "nct_purpose": sourced(PURPOSE, "This study will evaluate"),
        "nct_id": sourced("NCT09999999", "NCT09999999"),
    }
    return [("submit_metadata", {"submission": submission})]


def design_step(first: str, turn: int):
    if turn > 1:
        return "done"
    part1 = line_id_of(first, "Part 1 is dose escalation")
    part2 = line_id_of(first, "Part 2 is dose expansion")
    first_dose = (
        "150 mg (starting dose) orally once daily"
        if turn == 0
        else "100 mg (starting dose) orally once daily"
    )
    submission = {
        "design_type": "multi_part",
        "design_quote": "This is an open-label, two-part study.",
        "drugs": [{"name": "drugex", "role": "investigational", "line_ids": [part1]}],
        "arms": [
            {
                "arm_code": "Part 1: Dose Escalation",
                "arm_description": "Part 1 is dose escalation of drugex.",
                "scope_labels": ["Part 1"],
                "dose_levels": [
                    {"level_code": "drugex", "level_description": first_dose, "line_ids": [part1]}
                ],
                "line_ids": [part1],
            },
            {
                "arm_code": "Part 2: Dose Expansion",
                "arm_description": "Part 2 is dose expansion of drugex in participants with melanoma.",
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
    return [("submit_design", {"submission": submission})]


def criterion_step(first: str, results: list[str], turn: int):
    criterion_id = re.search(r"Criterion (\S+) \(", first).group(1)
    line = re.search(r"Lines:\n(L\d+)", first).group(1)

    def tested(tree: dict, patients: list[dict], category: str):
        if turn == 0:
            return [("test_tree", {"tree": tree, "patients": patients})]
        if turn == 1:
            submission = {"representable": "full", "tree": tree, "context_category": category}
            return [("submit_criterion", {"submission": submission})]
        return "done"

    if criterion_id == "INC-1":
        tree = leaf("clinical", {"age_expression": ">=18"}, "18 years or older", line)
        patients = [
            {"name": "adult", "expect": "eligible", "age": 40},
            {"name": "minor", "expect": "not_eligible", "age": 16},
        ]
        return tested(tree, patients, "disease context")
    if criterion_id == "INC-2":
        tree = leaf("clinical", {"oncotree_primary_diagnosis": "_SOLID_"}, "advanced solid tumor", line)
        patients = [
            {"name": "melanoma", "expect": "eligible", "diagnosis": "Melanoma"},
            {"name": "lymphoma", "expect": "not_eligible", "diagnosis": "Diffuse Large B-Cell Lymphoma, NOS"},
        ]
        return tested(tree, patients, "disease context")
    if criterion_id == "INC-3":
        if turn == 0:
            return [("search_diagnosis", {"text": "melanoma"})]
        tree = leaf(
            "clinical",
            {"oncotree_primary_diagnosis": "Melanoma"},
            "melanoma",
            line,
            [last_lookup_id(results)],
        )
        patients = [
            {"name": "acral", "expect": "eligible", "diagnosis": "Acral Melanoma"},
            {"name": "lung", "expect": "not_eligible", "diagnosis": "Non-Small Cell Lung Cancer"},
        ]
        if turn == 1:
            return [("test_tree", {"tree": tree, "patients": patients})]
        if turn == 2:
            return [
                (
                    "submit_criterion",
                    {
                        "submission": {
                            "representable": "full",
                            "tree": tree,
                            "context_category": "disease context",
                        }
                    },
                )
            ]
        return "done"
    if criterion_id == "EXC-1":
        if turn == 0:
            return [("search_therapy", {"text": "anti-PD-1 antibody"})]
        lookup = last_lookup_id(results)
        plain = leaf(
            "prior_treatment",
            {"treatment_category": "Medical Therapy", "agent_class": "Anti-PD1 Monoclonal Antibody"},
            "anti-PD-1 antibody",
            line,
            [lookup],
        )
        negated = leaf(
            "prior_treatment",
            {"treatment_category": "Medical Therapy", "agent_class": "!Anti-PD1 Monoclonal Antibody"},
            "anti-PD-1 antibody",
            line,
            [lookup],
        )
        if turn == 1:  # not negated and not tested: must be rejected
            return [
                (
                    "submit_criterion",
                    {
                        "submission": {
                            "representable": "full",
                            "tree": plain,
                            "context_category": "prior-therapy context",
                        }
                    },
                )
            ]
        patients = [
            {"name": "naive", "expect": "eligible", "prior_treatments": []},
            {
                "name": "prior anti-PD-1",
                "expect": "not_eligible",
                "prior_treatments": [
                    {
                        "treatment_category": "Medical Therapy",
                        "agent_classes": ["Anti-PD1 Monoclonal Antibody"],
                    }
                ],
            },
        ]
        if turn == 2:
            return [("test_tree", {"tree": negated, "patients": patients})]
        if turn == 3:
            return [
                (
                    "submit_criterion",
                    {
                        "submission": {
                            "representable": "full",
                            "tree": negated,
                            "context_category": "prior-therapy context",
                        }
                    },
                )
            ]
        return "done"
    if criterion_id == "EXC-2":
        tree = leaf(
            "prior_treatment",
            {"treatment_category": "Stem Cell Transplant", "transplant_type": "!Allogeneic"},
            "allogeneic stem cell transplant",
            line,
        )
        patients = [
            {"name": "none", "expect": "eligible", "prior_treatments": []},
            {
                "name": "allo",
                "expect": "not_eligible",
                "prior_treatments": [
                    {"treatment_category": "Stem Cell Transplant", "transplant_type": "Allogeneic"}
                ],
            },
        ]
        return tested(tree, patients, "prior-therapy context")
    categories = {"INC-4": "performance status", "EXC-3": "CNS disease"}
    if turn == 0:
        return [
            (
                "submit_criterion",
                {"submission": {"representable": "none", "context_category": categories[criterion_id]}},
            )
        ]
    return "done"


def synthetic_policy(instructions: str, first: str, results: list[str], turn: int):
    if instructions.startswith("You extract the header fields"):
        return metadata_step(first, turn)
    if instructions.startswith("You describe the treatment arms"):
        return design_step(first, turn)
    return criterion_step(first, results, turn)


def blank_pdf(path: Path, pages: int) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(612, 792)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


@pytest.fixture
def workspace(tmp_path: Path):
    pdf = blank_pdf(tmp_path / "Prot_SYNTHETIC.pdf", len(PAGES))
    layout = tmp_path / "layout.json"
    layout.write_text(json.dumps(build_layout()), encoding="utf-8")
    settings = Settings(
        runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache", foundry_model="fake", foundry_endpoint="x"
    )
    return pdf, layout, settings


async def test_end_to_end_offline(workspace):
    """The compact layout: one criterion agent per criterion."""
    pdf, layout, settings = workspace
    settings = settings.model_copy(update={"agent_layout": "compact"})
    client = PolicyClient(synthetic_policy, function_invocation_configuration=FUNCTION_INVOCATION)
    run_dir = settings.runs_dir / "run1"
    result = await run_pipeline(
        pdf,
        settings,
        RunOptions(di_json=layout, run_dir=run_dir),
        client=client,
        http_transport=mock_transport(),
    )
    assert result.ctml_path is not None and result.ctml_path.exists()
    ctml = json.loads(result.ctml_path.read_text(encoding="utf-8"))
    assert validation_errors(ctml) == []
    assert policy_errors(ctml) == []
    assert result.qa["status"] == "needs_review"
    assert result.qa["blocking"] == [], result.qa["blocking"]

    assert ctml["trial_id"] == "NCT09999999"
    assert ctml["protocol_no"] == "EXP-123"
    assert ctml["phase"] == "II"
    assert ctml["protocol_version_date"] == "2024-03-12T04:00:00.000Z"
    assert ctml["principal_investigator"] == ""

    arms = {arm["arm_code"]: arm for arm in ctml["treatment_list"]["step"][0]["arm"]}
    part1 = json.dumps(arms["Part 1: Dose Escalation"]["match"])
    part2 = json.dumps(arms["Part 2: Dose Expansion"]["match"])
    assert "_SOLID_" in part1 and "Melanoma" not in part1
    assert '"oncotree_primary_diagnosis": "Melanoma"' in part2 and "_SOLID_" not in part2
    for match in (part1, part2):
        assert ">=18" in match and "!Anti-PD1 Monoclonal Antibody" in match and "!Allogeneic" in match
    assert arms["Part 1: Dose Escalation"]["dose_level"][0]["level_description"].startswith("100 mg")
    part1_texts = [
        t["additional_criteria_requirement_name"]
        for t in arms["Part 1: Dose Escalation"]["arm_additional_criteria_not_captured"]
    ]
    assert part1_texts == ["Part 1: Histologically confirmed advanced solid tumor."]
    trial_texts = ctml["additional_criteria_requirements"]
    assert "ECOG performance status of 0 or 1." in trial_texts
    assert "Exclusion Criteria: Known active CNS metastases." in trial_texts
    assert not any("18 years" in t for t in trial_texts)  # fully encoded, no text

    for name in (
        "evidence.json",
        "qa_report.md",
        "qa_report.json",
        "manifest.json",
        "lookups.jsonl",
        "tool_calls.jsonl",
    ):
        assert (run_dir / name).exists(), name
    calls = [
        json.loads(line) for line in (run_dir / "tool_calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rejected = [
        c
        for c in calls
        if c["tool"] == "submit_criterion" and c["owner"] == "EXC-1" and '"accepted": false' in c["result"]
    ]
    assert (
        rejected
        and "tree_not_tested" in rejected[0]["result"]
        and "exclusion_without_negation" in rejected[0]["result"]
    )
    design_calls = [c for c in calls if c["tool"] == "submit_design"]
    assert "dose_number_not_in_source" in design_calls[0]["result"]
    metadata_calls = [c for c in calls if c["tool"] == "submit_metadata"]
    assert "value_starts_with_label" in metadata_calls[0]["result"]

    # Resume: every agent output is reused, so a model that must not be called is enough.
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

    # Resuming a folder with another PDF is refused.
    other = blank_pdf(pdf.parent / "Other.pdf", len(PAGES) + 1)
    with pytest.raises(ValueError, match="different input"):
        await run_pipeline(
            other,
            settings,
            RunOptions(di_json=layout, run_dir=run_dir, resume=True),
            client=PolicyClient(no_model, function_invocation_configuration=FUNCTION_INVOCATION),
            http_transport=mock_transport(),
        )
