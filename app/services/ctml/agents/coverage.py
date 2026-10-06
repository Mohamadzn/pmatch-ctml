"""The coverage checker (v5): an independent listing of one criterion's conditions. It sees the
criterion only, never the structure; code compares the two."""

from __future__ import annotations

from typing import Any

from agent_framework import FunctionTool, tool

from app.services.ctml.agents.criterion import criterion_message
from app.services.ctml.agents.instructions import COVERAGE
from app.services.ctml.agents.loop import (
    AgentSpec,
    LoopOutcome,
    already_accepted,
    run_submit_loop,
    submit_response,
)
from app.services.ctml.checks.common import line_texts
from app.services.ctml.checks.coverage import check_coverage
from app.services.ctml.contracts import CoverageSubmission
from app.services.ctml.document.inventory import Criterion
from app.services.ctml.runtime import AgentState, RunContext

MAX_SUBMITS = 4


def owner_of(criterion: Criterion) -> str:
    return f"{criterion.criterion_id}/coverage"


def coverage_message(context: RunContext, criterion: Criterion) -> str:
    parts = [criterion_message(context, criterion)]
    tables = [context.document.tables[t].line_id for t in criterion.table_ids if t in context.document.tables]
    if tables:
        parts.append("Tables the criterion refers to:\n" + line_texts(context.document, tables))
    return "\n\n".join(parts)


def coverage_tools(context: RunContext, criterion: Criterion, state: AgentState) -> list[FunctionTool]:
    @tool(
        name="submit_coverage",
        description="Submit every condition of the criterion with its exact words and kind. Returns "
        "accepted, or errors to fix.",
        max_invocations=MAX_SUBMITS,
    )
    async def submit_coverage(submission: CoverageSubmission) -> str:
        if state.accepted is not None:
            return already_accepted()
        submission = CoverageSubmission.model_validate(submission)
        problems, accepted = check_coverage(submission, criterion, context)
        return submit_response(state, problems, accepted, submission)

    return [submit_coverage]


async def list_conditions(client: Any, context: RunContext, criterion: Criterion) -> LoopOutcome:
    spec = AgentSpec(
        name="pmatch_coverage",
        owner=owner_of(criterion),
        instructions=COVERAGE,
        first_message=coverage_message(context, criterion),
        submit_tool="submit_coverage",
        build_tools=lambda state: coverage_tools(context, criterion, state),
    )
    return await run_submit_loop(client, spec, context)
