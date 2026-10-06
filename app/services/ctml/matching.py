"""Compile agent trees into CTML match nodes, and evaluate CTML match trees on patients.

Evaluation is three-valued: True, False, or None (not evaluable). AND is False when any
child is False, OR is True when any child is True; otherwise a None child makes the
result None. A "!" prefix negates a condition; None stays None. Diagnosis hierarchy and
solid/liquid come from the run's OncoTree tree. Nothing is guessed: an unknown name or a
missing patient fact gives None.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from app.services.ctml.contracts import GroupNode, LeafNode
from app.services.ctml.registries.oncotree import OncoTreeIndex

_AGE = re.compile(r"^(>=|<=|>|<)(\d+(?:\.\d+)?)$")
_NEGATABLE_GENOMIC = (
    "hugo_symbol",
    "variant_category",
    "protein_change",
    "wildcard_protein_change",
    "cnv_call",
    "fusion_partner_hugo_symbol",
)
_NEGATABLE_TREATMENT = ("agent", "agent_class", "transplant_type")


# --- compile ------------------------------------------------------------------------------


def leaf_payload(leaf: LeafNode) -> dict:
    kind = leaf.kind()
    if kind is None:
        raise ValueError("a leaf needs exactly one of clinical, genomic or prior_treatment")
    return {kind: getattr(leaf, kind).model_dump(exclude_none=True)}


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for item in items:
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def compile_tree(node: GroupNode | LeafNode) -> dict:
    """CTML match node. Nested groups with the same operator are merged, duplicates removed,
    and a group with one item becomes that item."""
    if isinstance(node, LeafNode):
        return leaf_payload(node)
    items: list[dict] = []
    for child in node.items:
        compiled = compile_tree(child)
        if list(compiled) == [node.type]:
            items.extend(compiled[node.type])
        else:
            items.append(compiled)
    items = _dedupe(items)
    return items[0] if len(items) == 1 else {node.type: items}


def tree_hash(ctml_node: dict) -> str:
    return hashlib.sha256(json.dumps(ctml_node, sort_keys=True).encode()).hexdigest()[:16]


def iter_leaves(node: GroupNode | LeafNode, path: str = "/tree"):
    """(path, leaf) for every leaf of an agent tree."""
    if isinstance(node, LeafNode):
        yield path, node
        return
    for index, child in enumerate(node.items):
        yield from iter_leaves(child, f"{path}/items/{index}")


def iter_groups(node: GroupNode | LeafNode, path: str = "/tree"):
    if isinstance(node, GroupNode):
        yield path, node
        for index, child in enumerate(node.items):
            yield from iter_groups(child, f"{path}/items/{index}")


# --- evaluate -----------------------------------------------------------------------------


@dataclass
class Knowledge:
    oncotree: OncoTreeIndex | None


def _and(values: list[bool | None]) -> bool | None:
    if any(value is False for value in values):
        return False
    return None if any(value is None for value in values) else True


def _or(values: list[bool | None]) -> bool | None:
    if any(value is True for value in values):
        return True
    return None if any(value is None for value in values) else False


def _split(value: str | None) -> tuple[bool, str]:
    text = str(value or "").strip()
    return text.startswith("!"), text.lstrip("!").strip()


def _age(expression: str, age: float | None) -> bool | None:
    match = _AGE.match(expression.replace(" ", ""))
    if match is None or age is None:
        return None
    operator, number = match.group(1), float(match.group(2))
    return {">=": age >= number, "<=": age <= number, ">": age > number, "<": age < number}[operator]


def _diagnosis(value: str, diagnosis: str | None, knowledge: Knowledge) -> bool | None:
    if diagnosis is None or knowledge.oncotree is None:
        return None
    liquid = knowledge.oncotree.is_liquid(diagnosis)
    if value == "_SOLID_":
        return None if liquid is None else not liquid
    if value == "_LIQUID_":
        return liquid
    return knowledge.oncotree.is_under(diagnosis, value)


def _clinical(fields: dict, patient: dict, knowledge: Knowledge) -> tuple[bool | None, list[str]]:
    results, notes = [], []
    for name, raw in fields.items():
        negated, value = _split(raw)
        if name == "age_expression":
            result = _age(value, patient.get("age"))
        elif name == "oncotree_primary_diagnosis":
            result = _diagnosis(value, patient.get("diagnosis"), knowledge)
        elif name in ("her2_status", "er_status", "pr_status"):
            result = None if patient.get(name) is None else patient.get(name) == value
        else:
            result = None
        if negated and result is not None:
            result = not result
        results.append(result)
        notes.append(f"{name}={raw}: {result}")
    return _and(results), notes


def _protein_matches(fields: dict, alteration: dict) -> bool:
    change = str(alteration.get("protein_change") or "")
    exact = fields.get("protein_change")
    if exact and _split(exact)[1] != change:
        return False
    wildcard = fields.get("wildcard_protein_change")
    if wildcard:
        stem = _split(wildcard)[1]
        if not (change.startswith(stem) and not change[len(stem) : len(stem) + 1].isdigit()):
            return False
    return True


def _genomic(fields: dict, patient: dict) -> tuple[bool | None, list[str]]:
    alterations = patient.get("alterations")
    if alterations is None:
        return None, ["alterations unknown"]
    negated = any(_split(fields.get(name))[0] for name in _NEGATABLE_GENOMIC if fields.get(name))
    gene = _split(fields.get("hugo_symbol"))[1].upper()
    category = _split(fields.get("variant_category"))[1]
    cnv = _split(fields.get("cnv_call"))[1] if fields.get("cnv_call") else None
    partner = (
        _split(fields.get("fusion_partner_hugo_symbol"))[1].upper()
        if fields.get("fusion_partner_hugo_symbol")
        else None
    )

    def matches(alteration: dict) -> bool:
        return (
            str(alteration.get("hugo_symbol", "")).upper() == gene
            and alteration.get("variant_category") == category
            and (cnv is None or alteration.get("cnv_call") == cnv)
            and (
                partner is None or str(alteration.get("fusion_partner_hugo_symbol") or "").upper() == partner
            )
            and _protein_matches(fields, alteration)
        )

    hit = any(matches(alteration) for alteration in alterations)
    return (not hit if negated else hit), [f"alteration {gene} {category}: {'found' if hit else 'absent'}"]


def _prior_treatment(fields: dict, patient: dict) -> tuple[bool | None, list[str]]:
    therapies = patient.get("prior_treatments")
    if therapies is None:
        return None, ["prior treatments unknown"]
    negated = any(_split(fields.get(name))[0] for name in _NEGATABLE_TREATMENT if fields.get(name))
    category = fields.get("treatment_category")
    agent = _split(fields.get("agent"))[1].casefold() if fields.get("agent") else None
    agent_class = _split(fields.get("agent_class"))[1].casefold() if fields.get("agent_class") else None
    transplant = _split(fields.get("transplant_type"))[1] if fields.get("transplant_type") else None

    def matches(therapy: dict) -> bool:
        if therapy.get("treatment_category") != category:
            return False
        name = str(therapy.get("agent") or "").casefold()
        classes = {str(c).casefold() for c in therapy.get("agent_classes") or []}
        if agent and name != agent:
            return False
        if agent_class and agent_class not in classes and name != agent_class:
            return False
        return not (transplant and therapy.get("transplant_type") != transplant)

    hit = any(matches(therapy) for therapy in therapies)
    label = fields.get("agent") or fields.get("agent_class") or fields.get("transplant_type") or category
    return (not hit if negated else hit), [f"prior {label}: {'found' if hit else 'absent'}"]


def evaluate(
    node: dict, patient: dict, knowledge: Knowledge, path: str = ""
) -> tuple[bool | None, list[str]]:
    """(result, trace) for a CTML match node."""
    if "and" in node or "or" in node:
        operator = "and" if "and" in node else "or"
        results, trace = [], []
        for index, child in enumerate(node[operator]):
            result, child_trace = evaluate(child, patient, knowledge, f"{path}/{operator}/{index}")
            results.append(result)
            trace.extend(child_trace)
        combined = _and(results) if operator == "and" else _or(results)
        trace.append(f"{path or '/'} {operator.upper()}: {combined}")
        return combined, trace
    if "clinical" in node:
        result, notes = _clinical(node["clinical"], patient, knowledge)
    elif "genomic" in node:
        result, notes = _genomic(node["genomic"], patient)
    elif "prior_treatment" in node:
        result, notes = _prior_treatment(node["prior_treatment"], patient)
    else:
        result, notes = None, ["unsupported node"]
    return result, [f"{path or '/'} {'; '.join(notes)} -> {result}"]


def evaluate_arm(match: list[dict], patient: dict, knowledge: Knowledge) -> tuple[bool | None, list[str]]:
    """An arm's match list is ANDed."""
    if not match:
        return None, ["empty match"]
    return evaluate({"and": match} if len(match) > 1 else match[0], patient, knowledge)
