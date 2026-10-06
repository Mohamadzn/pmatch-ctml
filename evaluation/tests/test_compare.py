"""Evaluation harness on synthetic CTML documents."""

from __future__ import annotations

import json

from app.services.ctml.matching import Knowledge
from app.services.ctml.registries.oncotree import OncoTreeIndex
from app.tests.ctml.synthetic import ONCOTREE_NODES
from evaluation.compare import (
    additional_criteria,
    align_arms,
    arm_labels,
    arm_name,
    arm_rows,
    compare,
    leaves,
    main,
    metadata_rows,
    overlap,
    score_profiles,
)


def arm(
    code: str | None, match: list, texts: tuple[str, ...] = (), drugs: tuple[str, ...] = ("drugex",)
) -> dict:
    return {
        "arm_code": code,
        "dose_level": [{"level_code": drug, "level_description": "100 mg"} for drug in drugs],
        "match": match,
        "arm_additional_criteria_not_captured": [{"additional_criteria_requirement_name": t} for t in texts],
    }


def pairs_of(generated: list[dict], gold: list[dict]) -> set[tuple]:
    return {(p["generated"], p["gold"], p["relation"]) for p in align_arms(generated, gold)}


MELANOMA = {"clinical": {"oncotree_primary_diagnosis": "Melanoma"}}
ADULT = {"clinical": {"age_expression": ">=18"}}
GOLD = {
    "trial_id": "NCT09999999_GoldStandard",
    "long_title": "A Study",
    "phase": "II",
    "protocol_version_date": "2024-03-12T04:00:00.000Z",
    "sponsor_list": {"sponsor": [{"sponsor_name": "Example Pharma Inc"}]},
    "treatment_list": {
        "step": [
            {
                "arm": [
                    arm("Part 1: Escalation", [{"and": [ADULT]}]),
                    arm("Part 2: Expansion", [{"and": [ADULT, MELANOMA]}]),
                ]
            }
        ]
    },
    "additional_criteria_requirements": ["Exclusion criteria: Known CNS metastases", "ECOG 0 or 1"],
}
GENERATED = {
    "trial_id": "NCT09999999",
    "long_title": "A Study",
    "phase": "I",
    "protocol_version_date": "2024-03-12T04:00:00.000Z",
    "sponsor_list": {"sponsor": [{"sponsor_name": "Example Pharma Inc."}]},
    "treatment_list": {
        "step": [
            {
                "arm": [
                    arm("Part 2: Dose Expansion", [{"and": [ADULT]}]),
                    arm("Part 1: Dose Escalation", [{"and": [ADULT]}]),
                ]
            }
        ]
    },
    "additional_criteria_requirements": [
        "Exclusion Criteria: Known CNS metastases.",
        "Signed consent",
        "Signed consent",
    ],
}
PROFILES = {
    "reviewed": True,
    "defaults": {"age": 50},
    "arm_groups": {"Part 1": "Part 1", "Part 2": "Part 2"},
    "profiles": [
        {
            "id": "P1",
            "patient": {"diagnosis": "Melanoma"},
            "expect": {"Part 1": "eligible", "Part 2": "eligible"},
        },
        {
            "id": "P2",
            "patient": {"diagnosis": "Non-Small Cell Lung Cancer"},
            "expect": {"Part 2": "not_eligible"},
            "pending": True,
        },
    ],
}


def test_metadata_rows():
    rows = {row["field"]: row for row in metadata_rows(GENERATED, GOLD)}
    assert (
        rows["trial_id"]["match"] and rows["protocol_version_date"]["match"] and rows["sponsor_name"]["match"]
    )
    assert not rows["phase"]["match"]


def test_arms_align_by_name_and_leaves_overlap():
    pairs = pairs_of(GENERATED["treatment_list"]["step"][0]["arm"], GOLD["treatment_list"]["step"][0]["arm"])
    assert pairs == {(0, 1, "pair"), (1, 0, "pair")}
    result = overlap(leaves([{"and": [ADULT]}]), leaves([{"and": [ADULT, MELANOMA]}]))
    assert result["precision"] == 1.0 and result["recall"] == 0.5
    assert result["missing"] == [json.dumps(MELANOMA, sort_keys=True)]


def test_additional_criteria_overlap_and_duplicates():
    trial = additional_criteria(GENERATED, GOLD)["trial"]
    assert trial["gold_found"] == 1 and trial["generated_in_gold"] == 1 and trial["duplicates"] == 1


def test_arm_labels_decide_before_names():
    assert arm_labels("Part 1 Optional Arm C (qd 1 week on/1 week off): Dose Escalation") == {
        "part": "1",
        "arm": "c",
    }
    assert arm_labels("Part II Cohort 3b: Expansion") == {"part": "2", "cohort": "3b"}
    assert arm_labels("Randomization Arm (A vs B): Drug X") == {}
    long_name = ": Dose Escalation and Confirmation of Drugex in Combination with Drugwhy"
    generated = [
        arm("Part 1 Optional Arm C (qd 1 week on/1 week off)" + long_name, []),
        arm("Part 1 Optional Arm D (monotherapy run-in, then qd)" + long_name, []),
    ]
    gold = [arm("Part 1 Optional Arm D" + long_name, []), arm("Part 1 Optional Arm C" + long_name, [])]
    assert pairs_of(generated, gold) == {(0, 1, "pair"), (1, 0, "pair")}


def test_conflicting_labels_are_not_paired():
    assert pairs_of([arm("Part 1: Escalation", [])], [arm("Part 2: Escalation", [])]) == {
        (0, None, "generated only"),
        (None, 0, "gold only"),
    }


def test_split_and_merged_arms():
    generated = [arm("Part 2 GBM Cohort: Expansion", []), arm("Part 2 TNBC Cohort: Expansion", [])]
    gold = [arm("Part 2: Disease-specific Expansion Cohorts", [])]
    relations = sorted(r for _, _, r in pairs_of(generated, gold))
    assert relations == ["pair", "split"]
    assert {j for _, j, _ in pairs_of(generated, gold)} == {0}
    relations = sorted(r for _, _, r in pairs_of(gold, generated))
    assert relations == ["merged", "pair"]


def test_gold_arm_without_code_pairs_by_content():
    generated = [
        arm("Part 2: Open-label Drugex", [{"and": [ADULT]}]),
        arm("Randomization Arm (Drugex vs Placebo)", [{"and": [ADULT]}], drugs=("drugex", "Placebo")),
    ]
    gold = [arm(None, [{"and": [ADULT, MELANOMA]}], drugs=("Drugex", "Placebo"))]
    gold[0]["arm_description"] = "Randomized, placebo-controlled study"
    assert (1, 0, "pair") in pairs_of(generated, gold)
    rows = arm_rows(
        {"treatment_list": {"step": [{"arm": generated}]}}, {"treatment_list": {"step": [{"arm": gold}]}}
    )
    assert arm_name(gold[0]).startswith("(no arm_code)")
    assert {row["gold_arm"] for row in rows if row["relation"] == "pair"} == {arm_name(gold[0])}


def test_arm_texts_compare_with_the_aligned_arm_only():
    shared = "Part 1: has received prior therapy with a cancer vaccine"
    generated = {
        "treatment_list": {
            "step": [
                {
                    "arm": [
                        arm("Part 1 Arm A: Escalation", [], texts=(shared, shared)),
                        arm("Part 1 Arm B: Escalation", [], texts=(shared,)),
                        arm("Part 2: Expansion", [], texts=("Part 2: colorectal adenocarcinoma",)),
                    ]
                }
            ]
        }
    }
    gold = {
        "treatment_list": {
            "step": [
                {
                    "arm": [
                        arm("Part 1 Arm A: Escalation", [], texts=(shared,)),
                        arm("Part 1 Arm B: Escalation", [], texts=(shared,)),
                        arm("Part 2: Expansion", [], texts=(shared,)),
                    ]
                }
            ]
        }
    }
    result = additional_criteria(generated, gold)["arms"]
    assert result["duplicates"] == 1  # the repeat inside Arm A, not the copy in Arm B
    assert result["repeated_across_arms"] == 1
    assert result["gold_found"] == 2  # the Part 2 gold text is not in the Part 2 generated arm
    assert result["generated_in_gold"] == 3


def test_profiles_score_each_arm_of_a_group():
    knowledge = Knowledge(OncoTreeIndex.from_nodes(ONCOTREE_NODES))
    rows = score_profiles(GENERATED, PROFILES, knowledge)
    by_key = {(r["profile"], r["group"]): r for r in rows}
    assert by_key[("P1", "Part 1")]["passed"] and by_key[("P1", "Part 2")]["passed"]
    assert not by_key[("P2", "Part 2")]["passed"]  # the generated Part 2 arm lost its diagnosis
    assert by_key[("P1", "Part 1")]["gates"] and not by_key[("P2", "Part 2")]["gates"]
    gold_rows = score_profiles(GOLD, PROFILES, knowledge)
    assert all(r["passed"] for r in gold_rows)


async def test_compare_without_registries(tmp_path):
    for name, data in (("ctml.json", GENERATED), ("gold.json", GOLD), ("profiles.json", PROFILES)):
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    report = await compare(
        tmp_path / "ctml.json", tmp_path / "gold.json", tmp_path / "profiles.json", use_registry=False
    )
    assert report["arms"]["generated"] == 2
    assert any("skipped" in note for note in report["profiles"]["notes"])
    melanoma_rows = [r for r in report["profiles"]["gold"] if r["profile"] == "P1" and r["group"] == "Part 2"]
    assert melanoma_rows[0]["result"] == "not_evaluable"  # no OncoTree tree without registries


def test_command_line_writes_reports(tmp_path, capsys):
    (tmp_path / "ctml.json").write_text(json.dumps(GENERATED), encoding="utf-8")
    (tmp_path / "gold.json").write_text(json.dumps(GOLD), encoding="utf-8")
    out = tmp_path / "comparison.md"
    assert (
        main(
            ["--ctml", str(tmp_path / "ctml.json"), "--gold", str(tmp_path / "gold.json"), "--out", str(out)]
        )
        == 0
    )
    assert "Leaf overlap is a representation metric" in out.read_text(encoding="utf-8")
    assert json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))["arms"]["gold"] == 2
