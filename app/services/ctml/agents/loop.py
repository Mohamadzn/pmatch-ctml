"""The submit-and-check loop shared by all agents.

An agent reads with its tools and calls its submit tool; the submit tool runs the checks
and returns errors until the output passes. The loop stops at the first accepted submit.

Two guards, found in offline tests of Agent Framework:
- tool_choice "required" applies only to the first model call of a run; after that a model
  can answer in text and end the run with nothing accepted. The same session then gets one
  reminder.
- Several submits can run in one model turn. The tools are async functions on one event loop
  (never worker threads), a submit check runs without interruption, and the submit tool
  refuses every call after an acceptance.
Tools and their state are built fresh for every attempt: max_invocations counters live on
the tool objects and never reset.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_framework import Agent, FunctionTool, MiddlewareTermination, function_middleware
from pydantic import BaseModel

from app.services.ctml.config import Settings
from app.services.ctml.runtime import AgentState, RunContext

logger = logging.getLogger(__name__)

REMINDER = (
    "No accepted submit yet. Fix the errors the submit tool returned and call {submit} again. "
    "Do not answer in text."
)
MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = (20, 60)
_RETRYABLE = {"RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError"}


@dataclass
class AgentSpec:
    name: str
    owner: str
    instructions: str
    first_message: str
    submit_tool: str
    build_tools: Callable[[AgentState], list[FunctionTool]]


@dataclass
class LoopOutcome:
    owner: str
    accepted: Any = None
    confirmations: list[dict] = field(default_factory=list)
    last_errors: list[dict] = field(default_factory=list)
    submits: int = 0
    reminders: int = 0
    attempts: int = 0
    tool_calls: int = 0
    lookup_ids: list[str] = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""

    def summary(self) -> dict:
        return {
            "owner": self.owner,
            "accepted": self.accepted is not None,
            "submits": self.submits,
            "reminders": self.reminders,
            "attempts": self.attempts,
            "tool_calls": self.tool_calls,
            "confirmations": len(self.confirmations),
            "seconds": round(self.seconds, 1),
            "error": self.error,
        }


def run_options(settings: Settings) -> dict:
    options: dict[str, Any] = {"tool_choice": "required", "store": False}
    if settings.reasoning_effort:
        options["reasoning"] = {"effort": settings.reasoning_effort}
    return options


def _result_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, list):
        return "\n".join(str(getattr(item, "text", None) or item) for item in result)
    return str(getattr(result, "text", None) or result)


def _arguments(arguments: Any) -> Any:
    if isinstance(arguments, BaseModel):
        return arguments.model_dump(mode="json")
    try:
        return json.loads(json.dumps(dict(arguments), default=str))
    except (TypeError, ValueError):
        return str(arguments)


def _retryable(error: BaseException) -> bool:
    seen = 0
    current: BaseException | None = error
    while current is not None and seen < 6:
        if type(current).__name__ in _RETRYABLE:
            return True
        current = getattr(current, "inner_exception", None) or current.__cause__
        seen += 1
    return False


def supervising_middleware(context: RunContext, state: AgentState, submit_tool: str):
    """Records every tool call, and ends the loop after an accepted submit."""

    @function_middleware
    async def supervise(call, call_next):
        started = time.monotonic()
        try:
            await call_next()
        finally:
            state.tool_calls += 1
            context.recorder.record(
                state.owner,
                call.function.name,
                _arguments(call.arguments),
                _result_text(call.result),
                time.monotonic() - started,
                state.attempt,
            )
        if call.function.name == submit_tool and state.accepted is not None:
            raise MiddlewareTermination(result=call.result)

    return supervise


def _outcome(spec: AgentSpec, state: AgentState, started: float, error: str = "") -> LoopOutcome:
    return LoopOutcome(
        owner=spec.owner,
        accepted=state.accepted,
        confirmations=state.accepted_confirmations,
        last_errors=state.last_errors,
        submits=state.submits,
        reminders=state.reminders,
        attempts=state.attempt,
        tool_calls=state.tool_calls,
        lookup_ids=sorted(state.lookup_ids),
        seconds=time.monotonic() - started,
        error=error,
    )


async def run_submit_loop(client: Any, spec: AgentSpec, context: RunContext) -> LoopOutcome:
    settings = context.settings
    started = time.monotonic()
    state = AgentState(owner=spec.owner)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        state = AgentState(owner=spec.owner, attempt=attempt)
        tools = spec.build_tools(state)
        agent = Agent(
            client,
            spec.instructions,
            name=spec.name,
            tools=tools,
            middleware=[supervising_middleware(context, state, spec.submit_tool)],
        )
        session = agent.create_session()
        options = run_options(settings)
        try:
            await asyncio.wait_for(
                agent.run(spec.first_message, session=session, options=options),
                settings.agent_timeout_seconds,
            )
            if state.accepted is None:
                state.reminders += 1
                reminder = REMINDER.format(submit=spec.submit_tool)
                await asyncio.wait_for(
                    agent.run(reminder, session=session, options=options), settings.agent_timeout_seconds
                )
            return _outcome(spec, state, started)
        except asyncio.TimeoutError:
            logger.warning("%s: agent timed out after %ss", spec.owner, settings.agent_timeout_seconds)
            return _outcome(spec, state, started, f"timeout after {settings.agent_timeout_seconds}s")
        except Exception as error:  # the model service can fail in many ways; the run must go on
            if state.accepted is not None:
                return _outcome(spec, state, started)
            if _retryable(error) and attempt < MAX_ATTEMPTS:
                wait = RETRY_WAIT_SECONDS[min(attempt - 1, len(RETRY_WAIT_SECONDS) - 1)]
                logger.warning(
                    "%s: model service error (%s); retrying in %ss", spec.owner, type(error).__name__, wait
                )
                await asyncio.sleep(wait)
                continue
            logger.error("%s: agent failed: %s", spec.owner, error)
            return _outcome(spec, state, started, f"{type(error).__name__}: {str(error)[:500]}")
    return _outcome(spec, state, started, "no attempts left")


def submit_response(state: AgentState, problems: list[dict], accepted: list[dict], value: Any) -> str:
    """Record a submit and return what the model sees."""
    state.submits += 1
    if problems:
        state.last_errors = problems
        rejects = [p for p in problems if p["kind"] == "reject"]
        confirms = [p for p in problems if p["kind"] == "confirm"]
        return json.dumps(
            {
                "accepted": False,
                "errors": rejects,
                "confirm_or_change": confirms,
                "note": "Fix every error. For each confirm_or_change item, change the output or add a "
                "confirmation {code, path, quote} with exact source words.",
            }
        )
    state.accepted = value
    state.accepted_confirmations = accepted
    return json.dumps({"accepted": True})


def already_accepted() -> str:
    return json.dumps(
        {
            "accepted": False,
            "errors": [{"code": "already_accepted", "hint": "Stop. Your output was accepted."}],
        }
    )
