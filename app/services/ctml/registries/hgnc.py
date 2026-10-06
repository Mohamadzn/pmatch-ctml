"""HGNC gene symbols through the HGNC REST API (approved, alias and previous symbols)."""

from __future__ import annotations

import re
from urllib.parse import quote

from app.services.ctml.registries.http import RegistryHttp

HGNC_URL = "https://rest.genenames.org"
_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-.]{0,19}$")


def _doc_summary(doc: dict, matched_by: str) -> dict:
    return {
        "symbol": doc.get("symbol"),
        "hgnc_id": doc.get("hgnc_id"),
        "name": doc.get("name"),
        "status": doc.get("status"),
        "aliases": list(doc.get("alias_symbol") or [])[:15],
        "previous_symbols": list(doc.get("prev_symbol") or [])[:15],
        "matched_by": matched_by,
    }


async def _fetch(http: RegistryHttp, field: str, value: str) -> tuple[list[dict], str]:
    url = f"{HGNC_URL}/fetch/{field}/{quote(value, safe='')}"
    data, source = await http.get_json(url, not_found_ok=True)
    docs = ((data or {}).get("response") or {}).get("docs") or []
    return [doc for doc in docs if isinstance(doc, dict)], source


async def search_genes(http: RegistryHttp, text: str) -> tuple[list[dict], str]:
    """Candidates for a gene symbol or name: approved symbol, then alias, then previous symbol."""
    value = text.strip()
    sources: list[str] = []
    candidates: list[dict] = []
    if _SYMBOL.match(value):
        for field, label in (
            ("symbol", "approved symbol"),
            ("alias_symbol", "alias"),
            ("prev_symbol", "previous symbol"),
        ):
            docs, source = await _fetch(http, field, value.upper() if field == "symbol" else value)
            sources.append(source)
            candidates.extend(_doc_summary(doc, label) for doc in docs)
            if candidates:
                break
    else:
        docs, source = await _fetch(http, "name", value)
        sources.append(source)
        candidates.extend(_doc_summary(doc, "approved name") for doc in docs)
    seen: set[str] = set()
    unique = []
    for candidate in candidates:
        if candidate["symbol"] and candidate["symbol"] not in seen:
            seen.add(candidate["symbol"])
            unique.append(candidate)
    return unique, "cache" if sources and all(s == "cache" for s in sources) else "live"
