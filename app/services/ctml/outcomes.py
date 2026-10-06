"""Agent outcomes saved in the run folder, and the runner that starts agents.

Every agent outcome is written to the run folder. With resume, an accepted outcome whose
fingerprint matches the current input is reused and the agent is not started again. The
runner's semaphore bounds how many agents talk to the model at the same time.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.services.ctml.agents.loop import LoopOutcome
from app.services.ctml.document.inventory import Criterion

logger = logging.getLogger(__name__)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def criterion_fingerprint(criterion: Criterion) -> str:
    """Changes when the criterion's text, kind or population changes (checked on resume)."""
    key = json.dumps([criterion.kind, criterion.scope_label, " ".join(criterion.text.split())])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def failure_reason(outcome: LoopOutcome) -> str:
    if outcome.error:
        return outcome.error
    if outcome.last_errors:
        return "last errors: " + "; ".join(e.get("code", "") for e in outcome.last_errors[:5])
    return f"no valid submit after {outcome.submits} submits and {outcome.reminders} reminder(s)"


def save_outcome(path: Path, outcome: LoopOutcome, fingerprint: str = "") -> None:
    accepted = outcome.accepted.model_dump(mode="json") if outcome.accepted is not None else None
    write_json(
        path,
        {
            "owner": outcome.owner,
            "fingerprint": fingerprint,
            "accepted": accepted,
            "confirmations": outcome.confirmations,
            "last_errors": outcome.last_errors,
            "lookup_ids": outcome.lookup_ids,
            "summary": outcome.summary(),
        },
    )


def load_outcome(path: Path, model, fingerprint: str = "") -> LoopOutcome | None:
    """A saved accepted outcome, or None when absent, not accepted or made for other input."""
    if not path.exists():
        return None
    data = read_json(path)
    if not data.get("accepted") or data.get("fingerprint", "") != fingerprint:
        return None
    outcome = LoopOutcome(owner=data["owner"], accepted=model.model_validate(data["accepted"]))
    outcome.confirmations = data.get("confirmations", [])
    outcome.lookup_ids = data.get("lookup_ids", [])
    outcome.error = "reused from an earlier attempt in this run folder"
    return outcome


class AgentRunner:
    """Starts agents under one concurrency limit, saving and (on resume) reusing their outcomes."""

    def __init__(self, max_parallel: int, resume: bool) -> None:
        self.semaphore = asyncio.Semaphore(max_parallel)
        self.resume = resume

    async def run(
        self,
        path: Path,
        model,
        factory: Callable[[], Awaitable[LoopOutcome]],
        fingerprint: str = "",
    ) -> LoopOutcome:
        if self.resume:
            saved = load_outcome(path, model, fingerprint)
            if saved is not None:
                logger.info("%s: reusing the accepted output saved in this run folder", saved.owner)
                return saved
        async with self.semaphore:
            outcome = await factory()
        save_outcome(path, outcome, fingerprint)
        status = (
            "accepted"
            if outcome.accepted is not None
            else f"NOT accepted ({outcome.error or 'no valid submit'})"
        )
        logger.info(
            "%s: %s after %s submits, %s tool calls",
            outcome.owner,
            status,
            outcome.submits,
            outcome.tool_calls,
        )
        return outcome
