"""The metadata agent: title, protocol number, version, phase, sponsor and purpose."""

from __future__ import annotations

from typing import Any

from agent_framework import FunctionTool, tool

from app.services.ctml.agents.instructions import METADATA
from app.services.ctml.agents.loop import (
    AgentSpec,
    LoopOutcome,
    already_accepted,
    run_submit_loop,
    submit_response,
)
from app.services.ctml.checks.metadata import check_metadata
from app.services.ctml.contracts import MetadataSubmission
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.tools.document import document_tools, render_lines

MAX_SUBMITS = 6
FIRST_PAGES_CHARS = 30000


def metadata_message(context: RunContext, state: AgentState) -> str:
    document = context.document
    first = document.lines[0].page if document.lines else 1
    last = first + context.settings.metadata_pages - 1
    rendered = render_lines(context, document.page_lines(first, last), state, limit=FIRST_PAGES_CHARS)
    heading = f"Pages {first}-{last} of the protocol (the document ends on page {document.page_count}):"
    return f"{heading}\n{rendered['lines']}"


def metadata_tools(context: RunContext, state: AgentState) -> list[FunctionTool]:
    tools = document_tools(context, state)

    @tool(
        name="submit_metadata",
        description="Submit the header fields. Returns accepted, or errors to fix.",
        max_invocations=MAX_SUBMITS,
    )
    async def submit_metadata(submission: MetadataSubmission) -> str:
        if state.accepted is not None:
            return already_accepted()
        submission = MetadataSubmission.model_validate(submission)
        problems, accepted = check_metadata(submission, [], context, state)
        return submit_response(state, problems, accepted, submission)

    return [*tools, submit_metadata]


async def extract_metadata(client: Any, context: RunContext) -> LoopOutcome:
    preview = AgentState(owner="metadata")
    spec = AgentSpec(
        name="pmatch_metadata",
        owner="metadata",
        instructions=METADATA,
        first_message=metadata_message(context, preview),
        submit_tool="submit_metadata",
        build_tools=lambda state: metadata_tools(context, state),
    )
    return await run_submit_loop(client, spec, context)
