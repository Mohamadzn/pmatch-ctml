"""The eligibility logic agent (v5): one criterion's structure, polarity and slots."""

from __future__ import annotations

from typing import Any

from agent_framework import FunctionTool, tool

from app.services.ctml.agents.criterion import criterion_message
from app.services.ctml.agents.instructions import ELIGIBILITY
from app.services.ctml.agents.loop import (
    AgentSpec,
    LoopOutcome,
    already_accepted,
    run_submit_loop,
    submit_response,
)
from app.services.ctml.checks.eligibility import check_structure
from app.services.ctml.contracts import Confirmation, EligibilitySubmission
from app.services.ctml.document.inventory import Criterion
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.tools.document import document_tools, plain_json_schema

MAX_SUBMITS = 8


def owner_of(criterion: Criterion) -> str:
    return f"{criterion.criterion_id}/eligibility"


def eligibility_message(context: RunContext, criterion: Criterion, arms: str, repair: str = "") -> str:
    parts = [criterion_message(context, criterion)]
    parts.append(
        "Treatment arms and the population labels each one covers (for context):\n" + (arms or "(none)")
    )
    if repair:
        parts.append(repair)
    return "\n\n".join(parts)


def eligibility_tools(context: RunContext, criterion: Criterion, state: AgentState) -> list[FunctionTool]:
    tools = document_tools(context, state, criterion)
    state.seen_line_ids.update(criterion.line_ids)

    @tool(
        name="submit_structure",
        description="Submit the criterion's structure: slots, logic, unmapped items and examples. Returns "
        "accepted, or errors to fix.",
        max_invocations=MAX_SUBMITS,
    )
    async def submit_structure(
        submission: EligibilitySubmission, confirmations: list[Confirmation] | None = None
    ) -> str:
        if state.accepted is not None:
            return already_accepted()
        submission = EligibilitySubmission.model_validate(submission)
        confirmations = [Confirmation.model_validate(c) for c in confirmations or []]
        problems, accepted = check_structure(submission, confirmations, criterion, context, state)
        return submit_response(state, problems, accepted, submission)

    return [*tools, plain_json_schema(submit_structure)]


async def structure_criterion(
    client: Any, context: RunContext, criterion: Criterion, arms: str, repair: str = ""
) -> LoopOutcome:
    spec = AgentSpec(
        name="pmatch_eligibility",
        owner=owner_of(criterion),
        instructions=ELIGIBILITY,
        first_message=eligibility_message(context, criterion, arms, repair),
        submit_tool="submit_structure",
        build_tools=lambda state: eligibility_tools(context, criterion, state),
    )
    return await run_submit_loop(client, spec, context)
