"""The resolver / critic (v5): audits the assembled candidate and reports problems with the
protocol's exact words. It never edits the candidate."""

from __future__ import annotations

from typing import Any

from agent_framework import FunctionTool, tool

from app.services.ctml.agents.instructions import RESOLVER
from app.services.ctml.agents.loop import (
    AgentSpec,
    LoopOutcome,
    already_accepted,
    run_submit_loop,
    submit_response,
)
from app.services.ctml.checks.resolver import check_review
from app.services.ctml.contracts import ResolverSubmission
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.tools.document import document_tools

MAX_SUBMITS = 4
OWNER = "resolver"


def resolver_tools(context: RunContext, criterion_ids: set[str], state: AgentState) -> list[FunctionTool]:
    tools = document_tools(context, state)

    @tool(
        name="submit_review",
        description="Submit the problems you found (an empty list when there are none). Returns accepted, or "
        "errors to fix.",
        max_invocations=MAX_SUBMITS,
    )
    async def submit_review(submission: ResolverSubmission) -> str:
        if state.accepted is not None:
            return already_accepted()
        submission = ResolverSubmission.model_validate(submission)
        problems, accepted = check_review(submission, context, criterion_ids)
        return submit_response(state, problems, accepted, submission)

    return [*tools, submit_review]


async def review_candidate(
    client: Any, context: RunContext, view: str, criterion_ids: set[str]
) -> LoopOutcome:
    spec = AgentSpec(
        name="pmatch_resolver",
        owner=OWNER,
        instructions=RESOLVER,
        first_message=view,
        submit_tool="submit_review",
        build_tools=lambda state: resolver_tools(context, criterion_ids, state),
    )
    return await run_submit_loop(client, spec, context)
