"""Registry search tools. Each call is recorded with a lookup_id owned by the calling agent."""

from __future__ import annotations

from typing import Annotated

from agent_framework import FunctionTool, tool
from pydantic import Field

from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.tools.document import as_json

Query = Annotated[str, Field(description="A short phrase (at most 120 characters, one line).")]


def terminology_tools(context: RunContext, state: AgentState) -> list[FunctionTool]:
    terminology = context.terminology

    def unavailable() -> str:
        return as_json({"error": "Registry search is not available in this run. List the item as unmapped."})

    def remember(result: dict) -> str:
        if result.get("lookup_id"):
            state.lookup_ids.add(result["lookup_id"])
        return as_json(result)

    @tool(
        name="search_diagnosis",
        description="Search OncoTree for a diagnosis. Returns candidates (name, code, tissue, parents) "
        "and a lookup_id.",
    )
    async def search_diagnosis(text: Query) -> str:
        if terminology is None:
            return unavailable()
        return remember(await terminology.search_diagnosis(text, state.owner))

    @tool(
        name="search_therapy",
        description="Search NCI Thesaurus for a drug or drug class. Candidates have kind drug or class, "
        "synonyms and parent classes. Returns a lookup_id.",
    )
    async def search_therapy(text: Query) -> str:
        if terminology is None:
            return unavailable()
        return remember(await terminology.search_therapy(text, state.owner))

    @tool(
        name="search_gene",
        description="Search HGNC for a gene symbol or name. Returns approved symbols and a lookup_id.",
    )
    async def search_gene(text: Query) -> str:
        if terminology is None:
            return unavailable()
        return remember(await terminology.search_gene(text, state.owner))

    return [search_diagnosis, search_therapy, search_gene]
