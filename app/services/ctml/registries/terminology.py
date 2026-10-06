"""Registry searches for the agents. Every search is recorded in the ledger with a lookup_id.

search_diagnosis: OncoTree word search. When the query is an abbreviation that the
protocol defines, its definition is searched too. When no OncoTree name fits well, the
query goes through NCIt: an OncoTree node is offered as "ncit_bridge" only if its NCI code
equals the NCIt concept found, or its name equals that concept's name or a synonym.
search_therapy: NCIt concepts (drugs and classes). search_gene: HGNC symbols.
"""

from __future__ import annotations

import logging

from app.services.ctml.document.glossary import Glossary
from app.services.ctml.registries.hgnc import search_genes
from app.services.ctml.registries.http import RegistryError, RegistryHttp
from app.services.ctml.registries.ledger import Ledger
from app.services.ctml.registries.ncit import search_concepts
from app.services.ctml.registries.oncotree import OncoTreeIndex

logger = logging.getLogger(__name__)

MAX_QUERY_CHARS = 120
BRIDGE_BELOW_SCORE = 0.75
MAX_CANDIDATES = 10


def clean_query(text: str) -> str:
    return " ".join(str(text or "").split())[:MAX_QUERY_CHARS]


class Terminology:
    def __init__(
        self,
        http: RegistryHttp,
        ledger: Ledger,
        oncotree: OncoTreeIndex | None,
        glossary: Glossary | None = None,
    ) -> None:
        self.http = http
        self.ledger = ledger
        self.oncotree = oncotree
        self.glossary = glossary or Glossary()

    @staticmethod
    def _response(record, notes: list[str] | None = None) -> dict:
        response = {"lookup_id": record.lookup_id, "query": record.query, "candidates": record.candidates}
        if record.error:
            response["error"] = record.error
        if notes:
            response["notes"] = notes
        if record.status == "no_match":
            response["notes"] = [
                *(notes or []),
                "No candidates. Try other words, or leave this item unmapped.",
            ]
        return response

    async def search_diagnosis(self, text: str, owner: str) -> dict:
        query = clean_query(text)
        if not query:
            return {"error": "Empty query."}
        if self.oncotree is None:
            record = self.ledger.add(
                "oncotree",
                query,
                owner,
                "error",
                source="error",
                error="OncoTree is not available in this run.",
            )
            return self._response(record)
        notes: list[str] = []
        found: dict[str, dict] = {}

        def keep(candidates: list[dict]) -> None:
            for candidate in candidates:
                current = found.get(candidate["code"])
                if current is None or candidate["score"] > current["score"]:
                    found[candidate["code"]] = candidate

        keep(self.oncotree.search(query))
        definitions = self.glossary.lookup(query)
        for definition in definitions:
            notes.append(f"The protocol defines {query} as '{definition.definition}' ({definition.line_id}).")
            expanded = self.oncotree.search(definition.definition)
            for candidate in expanded:
                candidate["match"] = f"{candidate['match']} (protocol definition)"
            keep(expanded)
            code_hit = found.get(query.upper())
            if code_hit and code_hit["match"] == "code":
                code_hit["warning"] = (
                    f"This OncoTree code equals the abbreviation, but the protocol defines {query} as "
                    f"'{definition.definition}'. Do not use it unless the names agree."
                )

        best = max((c["score"] for c in found.values()), default=0.0)
        source = self.oncotree.source
        if best < BRIDGE_BELOW_SCORE:
            bridge_terms = [query] + [d.definition for d in definitions]
            try:
                for term in bridge_terms[:2]:
                    concepts, _ = await search_concepts(self.http, term, limit=5)
                    for concept in concepts:
                        codes = self.oncotree.by_nci(str(concept.get("code")))
                        names = [concept.get("name"), *concept.get("synonyms", [])]
                        codes += [c for c in (self.oncotree.code_of(str(n)) for n in names if n) if c]
                        for code in dict.fromkeys(codes):
                            candidate = self.oncotree.candidate(code, 0.9, "ncit_bridge")
                            candidate["bridge"] = {
                                "ncit_code": concept.get("code"),
                                "ncit_name": concept.get("name"),
                            }
                            keep([candidate])
                notes.append(
                    "No close OncoTree name; NCIt was used to find OncoTree nodes (match ncit_bridge)."
                )
            except RegistryError as error:
                notes.append(f"NCIt bridge unavailable: {error}")
        candidates = sorted(found.values(), key=lambda c: -c["score"])[:MAX_CANDIDATES]
        record = self.ledger.add(
            "oncotree",
            query,
            owner,
            "ok" if candidates else "no_match",
            candidates,
            source=source,
            note="; ".join(notes),
        )
        return self._response(record, notes)

    async def search_therapy(self, text: str, owner: str) -> dict:
        query = clean_query(text)
        if not query:
            return {"error": "Empty query."}
        try:
            candidates, source = await search_concepts(self.http, query, limit=MAX_CANDIDATES)
        except RegistryError as error:
            record = self.ledger.add("ncit", query, owner, "error", source="error", error=str(error))
            return self._response(record)
        record = self.ledger.add("ncit", query, owner, "ok" if candidates else "no_match", candidates, source)
        return self._response(record)

    async def search_gene(self, text: str, owner: str) -> dict:
        query = clean_query(text)
        if not query:
            return {"error": "Empty query."}
        try:
            candidates, source = await search_genes(self.http, query)
        except RegistryError as error:
            record = self.ledger.add("hgnc", query, owner, "error", source="error", error=str(error))
            return self._response(record)
        record = self.ledger.add("hgnc", query, owner, "ok" if candidates else "no_match", candidates, source)
        return self._response(record)
