"""The submit-and-check loop with a scripted model (Microsoft Agent Framework, no network)."""

from __future__ import annotations

import asyncio

import pytest

from app.services.ctml.agents import loop
from app.services.ctml.agents.client import FUNCTION_INVOCATION
from app.services.ctml.agents.criterion import encode_criterion
from app.services.ctml.registries.http import RegistryHttp
from app.services.ctml.registries.terminology import Terminology
from app.tests.ctml.conftest import make_context
from app.tests.ctml.fake_model import ScriptedClient, last_lookup_id

PATIENTS = [
    {"name": "naive", "expect": "eligible", "prior_treatments": []},
    {
        "name": "treated",
        "expect": "not_eligible",
        "prior_treatments": [
            {"treatment_category": "Medical Therapy", "agent_classes": ["Anti-PD1 Monoclonal Antibody"]}
        ],
    },
]


@pytest.fixture
async def agent_context():
    from app.tests.ctml.synthetic import mock_transport

    context = make_context()
    http = RegistryHttp(transport=mock_transport())
    context.terminology = Terminology(http, context.ledger, context.oncotree, context.glossary)
    yield context
    await http.aclose()


def exclusion_tree(context, lookup_id: str, negated: bool = True) -> dict:
    line = context.inventory.by_id("EXC-1").line_ids[0]
    value = ("!" if negated else "") + "Anti-PD1 Monoclonal Antibody"
    return {
        "type": "leaf",
        "prior_treatment": {"treatment_category": "Medical Therapy", "agent_class": value},
        "source_concept": "anti-PD-1 antibody",
        "line_ids": [line],
        "lookup_ids": [lookup_id],
    }


def submit(tree: dict) -> tuple[str, dict]:
    return (
        "submit_criterion",
        {"submission": {"representable": "full", "tree": tree, "context_category": "prior-therapy context"}},
    )


def client_for(script) -> ScriptedClient:
    return ScriptedClient(script, function_invocation_configuration=FUNCTION_INVOCATION)


async def test_errors_are_fixed_and_the_loop_stops_at_acceptance(agent_context):
    good, bad = (
        exclusion_tree(agent_context, "lk-0001"),
        exclusion_tree(agent_context, "lk-0001", negated=False),
    )
    client = client_for(
        [
            [("search_therapy", {"text": "anti-PD-1 antibody"})],
            [submit(bad)],
            [("test_tree", {"tree": good, "patients": PATIENTS})],
            [submit(good)],
            [("search_therapy", {"text": "must not run"})],
        ]
    )
    outcome = await encode_criterion(client, agent_context, agent_context.inventory.by_id("EXC-1"))
    assert client.turn == 4
    assert outcome.accepted is not None and outcome.submits == 2 and outcome.reminders == 0
    assert any("exclusion_without_negation" in result for result in client.seen_results)
    assert [record.query for record in agent_context.ledger.all()] == ["anti-PD-1 antibody"]


async def test_invalid_enum_value_reaches_the_model(agent_context):
    tree = exclusion_tree(agent_context, "lk-0001")
    broken = {**tree, "prior_treatment": {"treatment_category": "Chemotherapy", "agent_class": "!X"}}
    client = client_for(
        [
            [("search_therapy", {"text": "anti-PD-1 antibody"})],
            [submit(broken)],
            [("test_tree", {"tree": tree, "patients": PATIENTS})],
            [submit(tree)],
        ]
    )
    outcome = await encode_criterion(client, agent_context, agent_context.inventory.by_id("EXC-1"))
    assert outcome.accepted is not None
    assert any("Medical Therapy" in result and "Chemotherapy" in result for result in client.seen_results)
    assert outcome.submits == 1  # an argument error never reaches the submit tool


async def test_text_reply_gets_one_reminder_in_the_same_session(agent_context):
    tree = exclusion_tree(agent_context, "lk-0001")
    client = client_for(
        [
            [("search_therapy", {"text": "anti-PD-1 antibody"})],
            [submit(tree)],  # rejected: not tested
            "I think this is fine.",
            [("test_tree", {"tree": tree, "patients": PATIENTS})],
            [submit(tree)],
        ]
    )
    outcome = await encode_criterion(client, agent_context, agent_context.inventory.by_id("EXC-1"))
    assert outcome.accepted is not None and outcome.reminders == 1
    assert client.tool_choices[0] == "required" and client.tool_choices[3] == "required"


async def test_second_submit_in_one_turn_is_refused(agent_context):
    tree = exclusion_tree(agent_context, "lk-0001")
    other = {**tree, "source_concept": "anti-PD-1"}
    client = client_for(
        [
            [("search_therapy", {"text": "anti-PD-1 antibody"})],
            [("test_tree", {"tree": tree, "patients": PATIENTS})],
            [submit(tree), submit(other)],
            [("search_therapy", {"text": "must not run"})],
        ]
    )
    outcome = await encode_criterion(client, agent_context, agent_context.inventory.by_id("EXC-1"))
    assert outcome.accepted.tree.source_concept == "anti-PD-1 antibody"
    assert client.turn == 3


async def test_parallel_criteria_do_not_share_lookups(agent_context):
    stolen = agent_context.ledger.add("oncotree", "melanoma", "EXC-2", "ok", [{"name": "Melanoma"}]).lookup_id
    line = agent_context.inventory.by_id("INC-3").line_ids[0]

    def melanoma(lookup_id: str) -> dict:
        return {
            "type": "leaf",
            "clinical": {"oncotree_primary_diagnosis": "Melanoma"},
            "source_concept": "melanoma",
            "line_ids": [line],
            "lookup_ids": [lookup_id],
        }

    def with_own_lookup(build):
        return lambda results: build(last_lookup_id(results))

    patients = [
        {"name": "acral", "expect": "eligible", "diagnosis": "Acral Melanoma"},
        {"name": "lung", "expect": "not_eligible", "diagnosis": "Non-Small Cell Lung Cancer"},
    ]
    first = client_for(
        [
            [("search_therapy", {"text": "anti-PD-1 antibody"})],
            with_own_lookup(
                lambda lk: [("test_tree", {"tree": exclusion_tree(agent_context, lk), "patients": PATIENTS})]
            ),
            with_own_lookup(lambda lk: [submit(exclusion_tree(agent_context, lk))]),
        ]
    )
    second = client_for(
        [
            [("search_diagnosis", {"text": "melanoma"})],
            [("test_tree", {"tree": melanoma(stolen), "patients": patients})],
            [
                (
                    "submit_criterion",
                    {
                        "submission": {
                            "representable": "full",
                            "tree": melanoma(stolen),
                            "context_category": "disease context",
                        }
                    },
                )
            ],
            with_own_lookup(lambda lk: [("test_tree", {"tree": melanoma(lk), "patients": patients})]),
            with_own_lookup(
                lambda lk: [
                    (
                        "submit_criterion",
                        {
                            "submission": {
                                "representable": "full",
                                "tree": melanoma(lk),
                                "context_category": "disease context",
                            }
                        },
                    )
                ]
            ),
        ]
    )
    one, two = await asyncio.gather(
        encode_criterion(first, agent_context, agent_context.inventory.by_id("EXC-1")),
        encode_criterion(second, agent_context, agent_context.inventory.by_id("INC-3")),
    )
    assert one.accepted is not None and two.accepted is not None
    assert len(one.lookup_ids) == 1 and len(two.lookup_ids) == 1
    assert set(one.lookup_ids).isdisjoint(two.lookup_ids) and stolen not in one.lookup_ids + two.lookup_ids
    assert any("unknown_lookup_id" in result for result in second.seen_results)


async def test_rate_limit_retries_with_fresh_tools(agent_context, monkeypatch):
    monkeypatch.setattr(loop, "RETRY_WAIT_SECONDS", (0, 0))

    class RateLimitError(Exception):
        pass

    tree = exclusion_tree(agent_context, "lk-0002")  # the first attempt's lookup is lk-0001
    client = client_for(
        [
            [("search_therapy", {"text": "anti-PD-1 antibody"})],
            [("search_therapy", {"text": "anti-PD-1 antibody"})],
            [("test_tree", {"tree": tree, "patients": PATIENTS})],
            [submit(tree)],
        ]
    )
    original = client._inner_get_response
    calls = {"count": 0}

    async def flaky(**kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RateLimitError("429")
        return await original(**kwargs)

    client._inner_get_response = flaky
    outcome = await encode_criterion(client, agent_context, agent_context.inventory.by_id("EXC-1"))
    assert outcome.accepted is not None and outcome.attempts == 2


async def test_timeout_is_reported(agent_context, monkeypatch):
    agent_context.settings = agent_context.settings.model_copy(update={"agent_timeout_seconds": 30})

    async def slow(awaitable, timeout):
        awaitable.close()  # the agent run never starts
        raise asyncio.TimeoutError

    monkeypatch.setattr(loop.asyncio, "wait_for", slow)
    outcome = await encode_criterion(client_for(["x"]), agent_context, agent_context.inventory.by_id("EXC-1"))
    assert outcome.accepted is None and outcome.error.startswith("timeout")


def test_tool_schemas_are_plain_json_schema(agent_context):
    import json

    from app.services.ctml.agents.criterion import criterion_tools
    from app.services.ctml.runtime import AgentState

    tools = criterion_tools(agent_context, agent_context.inventory.by_id("EXC-1"), AgentState("EXC-1"))
    for tool in tools:
        assert "discriminator" not in json.dumps(tool.parameters()), tool.name


async def test_design_agent_reads_the_registry_record_only_when_present(agent_context):
    import json

    from app.services.ctml.agents.design import design_tools
    from app.services.ctml.runtime import AgentState

    state = AgentState("design")
    tool = next(t for t in design_tools(agent_context, state) if t.name == "read_registry_record")
    assert "error" in json.loads(await tool.func())
    agent_context.registry_lines = {"R1": "Arm 'A' (EXPERIMENTAL; Drug: Drugex): Drugex 10 mg IV Q3W"}
    agent_context.registry_source = "ClinicalTrials.gov NCT09999999"
    result = json.loads(await tool.func())
    assert result["source"] == "ClinicalTrials.gov NCT09999999" and result["lines"].startswith("R1 | Arm 'A'")
    assert "R1" in state.seen_line_ids
