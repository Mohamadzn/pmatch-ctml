"""OncoTree: the full tumor-type tree, loaded once per run from the public API.

Search is word overlap between the query and each node's name, main type and code. It
ranks candidates for the agent; it never decides a mapping. Hierarchy and tissue answer
"is diagnosis X under node Y" and "is X a solid or liquid tumor" for test_tree and the
evaluator. Liquid means the OncoTree tissue Lymphoid or Myeloid (the MatchMiner rule for
_LIQUID_); everything else is solid.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.services.ctml.registries.http import RegistryHttp

ONCOTREE_URL = "https://oncotree.mskcc.org/api/tumorTypes"
ONCOTREE_VERSION = "oncotree_latest_stable (API default)"
LIQUID_TISSUES = ("Lymphoid", "Myeloid")
# English function words ignored when comparing words.
_FUNCTION_WORDS = {"of", "the", "and", "or", "with", "in", "a", "an", "for", "to", "on", "by", "type", "nos"}


def words(text: str) -> set[str]:
    """Comparable words: lower case, British "-our" spelling and plural "-s" folded."""
    result = set()
    # "PD-1" and "PD1" are the same word; "PD-L1" keeps its parts.
    folded = re.sub(r"(?<=[a-z])-(?=\d)", "", text.casefold())
    for word in re.findall(r"[a-z0-9]+", folded):
        if word in _FUNCTION_WORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        word = re.sub(r"our$", "or", word)
        result.add(word)
    return result


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


@dataclass
class OncoTreeIndex:
    nodes: dict[str, dict]
    version: str = ONCOTREE_VERSION
    source: str = "live"
    _by_name: dict[str, str] = field(default_factory=dict, repr=False)
    _by_nci: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _words: dict[str, tuple[set[str], set[str]]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for code, node in self.nodes.items():
            self._by_name.setdefault(_normalized(str(node.get("name", ""))), code)
            for nci in (node.get("externalReferences") or {}).get("NCI") or []:
                self._by_nci.setdefault(str(nci), []).append(code)
            self._words[code] = (words(str(node.get("name", ""))), words(str(node.get("mainType") or "")))

    @classmethod
    def from_nodes(
        cls, nodes: list[dict], version: str = ONCOTREE_VERSION, source: str = "live"
    ) -> OncoTreeIndex:
        return cls({str(node["code"]).upper(): node for node in nodes if node.get("code")}, version, source)

    @classmethod
    async def load(cls, http: RegistryHttp, snapshot_path: Path | None = None) -> OncoTreeIndex:
        data, source = await http.get_json(ONCOTREE_URL)
        if not isinstance(data, list) or not data:
            raise ValueError("OncoTree returned no tumor types")
        index = cls.from_nodes(data, ONCOTREE_VERSION, source)
        if snapshot_path is not None:
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_text(json.dumps(data), encoding="utf-8")
        return index

    # --- lookups ---

    def code_of(self, name: str) -> str | None:
        return self._by_name.get(_normalized(name))

    def has_name(self, name: str) -> bool:
        return self.code_of(name) is not None

    def chain(self, code: str) -> list[str]:
        """Parent codes from the immediate parent up to the root (the node itself excluded)."""
        result: list[str] = []
        current = self.nodes.get(code, {}).get("parent")
        while current and current in self.nodes and current not in result:
            result.append(current)
            current = self.nodes[current].get("parent")
        return result

    def is_under(self, name: str, ancestor_name: str) -> bool | None:
        """True when the diagnosis is the node or one of its descendants; None when unknown."""
        code, ancestor = self.code_of(name), self.code_of(ancestor_name)
        if code is None or ancestor is None:
            return None
        return code == ancestor or ancestor in self.chain(code)

    def tissue(self, name: str) -> str | None:
        code = self.code_of(name)
        return None if code is None else self.nodes[code].get("tissue")

    def is_liquid(self, name: str) -> bool | None:
        tissue = self.tissue(name)
        return None if tissue is None else tissue in LIQUID_TISSUES

    def by_nci(self, nci_code: str) -> list[str]:
        return list(self._by_nci.get(nci_code, []))

    def candidate(self, code: str, score: float, match: str) -> dict:
        node = self.nodes[code]
        references = node.get("externalReferences") or {}
        tissue = node.get("tissue")
        return {
            "code": code,
            "name": node.get("name"),
            "main_type": node.get("mainType"),
            "tissue": tissue,
            "level": node.get("level"),
            "solid_or_liquid": "liquid" if tissue in LIQUID_TISSUES else "solid",
            "parents": [{"code": p, "name": self.nodes[p].get("name")} for p in self.chain(code)],
            "nci_codes": list(references.get("NCI") or []),
            "score": round(score, 3),
            "match": match,
        }

    def search(self, text: str, limit: int = 10) -> list[dict]:
        query = _normalized(text)
        query_words = words(text)
        scored: list[tuple[float, str, str]] = []
        for code, node in self.nodes.items():
            name = _normalized(str(node.get("name", "")))
            if query and query == name:
                scored.append((1.0, code, "name"))
                continue
            if text.strip().upper() == code:
                scored.append((0.95, code, "code"))
                continue
            name_words, main_words = self._words[code]
            if not query_words:
                continue
            name_overlap = (
                len(query_words & name_words) / len(query_words | name_words) if name_words else 0.0
            )
            main_overlap = (
                len(query_words & main_words) / len(query_words | main_words) if main_words else 0.0
            )
            score = max(name_overlap, 0.8 * main_overlap)
            if query and query in name:
                score = max(score, 0.6)
            if score > 0:
                scored.append((score, code, "words"))
        scored.sort(key=lambda item: (-item[0], self.nodes[item[1]].get("level") or 0, item[1]))
        return [self.candidate(code, score, match) for score, code, match in scored[:limit]]
