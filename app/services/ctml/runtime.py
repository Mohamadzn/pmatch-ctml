"""Per-run objects shared by tools, checks and agents."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.ctml.config import Settings
from app.services.ctml.document.glossary import Glossary
from app.services.ctml.document.inventory import Inventory
from app.services.ctml.document.model import Document
from app.services.ctml.document.sections import Routing
from app.services.ctml.registries.ledger import Ledger
from app.services.ctml.registries.oncotree import OncoTreeIndex
from app.services.ctml.registries.terminology import Terminology

MAX_RECORDED_CHARS = 20000


class ToolRecorder:
    """Every tool call of the run, one JSON line each (tool_calls.jsonl)."""

    def __init__(self, path: Path | None = None, keep_existing: bool = False) -> None:
        self._path = path
        self.count = 0
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not (keep_existing and path.exists()):
                path.write_text("", encoding="utf-8")

    def record(
        self, owner: str, tool: str, arguments: Any, result: Any, seconds: float, attempt: int = 1
    ) -> None:
        self.count += 1
        if self._path is None:
            return
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        entry = {
            "owner": owner,
            "attempt": attempt,
            "tool": tool,
            "arguments": arguments,
            "result": text[:MAX_RECORDED_CHARS],
            "result_chars": len(text),
            "seconds": round(seconds, 3),
            "at": time.time(),
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")


@dataclass
class RunContext:
    settings: Settings
    document: Document
    routing: Routing
    inventory: Inventory
    glossary: Glossary
    ledger: Ledger
    terminology: Terminology | None = None
    oncotree: OncoTreeIndex | None = None
    recorder: ToolRecorder = field(default_factory=ToolRecorder)
    run_dir: Path | None = None
    # ClinicalTrials.gov arm and intervention descriptions ("R1": text), for redacted values only.
    registry_lines: dict[str, str] = field(default_factory=dict)
    registry_source: str = ""


@dataclass
class AgentState:
    """State of one agent run. Tools and checks of that run share it; nothing else does."""

    owner: str
    attempt: int = 1
    accepted: Any = None
    accepted_confirmations: list[dict] = field(default_factory=list)
    submits: int = 0
    last_errors: list[dict] = field(default_factory=list)
    seen_line_ids: set[str] = field(default_factory=set)
    lookup_ids: set[str] = field(default_factory=set)
    tests: dict[str, dict] = field(default_factory=dict)
    reminders: int = 0
    tool_calls: int = 0
