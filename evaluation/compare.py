"""Compare a generated CTML with a gold CTML, and score patient profiles.

    python -m evaluation.compare --ctml runs/<run>/<id>_CTML.json \
        --gold evaluation/gold/<id>_GoldStandard_<date>.json \
        --profiles evaluation/profiles/<id>.json --out runs/<run>/comparison.md

Leaf overlap with gold is a representation metric, not clinical accuracy: gold files have
known errors, and one meaning can be encoded in several ways. Patient profiles measure
meaning. A profile gates only when a reviewer has confirmed it against the protocol
(reviewed: true) and it does not depend on a pending decision (pending: false).

This module is evaluation only. Nothing under app/ imports it.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from app.services.ctml.matching import Knowledge, evaluate_arm
from app.services.ctml.registries.http import RegistryError, RegistryHttp
from app.services.ctml.registries.ncit import search_concepts
from app.services.ctml.registries.oncotree import OncoTreeIndex

EVS_CONCEPT_URL = "https://api-evsrest.nci.nih.gov/api/v1/concept/ncit"
METADATA_FIELDS = (
    "trial_id",
    "long_title",
    "short_title",
    "protocol_no",
    "protocol_version_no",
    "protocol_version_date",
    "phase",
)
TEXT_MATCH = 0.8


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _norm(text: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").casefold()))


def _similar(a: Any, b: Any) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def arms_of(document: dict) -> list[dict]:
    return [arm for step in document.get("treatment_list", {}).get("step", []) for arm in step.get("arm", [])]


# --- metadata ---------------------------------------------------------------------------


def metadata_rows(ctml: dict, gold: dict) -> list[dict]:
    rows = []
    for name in METADATA_FIELDS:
        generated, expected = ctml.get(name), gold.get(name)
        if name == "trial_id":
            expected = str(expected or "").split("_")[0]
        if name == "protocol_version_date":
            generated, expected = str(generated or "")[:10], str(expected or "")[:10]
        rows.append(
            {
                "field": name,
                "generated": generated,
                "gold": expected,
                "match": _norm(generated) == _norm(expected),
            }
        )
    sponsor = lambda d: (d.get("sponsor_list", {}).get("sponsor") or [{}])[0].get("sponsor_name")  # noqa: E731
    rows.append(
        {
            "field": "sponsor_name",
            "generated": sponsor(ctml),
            "gold": sponsor(gold),
            "match": _similar(sponsor(ctml), sponsor(gold)) >= TEXT_MATCH,
        }
    )
    return rows


# --- arms and leaves --------------------------------------------------------------------


LABEL = re.compile(r"\b(part|arm|cohort|stage|group|module)\s+([0-9]+[a-z]?|[a-z]|[ivx]+)\b", re.I)
ROMAN = {
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
}


def arm_name(arm: dict) -> str:
    """arm_code, or the start of arm_description when a (gold) arm has no code."""
    if arm.get("arm_code"):
        return str(arm["arm_code"]).strip()
    description = str(arm.get("arm_description") or "").strip()
    return f"(no arm_code) {description[:60]}".strip()


def arm_labels(code: Any) -> dict[str, str]:
    """Design labels in an arm code: 'Part 1 Optional Arm C (qd)' -> {'part': '1', 'arm': 'c'}."""
    labels: dict[str, str] = {}
    for kind, value in LABEL.findall(str(code or "")):
        value = value.casefold()
        labels.setdefault(kind.casefold(), ROMAN.get(value, value))
    return labels


def label_score(a: dict[str, str], b: dict[str, str]) -> int:
    """+1 per label both arms share, -2 per label kind with different values (Arm C vs Arm D)."""
    shared = set(a) & set(b)
    agree = sum(1 for kind in shared if a[kind] == b[kind])
    return agree - 2 * (len(shared) - agree)


def _drugs(arm: dict) -> set[str]:
    return {_norm(level.get("level_code")) for level in arm.get("dose_level", []) if level.get("level_code")}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def arm_score(a: dict, b: dict) -> tuple[int, float]:
    """(label score, content score). Content: arm name 50%, drugs 25%, match-tree leaves 25%."""
    name = _similar(a.get("arm_code"), b.get("arm_code")) if a.get("arm_code") and b.get("arm_code") else 0.0
    content = (
        0.5 * name
        + 0.25 * _jaccard(_drugs(a), _drugs(b))
        + 0.25 * overlap(leaves(a.get("match", [])), leaves(b.get("match", [])))["f1"]
    )
    return label_score(arm_labels(a.get("arm_code")), arm_labels(b.get("arm_code"))), round(content, 3)


def align_arms(generated: list[dict], gold: list[dict]) -> list[dict]:
    """Pair arms one to one, best first: design labels (Part, Arm, Cohort), then content.

    Arms whose labels conflict (Arm C and Arm D) are never paired. An arm left over is
    'split' when a paired arm on the other side shares its labels (one gold arm encoded as
    several generated arms), or 'merged' (several gold arms encoded as one generated arm).
    """
    scores = {(i, j): arm_score(a, b) for i, a in enumerate(generated) for j, b in enumerate(gold)}
    ranked = sorted(scores, key=lambda key: (scores[key], -key[0], -key[1]), reverse=True)
    used_generated: set[int] = set()
    used_gold: set[int] = set()
    result: list[dict] = []
    for i, j in ranked:
        if i in used_generated or j in used_gold or scores[i, j][0] < 0:
            continue
        used_generated.add(i)
        used_gold.add(j)
        result.append({"generated": i, "gold": j, "relation": "pair", "score": scores[i, j]})
    for i in range(len(generated)):
        if i not in used_generated:
            options = [j for j in range(len(gold)) if scores[i, j][0] > 0]
            best = max(options, key=lambda j: scores[i, j], default=None)
            result.append(
                {
                    "generated": i,
                    "gold": best,
                    "relation": "split" if best is not None else "generated only",
                    "score": scores[i, best] if best is not None else (0, 0.0),
                }
            )
    for j in range(len(gold)):
        if j not in used_gold:
            options = [i for i in range(len(generated)) if scores[i, j][0] > 0]
            best = max(options, key=lambda i: scores[i, j], default=None)
            result.append(
                {
                    "generated": best,
                    "gold": j,
                    "relation": "merged" if best is not None else "gold only",
                    "score": scores[best, j] if best is not None else (0, 0.0),
                }
            )
    return result


def leaves(node: Any) -> list[str]:
    """Every leaf of a match tree as canonical JSON (values trimmed of spaces)."""
    found: list[str] = []
    if isinstance(node, list):
        for item in node:
            found += leaves(item)
    elif isinstance(node, dict):
        if "and" in node or "or" in node:
            found += leaves(node.get("and") or node.get("or"))
        else:
            cleaned = {kind: {k: str(v).strip() for k, v in fields.items()} for kind, fields in node.items()}
            found.append(json.dumps(cleaned, sort_keys=True))
    return found


def overlap(generated: list[str], gold: list[str]) -> dict:
    got, want = Counter(generated), Counter(gold)
    common = sum((got & want).values())
    precision = common / sum(got.values()) if got else 0.0
    recall = common / sum(want.values()) if want else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "missing": sorted((want - got).elements()),
        "extra": sorted((got - want).elements()),
    }


def arm_rows(ctml: dict, gold: dict) -> list[dict]:
    generated, expected = arms_of(ctml), arms_of(gold)
    rows = []
    for pairing in align_arms(generated, expected):
        i, j = pairing["generated"], pairing["gold"]
        a = generated[i] if i is not None else {}
        b = expected[j] if j is not None else {}
        rows.append(
            {
                "generated_index": i,
                "gold_index": j,
                "generated_arm": arm_name(a) if a else None,
                "gold_arm": arm_name(b) if b else None,
                "relation": pairing["relation"],
                "label_score": pairing["score"][0],
                "content_score": pairing["score"][1],
                "doses_generated": [
                    (d.get("level_code"), d.get("level_description")) for d in a.get("dose_level", [])
                ],
                "doses_gold": [
                    (d.get("level_code"), d.get("level_description")) for d in b.get("dose_level", [])
                ],
                "leaves": overlap(leaves(a.get("match", [])), leaves(b.get("match", []))),
                "same_tree": bool(a and b)
                and json.dumps(a.get("match"), sort_keys=True) == json.dumps(b.get("match"), sort_keys=True),
            }
        )
    return rows


# --- additional criteria ------------------------------------------------------------------


def _criterion_key(text: str) -> str:
    return re.sub(r"^\s*exclusion\s+cri?t[a-z]*\s*:?\s*", "", str(text), flags=re.I)


def _best(text: str, pool: list[str]) -> float:
    return max((_similar(_criterion_key(text), _criterion_key(other)) for other in pool), default=0.0)


def _duplicates(texts: list[str]) -> int:
    return sum(count - 1 for count in Counter(_norm(t) for t in texts).values() if count > 1)


def text_overlap(generated: list[str], gold: list[str]) -> dict:
    return {
        "generated": len(generated),
        "gold": len(gold),
        "gold_found": sum(1 for text in gold if _best(text, generated) >= TEXT_MATCH),
        "generated_in_gold": sum(1 for text in generated if _best(text, gold) >= TEXT_MATCH),
        "duplicates": _duplicates(generated),
    }


def _arm_texts(arm: dict) -> list[str]:
    return [
        str(item.get("additional_criteria_requirement_name", "")) if isinstance(item, dict) else str(item)
        for item in arm.get("arm_additional_criteria_not_captured", [])
    ]


def additional_criteria(ctml: dict, gold: dict, rows: list[dict] | None = None) -> dict:
    """Trial-level and arm-level text overlap.

    Arm texts are compared with the texts of the aligned arm (pair, split or merged), not with
    every arm. Duplicates count repeats inside one list. The same text in several arms is
    reported apart ('repeated_across_arms'): it is expected when a criterion applies to them.
    """
    rows = rows if rows is not None else arm_rows(ctml, gold)
    generated_arms, gold_arms = arms_of(ctml), arms_of(gold)
    generated_texts = [_arm_texts(arm) for arm in generated_arms]
    gold_texts = [_arm_texts(arm) for arm in gold_arms]
    linked = [(r["generated_index"], r["gold_index"]) for r in rows]
    linked = [(i, j) for i, j in linked if i is not None and j is not None]

    generated_in_gold = 0
    for i, texts in enumerate(generated_texts):
        pool = [text for gi, gj in linked if gi == i for text in gold_texts[gj]]
        generated_in_gold += sum(1 for text in texts if _best(text, pool) >= TEXT_MATCH)
    gold_found = 0
    for j, texts in enumerate(gold_texts):
        pool = [text for gi, gj in linked if gj == j for text in generated_texts[gi]]
        gold_found += sum(1 for text in texts if _best(text, pool) >= TEXT_MATCH)
    arm_count = Counter(key for texts in generated_texts for key in {_norm(t) for t in texts})

    trial_generated = [str(t) for t in ctml.get("additional_criteria_requirements", [])]
    trial_gold = [str(t) for t in gold.get("additional_criteria_requirements", [])]
    all_generated_arm_texts = [t for texts in generated_texts for t in texts]
    trial = text_overlap(trial_generated, trial_gold)
    trial["gold_found_only_in_arms"] = sum(
        1
        for text in trial_gold
        if _best(text, trial_generated) < TEXT_MATCH and _best(text, all_generated_arm_texts) >= TEXT_MATCH
    )
    return {
        "trial": trial,
        "arms": {
            "generated": sum(len(texts) for texts in generated_texts),
            "gold": sum(len(texts) for texts in gold_texts),
            "gold_found": gold_found,
            "generated_in_gold": generated_in_gold,
            "duplicates": sum(_duplicates(texts) for texts in generated_texts),
            "repeated_across_arms": sum(1 for count in arm_count.values() if count > 1),
            "gold_found_only_in_trial": sum(
                1
                for j, texts in enumerate(gold_texts)
                for text in texts
                if _best(text, [t for gi, gj in linked if gj == j for t in generated_texts[gi]]) < TEXT_MATCH
                and _best(text, trial_generated) >= TEXT_MATCH
            ),
        },
    }


# --- profiles ---------------------------------------------------------------------------


async def expand_classes(profiles: dict, http: RegistryHttp | None) -> list[str]:
    """Add each drug's NCIt ancestor classes to the patient's agent_classes (in place)."""
    notes: list[str] = []
    if http is None:
        return ["NCIt class expansion skipped (--no-registry)."]
    cache: dict[str, set[str]] = {}
    for profile in profiles.get("profiles", []):
        for therapy in profile.get("patient", {}).get("prior_treatments") or []:
            agent = therapy.get("agent")
            if not agent:
                continue
            if agent not in cache:
                cache[agent] = set()
                try:
                    candidates, _ = await search_concepts(http, agent, limit=5)
                    exact = [c for c in candidates if str(c.get("name", "")).casefold() == agent.casefold()]
                    if exact:
                        paths, _ = await http.get_json(
                            f"{EVS_CONCEPT_URL}/{exact[0]['code']}/pathsToRoot", {"include": "minimal"}
                        )
                        cache[agent] = _names_in(paths) - {exact[0]["name"]}
                    else:
                        notes.append(f"NCIt has no concept named '{agent}'.")
                except RegistryError as error:
                    notes.append(f"NCIt unavailable for '{agent}': {error}")
            therapy["agent_classes"] = sorted(set(therapy.get("agent_classes") or []) | cache[agent])
    return notes


def _names_in(data: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(data, dict):
        if data.get("code") and data.get("name"):
            names.add(str(data["name"]))
        for value in data.values():
            names |= _names_in(value)
    elif isinstance(data, list):
        for item in data:
            names |= _names_in(item)
    return names


def score_profiles(document: dict, profiles: dict, knowledge: Knowledge) -> list[dict]:
    defaults = profiles.get("defaults", {})
    groups = profiles.get("arm_groups", {})
    arms = arms_of(document)
    rows = []
    for profile in profiles.get("profiles", []):
        patient = {**defaults, **profile.get("patient", {})}
        for group, expected in profile.get("expect", {}).items():
            pattern = groups.get(group, group).casefold()
            selected = [arm for arm in arms if pattern in str(arm.get("arm_code") or "").casefold()]
            if not selected:
                rows.append(
                    {
                        "profile": profile["id"],
                        "group": group,
                        "expected": expected,
                        "result": "no arm",
                        "passed": False,
                    }
                )
                continue
            for arm in selected:
                value, _ = evaluate_arm(arm.get("match", []), patient, knowledge)
                result = "not_evaluable" if value is None else ("eligible" if value else "not_eligible")
                rows.append(
                    {
                        "profile": profile["id"],
                        "description": profile.get("description", ""),
                        "group": group,
                        "arm": arm_name(arm),
                        "expected": expected,
                        "result": result,
                        "passed": result == expected,
                        "gates": bool(profiles.get("reviewed")) and not profile.get("pending", False),
                    }
                )
    return rows


# --- report -----------------------------------------------------------------------------


async def compare(
    ctml_path: Path, gold_path: Path, profiles_path: Path | None, use_registry: bool = True
) -> dict:
    ctml, gold = load(ctml_path), load(gold_path)
    rows = arm_rows(ctml, gold)
    report: dict[str, Any] = {
        "ctml": str(ctml_path),
        "gold": str(gold_path),
        "metadata": metadata_rows(ctml, gold),
        "arms": {"generated": len(arms_of(ctml)), "gold": len(arms_of(gold)), "pairs": rows},
        "additional_criteria": additional_criteria(ctml, gold, rows),
        "all_leaves": overlap(
            leaves([a.get("match") for a in arms_of(ctml)]), leaves([a.get("match") for a in arms_of(gold)])
        ),
    }
    if profiles_path is None:
        return report
    profiles = load(profiles_path)
    http = RegistryHttp(cache_dir=Path(".cache/pmatch")) if use_registry else None
    try:
        oncotree = await OncoTreeIndex.load(http) if http else None
        notes = await expand_classes(profiles, http)
    except (RegistryError, ValueError) as error:
        oncotree, notes = None, [f"Registries unavailable: {error}. Profile results are not evaluable."]
    finally:
        if http is not None:
            await http.aclose()
    knowledge = Knowledge(oncotree)
    report["profiles"] = {
        "reviewed": bool(profiles.get("reviewed")),
        "notes": notes,
        "generated": score_profiles(ctml, profiles, knowledge),
        "gold": score_profiles(gold, profiles, knowledge),
    }
    return report


def markdown(report: dict) -> str:
    lines = ["# Comparison with gold", "", f"- CTML: `{report['ctml']}`", f"- Gold: `{report['gold']}`", ""]
    lines += ["Leaf overlap is a representation metric, not clinical accuracy.", "", "## Metadata", ""]
    lines += ["| field | generated | gold | match |", "|---|---|---|---|"]
    lines += [
        f"| {r['field']} | {r['generated']} | {r['gold']} | {'yes' if r['match'] else 'no'} |"
        for r in report["metadata"]
    ]
    arms = report["arms"]
    lines += ["", f"## Arms (generated {arms['generated']}, gold {arms['gold']})", ""]
    lines += [
        "Arms are paired by their Part, Arm and Cohort labels first, then by name, drugs and leaves. "
        "'split': one gold arm encoded as several generated arms. 'merged': the reverse.",
        "",
        "| generated arm | gold arm | relation | leaf precision | leaf recall | same tree |",
        "|---|---|---|---|---|---|",
    ]
    for pair in arms["pairs"]:
        same = "yes" if pair["same_tree"] else "no"
        lines.append(
            f"| {pair['generated_arm'] or '-'} | {pair['gold_arm'] or '-'} | {pair['relation']} | "
            f"{pair['leaves']['precision']} | {pair['leaves']['recall']} | {same} |"
        )
    for pair in arms["pairs"]:
        if pair["leaves"]["missing"] or pair["leaves"]["extra"]:
            lines += [
                "",
                f"### {pair['generated_arm'] or '-'} / {pair['gold_arm'] or '-'} ({pair['relation']})",
                "",
            ]
            lines += [f"- gold only: `{leaf}`" for leaf in pair["leaves"]["missing"]]
            lines += [f"- generated only: `{leaf}`" for leaf in pair["leaves"]["extra"]]
            lines += [f"- doses generated: {pair['doses_generated']}", f"- doses gold: {pair['doses_gold']}"]
    extra = report["additional_criteria"]
    lines += [
        "",
        "## Additional criteria",
        "",
        "Arm texts are compared with the aligned gold arm. 'duplicates' counts repeats inside one list; "
        "'repeated_across_arms' is the number of texts present in several generated arms. "
        "Counts are text similarity, not clinical equivalence.",
        "",
        "```json",
        json.dumps(extra, indent=2),
        "```",
    ]
    if "profiles" in report:
        profiles = report["profiles"]
        lines += ["", f"## Patient profiles (reviewed: {profiles['reviewed']})", ""]
        lines += [f"- {note}" for note in profiles["notes"]]
        for name in ("generated", "gold"):
            rows = profiles[name]
            passed = sum(1 for r in rows if r["passed"])
            lines += ["", f"### {name}: {passed} of {len(rows)} pass", ""]
            lines += ["| profile | arm | expected | result | gates |", "|---|---|---|---|---|"]
            lines += [
                f"| {r['profile']} | {r.get('arm') or r['group']} | {r['expected']} | {r['result']} | "
                f"{'yes' if r.get('gates') else 'no'} |"
                for r in rows
            ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare a CTML with gold and score patient profiles.")
    parser.add_argument("--ctml", required=True)
    parser.add_argument("--gold", required=True)
    parser.add_argument("--profiles")
    parser.add_argument("--out", help="Markdown report path (a .json file is written next to it)")
    parser.add_argument("--no-registry", action="store_true", help="Do not call OncoTree or NCIt")
    args = parser.parse_args(argv)
    report = asyncio.run(
        compare(
            Path(args.ctml),
            Path(args.gold),
            Path(args.profiles) if args.profiles else None,
            not args.no_registry,
        )
    )
    text = markdown(report)
    if args.out:
        out = Path(args.out)
        out.write_text(text, encoding="utf-8")
        out.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"Report: {out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
