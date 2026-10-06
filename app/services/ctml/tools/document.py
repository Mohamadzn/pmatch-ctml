"""Read-only document tools. Every line they return has its run-wide line ID ("L123"),
and every returned line is remembered in the agent state (a source may quote it)."""

from __future__ import annotations

import json
from typing import Annotated

from agent_framework import FunctionTool, tool
from pydantic import Field

from app.services.ctml.document.inventory import Criterion
from app.services.ctml.document.model import Line
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.text import comparison_key

MAX_TOOL_CHARS = 15000
MAX_PAGES_PER_CALL = 3
MAX_LINES_PER_CALL = 80
MAX_FIND_RESULTS = 10


def as_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def plain_json_schema(tool: FunctionTool) -> FunctionTool:
    """Remove the OpenAPI keyword "discriminator" that Pydantic adds for tagged unions from the
    schema the model receives. Arguments are still validated by the same Pydantic types."""

    def strip(node):
        if isinstance(node, dict):
            return {key: strip(value) for key, value in node.items() if key != "discriminator"}
        if isinstance(node, list):
            return [strip(value) for value in node]
        return node

    if hasattr(tool, "_cached_parameters"):
        tool._cached_parameters = strip(tool.parameters())
    return tool


def render_lines(
    context: RunContext, lines: list[Line], state: AgentState, limit: int = MAX_TOOL_CHARS
) -> dict:
    """Lines as "L12 | text" (tables in full), cut at the limit with a pointer to continue."""
    parts: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        if line.table_id and line.table_id in context.document.tables:
            text = f"{line.line_id} | " + context.document.tables[line.table_id].render(max_rows=60)
        else:
            text = f"{line.line_id} | {line.text}"
        if used + len(text) > limit and parts:
            return {"lines": "\n".join(parts), "truncated": True, "continue_from": lines[index].line_id}
        parts.append(text)
        used += len(text) + 1
        state.seen_line_ids.add(line.line_id)
    return {"lines": "\n".join(parts), "truncated": False}


def section_label_of(context: RunContext, line: Line) -> str:
    position = context.document.position(line.line_id)
    label = ""
    for section in context.routing.sections:
        if section.start <= position < section.end:
            label = section.label()  # the innermost section wins (sections are in order)
    return label


def is_historical(context: RunContext, line: Line) -> bool:
    position = context.document.position(line.line_id)
    return any(s.historical and s.start <= position < s.end for s in context.routing.sections)


def document_tools(
    context: RunContext, state: AgentState, criterion: Criterion | None = None
) -> list[FunctionTool]:
    document = context.document

    @tool(name="read_pages", description="Read whole pages of the protocol (at most 3 pages per call).")
    async def read_pages(
        first: Annotated[int, Field(description="First page number.", ge=1)],
        last: Annotated[int, Field(description="Last page number (at most first + 2).", ge=1)],
    ) -> str:
        last = min(last, first + MAX_PAGES_PER_CALL - 1)
        lines = document.page_lines(first, last)
        if not lines:
            return as_json(
                {"error": f"No text on pages {first}-{last}. The document has {document.page_count} pages."}
            )
        return as_json({"pages": f"{first}-{last}", **render_lines(context, lines, state)})

    @tool(name="read_lines", description="Read lines starting at a line ID (at most 80 lines).")
    async def read_lines(
        start_line_id: Annotated[str, Field(description="A line ID such as L120.")],
        count: Annotated[int, Field(description="Number of lines.", ge=1, le=MAX_LINES_PER_CALL)] = 30,
    ) -> str:
        if not document.has_line(start_line_id):
            return as_json({"error": f"Unknown line ID {start_line_id}."})
        start = document.position(start_line_id)
        return as_json(render_lines(context, document.lines[start : start + count], state))

    @tool(
        name="read_section",
        description="Read one section of the protocol by its section ID (from the outline).",
    )
    async def read_section(section_id: Annotated[str, Field(description="A section ID such as S12.")]) -> str:
        section = context.routing.section(section_id)
        if section is None:
            return as_json({"error": f"Unknown section ID {section_id}."})
        lines = document.lines[section.start : section.end]
        note = {"historical": True} if section.historical else {}
        return as_json(
            {"section": section.label(), "page": section.page, **note, **render_lines(context, lines, state)}
        )

    @tool(name="get_table", description="Read a table by its ID (for example T3), with all rows.")
    async def get_table(table_id: Annotated[str, Field(description="A table ID such as T3.")]) -> str:
        table = document.tables.get(table_id)
        if table is None:
            return as_json({"error": f"Unknown table ID {table_id}."})
        state.seen_line_ids.add(table.line_id)
        text = table.render()
        return as_json({"line_id": table.line_id, "page": table.page, "table": text[:MAX_TOOL_CHARS]})

    @tool(
        name="find_in_protocol",
        description="Find a phrase anywhere in the protocol (case-insensitive exact words). "
        "At most 10 matches.",
    )
    async def find_in_protocol(
        phrase: Annotated[str, Field(description="Words to find, 2 to 120 characters.")],
    ) -> str:
        key = comparison_key(phrase)
        if len(key) < 2:
            return as_json({"error": "Give at least 2 characters."})
        matches = []
        for line in document.lines:
            text = line.text
            if line.table_id and line.table_id in document.tables:
                text = document.tables[line.table_id].render()
            if key in comparison_key(text):
                state.seen_line_ids.add(line.line_id)
                matches.append(
                    {
                        "line_id": line.line_id,
                        "page": line.page,
                        "section": section_label_of(context, line),
                        "historical": is_historical(context, line),
                        "text": text[:600],
                    }
                )
                if len(matches) >= MAX_FIND_RESULTS:
                    break
        return as_json({"phrase": phrase, "matches": matches})

    @tool(name="define_term", description="The protocol's own definition of an abbreviation, with line IDs.")
    async def define_term(
        abbreviation: Annotated[str, Field(description="An abbreviation such as ANC.")],
    ) -> str:
        definitions = context.glossary.lookup(abbreviation)
        if not definitions:
            return as_json(
                {"abbreviation": abbreviation, "definitions": [], "note": "Not defined in the protocol."}
            )
        for definition in definitions:
            state.seen_line_ids.add(definition.line_id)
        conflict = len({d.definition.casefold() for d in definitions}) > 1
        return as_json(
            {
                "abbreviation": abbreviation,
                "definitions": [
                    {"definition": d.definition, "line_id": d.line_id, "page": d.page} for d in definitions
                ],
                **({"note": "The protocol gives different definitions."} if conflict else {}),
            }
        )

    tools = [read_pages, read_lines, read_section, get_table, find_in_protocol, define_term]
    if criterion is None:
        return tools

    @tool(name="read_context", description="Lines before and after this criterion, and its section heading.")
    async def read_context(
        before: Annotated[int, Field(description="Lines before.", ge=0, le=40)] = 10,
        after: Annotated[int, Field(description="Lines after.", ge=0, le=40)] = 10,
    ) -> str:
        first = document.position(criterion.line_ids[0])
        last = document.position(criterion.line_ids[-1])
        lines = document.lines[max(0, first - before) : last + after + 1]
        return as_json({"section": criterion.section_title, **render_lines(context, lines, state)})

    return [*tools, read_context]
