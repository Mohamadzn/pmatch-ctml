"""Chat clients that play the model offline, for tests. No network call."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from agent_framework import BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, Message

Step = list[tuple[str, dict]] | str


class ScriptedClient(FunctionInvocationLayer, BaseChatClient):
    """Each model turn returns scripted tool calls [(name, args), ...] or a text reply.
    A step can also be a function of the tool results seen so far."""

    def __init__(self, script: list[Step], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.script = script
        self.turn = 0
        self.seen_results: list[str] = []
        self.tool_choices: list[Any] = []

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.tool_choices.append(options.get("tool_choice"))
        for content in messages[-1].contents or []:
            if content.type == "function_result":
                self.seen_results.append(str(content.result))
        step = self.script[self.turn] if self.turn < len(self.script) else "done"
        if callable(step):
            step = step(self.seen_results)
        self.turn += 1
        return reply(step, self.turn)


def reply(step: Step, turn: int) -> ChatResponse:
    if isinstance(step, str):
        return ChatResponse(messages=[Message("assistant", [Content.from_text(step)])])
    calls = [
        Content.from_function_call(call_id=f"t{turn}_{index}", name=name, arguments=json.dumps(args))
        for index, (name, args) in enumerate(step)
    ]
    return ChatResponse(messages=[Message("assistant", calls)])


class PolicyClient(FunctionInvocationLayer, BaseChatClient):
    """Plays several agents at once: policy(instructions, first_message, results, turn) -> step.

    results holds the text of every tool result seen so far in that conversation, oldest first.
    """

    def __init__(self, policy: Callable[[str, str, list[str], int], Step], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.policy = policy
        self.turns = 0

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.turns += 1
        instructions = str(options.get("instructions") or "")
        first = next((m.text for m in messages if m.role == "user"), "")
        results = [
            str(content.result)
            for message in messages
            for content in message.contents or []
            if content.type == "function_result"
        ]
        turn = sum(1 for m in messages if m.role == "assistant")
        return reply(self.policy(instructions, first, results, turn), self.turns)


def line_id_of(message: str, fragment: str) -> str:
    """The ID of the first rendered line ("L12 | text") that contains the fragment."""
    for line in message.splitlines():
        match = re.match(r"^(L\d+) \| (.*)$", line)
        if match and fragment in match.group(2):
            return match.group(1)
    raise AssertionError(f"line not found: {fragment}")


def last_lookup_id(results: list[str]) -> str:
    for text in reversed(results):
        match = re.search(r'"lookup_id": "(lk-\d+)"', text)
        if match:
            return match.group(1)
    raise AssertionError("no lookup in results")
