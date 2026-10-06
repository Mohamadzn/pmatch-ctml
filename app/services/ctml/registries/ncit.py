"""NCI Thesaurus search through the NCI EVS REST API.

Each concept gets a kind, from NCIt's own properties:
- "class": a semantic type of a drug class ("Chemical Viewed Functionally", "Chemical Viewed
  Structurally" or "Classification"), for example NCIt's inhibitor, agonist, antibody and
  targeting-agent classes. The NCI Drug Dictionary lists many of these classes, so an NCI
  drug dictionary ID alone does not make a concept a drug. Used for agent_class.
- "drug": a single substance: an FDA UNII, CAS or NSC code, or an NCI drug dictionary ID, or
  the semantic type "Pharmacologic Substance" (investigational agents often have no codes).
  Used for agent.
- "other": genes, proteins, diseases, procedures and the rest. Not used for therapy leaves.
Semantic types are UMLS categories, not a term list.
"""

from __future__ import annotations

from app.services.ctml.registries.http import RegistryHttp

EVS_SEARCH_URL = "https://api-evsrest.nci.nih.gov/api/v1/concept/ncit/search"
# Codes that identify one substance.
SUBSTANCE_IDENTIFIERS = {"FDA_UNII_Code", "CAS_Registry", "NSC_Code"}
DRUG_IDENTIFIERS = SUBSTANCE_IDENTIFIERS | {"NCI_Drug_Dictionary_ID"}
CLASS_TYPES = {"Chemical Viewed Functionally", "Chemical Viewed Structurally", "Classification"}
DRUG_TYPES = {"Pharmacologic Substance", "Antibiotic", "Clinical Drug"}
MAX_SYNONYMS = 20


def concept_kind(property_types: set[str], semantic_types: list[str]) -> str:
    types = set(semantic_types)
    if types & CLASS_TYPES and not property_types & SUBSTANCE_IDENTIFIERS:
        return "class"
    if property_types & DRUG_IDENTIFIERS or types & DRUG_TYPES:
        return "drug"
    if not types:
        return "class"  # no semantic type returned: nothing says it is a single substance
    return "other"


def concept_summary(concept: dict) -> dict:
    """The fields an agent needs from an EVS concept."""
    properties = concept.get("properties") or []
    property_types = {str(p.get("type")) for p in properties if isinstance(p, dict)}
    semantic_types = sorted(
        {str(p.get("value")) for p in properties if isinstance(p, dict) and p.get("type") == "Semantic_Type"}
    )
    synonyms: list[str] = []
    for synonym in concept.get("synonyms") or []:
        name = str(synonym.get("name", "")).strip() if isinstance(synonym, dict) else ""
        if name and name not in synonyms and name != concept.get("name"):
            synonyms.append(name)
    return {
        "code": concept.get("code"),
        "name": concept.get("name"),
        "kind": concept_kind(property_types, semantic_types),
        "semantic_types": semantic_types,
        "synonyms": synonyms[:MAX_SYNONYMS],
        "parents": [
            {"code": p.get("code"), "name": p.get("name")}
            for p in concept.get("parents") or []
            if isinstance(p, dict) and p.get("name")
        ],
    }


def _rank(query: str, candidates: list[dict]) -> list[dict]:
    """Exact name first, then exact synonym, then the registry's own order."""
    key = query.casefold().strip()

    def order(item: tuple[int, dict]) -> tuple[int, int]:
        position, candidate = item
        if str(candidate.get("name", "")).casefold() == key:
            return (0, position)
        if any(synonym.casefold() == key for synonym in candidate.get("synonyms", [])):
            return (1, position)
        return (2, position)

    return [candidate for _, candidate in sorted(enumerate(candidates), key=order)]


async def search_concepts(http: RegistryHttp, text: str, limit: int = 10) -> tuple[list[dict], str]:
    """(candidates, "live" or "cache") for a term, drugs and classes alike."""
    params = {
        "term": text,
        "type": "contains",
        "include": "synonyms,parents,properties",
        "pageSize": str(limit),
    }
    data, source = await http.get_json(EVS_SEARCH_URL, params=params)
    concepts = (data or {}).get("concepts") or []
    candidates = [concept_summary(concept) for concept in concepts if isinstance(concept, dict)]
    return _rank(text, candidates), source
