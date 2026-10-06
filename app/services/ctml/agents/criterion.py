"""The criterion agent: one eligibility criterion to a CTML match tree, residual text, or both."""

from __future__ import annotations

from typing import Any

from agent_framework import FunctionTool, tool

from app.services.ctml.agents.instructions import CRITERION
from app.services.ctml.agents.loop import (
    AgentSpec,
    LoopOutcome,
    already_accepted,
    run_submit_loop,
    submit_response,
)
from app.services.ctml.checks.criterion import check_criterion
from app.services.ctml.contracts import Confirmation, CriterionSubmission
from app.services.ctml.document.inventory import Criterion
from app.services.ctml.document.model import Document
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.tools.document import document_tools, plain_json_schema
from app.services.ctml.tools.terminology import terminology_tools
from app.services.ctml.tools.tree import test_tree_tool

MAX_SUBMITS = 8


def criterion_message(context: RunContext, criterion: Criterion) -> str:
    document = context.document
    population = (
        f"Population: {criterion.scope_label}. This criterion applies only to that population."
        if criterion.scope_label
        else "Population: all participants."
    )
    parts = [
        f"Criterion {criterion.criterion_id} ({criterion.kind}).",
        population,
        f"Section: {criterion.section_title}",
    ]
    if criterion.stem:
        parts.append(f"Introduction of the list: {criterion.stem}")
    parts.append(
        "Lines:\n" + Document.render([document.line(i) for i in criterion.line_ids if document.has_line(i)])
    )
    if criterion.item_line_ids:
        parts.append(
            "Listed sub-items (each must be cited by a leaf or by an unmapped item): "
            + ", ".join(criterion.item_line_ids)
        )
    if criterion.table_ids:
        parts.append(
            "Tables this criterion refers to (read with get_table): " + ", ".join(criterion.table_ids)
        )
    definitions = context.glossary.for_text(criterion.text)
    if definitions:
        parts.append(
            "Abbreviations defined by the protocol:\n"
            + "\n".join(f"- {d.abbreviation}: {d.definition} ({d.line_id})" for d in definitions)
        )
    return "\n\n".join(parts)


def criterion_tools(context: RunContext, criterion: Criterion, state: AgentState) -> list[FunctionTool]:
    tools = [
        *document_tools(context, state, criterion),
        *terminology_tools(context, state),
        test_tree_tool(context, state),
    ]
    state.seen_line_ids.update(criterion.line_ids)

    @tool(
        name="submit_criterion",
        description="Submit the final encoding of this criterion. Returns accepted, or errors to fix.",
        max_invocations=MAX_SUBMITS,
    )
    async def submit_criterion(
        submission: CriterionSubmission, confirmations: list[Confirmation] | None = None
    ) -> str:
        if state.accepted is not None:
            return already_accepted()
        submission = CriterionSubmission.model_validate(submission)
        confirmations = [Confirmation.model_validate(c) for c in confirmations or []]
        problems, accepted = check_criterion(submission, confirmations, criterion, context, state)
        return submit_response(state, problems, accepted, submission)

    return [*tools, plain_json_schema(submit_criterion)]


async def encode_criterion(client: Any, context: RunContext, criterion: Criterion) -> LoopOutcome:
    spec = AgentSpec(
        name="pmatch_criterion",
        owner=criterion.criterion_id,
        instructions=CRITERION,
        first_message=criterion_message(context, criterion),
        submit_tool="submit_criterion",
        build_tools=lambda state: criterion_tools(context, criterion, state),
    )
    return await run_submit_loop(client, spec, context)
