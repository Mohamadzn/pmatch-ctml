"""test_tree: run a draft tree on example patients before submitting it.

Patient diagnoses must be OncoTree names; drug and class names must come from this
agent's own search_therapy results. Anything else is "not_evaluable": the tool never
guesses a hierarchy.
"""

from __future__ import annotations

from typing import Annotated

from agent_framework import FunctionTool, tool
from pydantic import Field

from app.services.ctml.contracts import ExamplePatient, Tree
from app.services.ctml.matching import Knowledge, compile_tree, evaluate, tree_hash
from app.services.ctml.runtime import AgentState, RunContext
from app.services.ctml.tools.document import as_json, plain_json_schema

MAX_PATIENTS = 8


def known_therapy_names(context: RunContext, state: AgentState) -> set[str]:
    names: set[str] = set()
    for lookup_id in state.lookup_ids:
        record = context.ledger.get(lookup_id)
        if record is not None and record.registry == "ncit":
            names.update(name.casefold() for name in record.names())
    return names


def _unknown_names(patient: ExamplePatient, context: RunContext, therapies: set[str]) -> list[str]:
    problems = []
    if (
        patient.diagnosis
        and context.oncotree is not None
        and not context.oncotree.has_name(patient.diagnosis)
    ):
        problems.append(f"diagnosis '{patient.diagnosis}' is not an OncoTree name")
    for therapy in patient.prior_treatments or []:
        for name in [therapy.agent, *therapy.agent_classes]:
            if name and name.casefold() not in therapies:
                problems.append(f"'{name}' was not returned by your search_therapy calls")
    return problems


def test_tree_tool(context: RunContext, state: AgentState) -> FunctionTool:
    knowledge = Knowledge(context.oncotree)

    @tool(
        name="test_tree",
        description="Evaluate a draft tree on example patients. Returns eligible, not_eligible or "
        "not_evaluable for each patient, whether it agrees with your expectation, a trace, and the "
        "tree hash.",
    )
    async def test_tree(
        tree: Tree,
        patients: Annotated[list[ExamplePatient], Field(min_length=1, max_length=MAX_PATIENTS)],
    ) -> str:
        from pydantic import TypeAdapter

        tree = TypeAdapter(Tree).validate_python(tree)  # nested models can arrive as dicts
        patients = [ExamplePatient.model_validate(p) for p in patients]
        try:
            compiled = compile_tree(tree)
        except ValueError as error:
            return as_json({"error": str(error)})
        digest = tree_hash(compiled)
        therapies = known_therapy_names(context, state)
        results, disagreements = [], []
        eligible_agreed = not_eligible_agreed = False
        for patient in patients:
            unknown = _unknown_names(patient, context, therapies)
            if unknown:
                results.append(
                    {"name": patient.name, "result": "not_evaluable", "reason": "; ".join(unknown)}
                )
                continue
            value, trace = evaluate(compiled, patient.model_dump(), knowledge)
            outcome = "not_evaluable" if value is None else ("eligible" if value else "not_eligible")
            agrees = None if value is None else outcome == patient.expect
            if agrees is False:
                disagreements.append(f"{patient.name}: expected {patient.expect}, got {outcome}")
            eligible_agreed |= bool(agrees) and patient.expect == "eligible"
            not_eligible_agreed |= bool(agrees) and patient.expect == "not_eligible"
            results.append(
                {
                    "name": patient.name,
                    "expect": patient.expect,
                    "result": outcome,
                    "agrees": agrees,
                    "trace": trace,
                }
            )
        state.tests[digest] = {
            "eligible_agreed": eligible_agreed,
            "not_eligible_agreed": not_eligible_agreed,
            "disagreements": disagreements,
        }
        return as_json(
            {
                "tree_hash": digest,
                "compiled": compiled,
                "results": results,
                "ready_to_submit": eligible_agreed and not_eligible_agreed and not disagreements,
            }
        )

    return plain_json_schema(test_tree)
