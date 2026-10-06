"""The study design agent: drugs, arms and dose levels, and which eligibility scopes each arm has."""

from __future__ import annotations

import re
from typing import Any

from agent_framework import FunctionTool, tool

from app.services.ctml.agents.instructions import DESIGN
from app.services.ctml.agents.loop import (
    AgentSpec,
    LoopOutcome,
    already_accepted,
    run_submit_loop,
    submit_response,
)
from app.services.ctml.checks.design import check_design
from app.services.ctml.contracts import Confirmation, DesignSubmission
from app.services.ctml.document.sections import Section, section_lines
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.tools.document import as_json, document_tools, render_lines

MAX_SUBMITS = 8


_RANKS = (
    # Read last: reasoning, statistics and treatment-management sections rarely define arms.
    re.compile(
        r"rationale|justification|background|statistic|bayesian|sample size|analys|"
        r"discontinu|overdose|compliance|restart|rechallenge|modification",
        re.I,
    ),
    re.compile(r"synopsis|summary", re.I),
    re.compile(r"design|schema|plan", re.I),
    re.compile(r"treatment|intervention|product|drug|arm", re.I),
)


def ranked_design_sections(context: RunContext) -> list[Section]:
    """Synopsis, then design, then treatment and dosing sections; rationale sections last."""

    def rank(section: Section) -> int:
        if _RANKS[0].search(section.title):
            return 9
        for position, pattern in enumerate(_RANKS[1:]):
            if pattern.search(section.title):
                return position
        return 5

    return sorted(context.routing.design, key=lambda s: (rank(s), s.start))


def design_message(context: RunContext, state: AgentState) -> str:
    design_ids = {s.section_id for s in context.routing.design}
    outline = "\n".join(
        f"{'*' if item['section_id'] in design_ids else ' '} {item['section_id']} | {item['title']} "
        f"(page {item['page']}, {item['lines']} lines)"
        for item in context.routing.outline()
    )
    labels = context.inventory.scope_labels()
    parts = [
        "Protocol outline (section IDs for read_section; * marks design and treatment sections):\n" + outline,
        "Population labels used in the eligibility criteria: "
        + (
            ", ".join(f'"{label}"' for label in labels)
            if labels
            else "none (every criterion applies to every arm)"
        ),
    ]
    budget = context.settings.design_context_chars
    included: set[int] = set()
    for section in ranked_design_sections(context):
        if budget <= 0:
            break
        lines = [
            line
            for line in section_lines(context.document, section)
            if context.document.position(line.line_id) not in included
        ]
        if not lines:
            continue
        rendered = render_lines(context, lines, state, limit=budget)
        shown = lines
        text = f"Section {section.section_id} {section.label()}:\n{rendered['lines']}"
        if rendered["truncated"]:
            stop = next(i for i, line in enumerate(lines) if line.line_id == rendered["continue_from"])
            shown = lines[:stop]
            text += f"\n[The section continues: read_lines from {rendered['continue_from']}.]"
        included.update(context.document.position(line.line_id) for line in shown)
        parts.append(text)
        budget -= len(rendered["lines"])
    return "\n\n".join(parts)


def design_tools(context: RunContext, state: AgentState) -> list[FunctionTool]:
    tools = document_tools(context, state)

    @tool(
        name="read_registry_record",
        description="The ClinicalTrials.gov arm and intervention descriptions as lines R1, R2, ... "
        "Use them only for a dose the protocol redacts or does not state.",
    )
    async def read_registry_record() -> str:
        if not context.registry_lines:
            return as_json({"error": "No ClinicalTrials.gov record is available in this run."})
        state.seen_line_ids.update(context.registry_lines)
        lines = "\n".join(f"{line_id} | {text}" for line_id, text in context.registry_lines.items())
        return as_json({"source": context.registry_source, "lines": lines})

    @tool(
        name="submit_design",
        description="Submit the drugs and arms. Returns accepted, or errors to fix.",
        max_invocations=MAX_SUBMITS,
    )
    async def submit_design(
        submission: DesignSubmission, confirmations: list[Confirmation] | None = None
    ) -> str:
        if state.accepted is not None:
            return already_accepted()
        submission = DesignSubmission.model_validate(submission)
        confirmations = [Confirmation.model_validate(c) for c in confirmations or []]
        problems, accepted = check_design(submission, confirmations, context, state)
        return submit_response(state, problems, accepted, submission)

    return [*tools, read_registry_record, submit_design]


async def describe_design(client: Any, context: RunContext) -> LoopOutcome:
    preview = AgentState(owner="design")
    spec = AgentSpec(
        name="pmatch_design",
        owner="design",
        instructions=DESIGN,
        first_message=design_message(context, preview),
        submit_tool="submit_design",
        build_tools=lambda state: design_tools(context, state),
    )
    return await run_submit_loop(client, spec, context)
