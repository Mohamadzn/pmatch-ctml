"""Shared code of the clinical, genomics and prior therapy agents (v5). Each fills the slots of
its domain in one criterion's structure with registry values from its own searches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_framework import FunctionTool, tool
from pydantic import BaseModel

from app.services.ctml.agents.criterion import criterion_message
from app.services.ctml.agents.loop import (
    AgentSpec,
    LoopOutcome,
    already_accepted,
    run_submit_loop,
    submit_response,
)
from app.services.ctml.checks.slots import check_fills
from app.services.ctml.contracts import Confirmation, EligibilitySubmission
from app.services.ctml.document.inventory import Criterion
from app.services.ctml.merge import render_logic, slots_of
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.tools.document import document_tools, plain_json_schema
from app.services.ctml.tools.terminology import terminology_tools

MAX_SUBMITS = 6


@dataclass(frozen=True)
class Specialist:
    domain: str  # "clinical", "genomic" or "prior_treatment"
    role: str  # "clinical", "genomics" or "prior_therapy" (agent and folder name)
    instructions: str
    search_tool: str
    submit_tool: str
    submission: type[BaseModel]


def owner_of(criterion: Criterion, specialist: Specialist) -> str:
    return f"{criterion.criterion_id}/{specialist.role}"


def specialist_message(
    context: RunContext,
    criterion: Criterion,
    structure: EligibilitySubmission,
    specialist: Specialist,
    note: str = "",
) -> str:
    slots = slots_of(structure, specialist.domain)
    listed = "\n".join(
        f'- {slot.slot_id}: "{slot.concept}" ({", ".join(slot.line_ids)})'
        + (" - negated: the structure excludes it; give its positive value" if slot.negated else "")
        for slot in slots
    )
    parts = [
        criterion_message(context, criterion),
        f"Structure from the eligibility logic agent (representable: {structure.representable}):\n"
        + render_logic(structure.logic),
        f"Your {specialist.domain} slots:\n{listed}",
    ]
    if note:
        parts.append(note)
    return "\n\n".join(parts)


def specialist_tools(
    context: RunContext,
    criterion: Criterion,
    structure: EligibilitySubmission,
    specialist: Specialist,
    state: AgentState,
) -> list[FunctionTool]:
    tools = document_tools(context, state, criterion)
    search = [t for t in terminology_tools(context, state) if t.name == specialist.search_tool]
    state.seen_line_ids.update(criterion.line_ids)
    for slot in slots_of(structure):
        state.seen_line_ids.update(slot.line_ids)
    model = specialist.submission

    async def submit(submission, confirmations=None) -> str:
        if state.accepted is not None:
            return already_accepted()
        submission = model.model_validate(submission)
        confirmations = [Confirmation.model_validate(c) for c in confirmations or []]
        problems, accepted = check_fills(
            specialist.domain, submission.fills, confirmations, criterion, structure, context, state
        )
        return submit_response(state, problems, accepted, submission)

    # The submission type depends on the domain, so the annotations are set here, as real types.
    submit.__annotations__ = {"submission": model, "confirmations": list[Confirmation] | None, "return": str}
    submit_tool = tool(
        name=specialist.submit_tool,
        description=f"Submit one fill for each of your {specialist.domain} slots. Returns accepted, or "
        "errors to fix.",
        max_invocations=MAX_SUBMITS,
    )(submit)
    return [*tools, *search, plain_json_schema(submit_tool)]


async def fill_slots(
    client: Any,
    context: RunContext,
    criterion: Criterion,
    structure: EligibilitySubmission,
    specialist: Specialist,
    note: str = "",
) -> LoopOutcome:
    """note: a repair request from the supervisor, added to the first message."""
    spec = AgentSpec(
        name=f"pmatch_{specialist.role}",
        owner=owner_of(criterion, specialist),
        instructions=specialist.instructions,
        first_message=specialist_message(context, criterion, structure, specialist, note),
        submit_tool=specialist.submit_tool,
        build_tools=lambda state: specialist_tools(context, criterion, structure, specialist, state),
    )
    return await run_submit_loop(client, spec, context)
