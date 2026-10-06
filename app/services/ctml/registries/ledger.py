"""Every registry lookup of a run, with an ID that leaves cite as mapping evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class LookupRecord:
    lookup_id: str
    registry: str  # "oncotree", "ncit", "hgnc" or "clinicaltrials"
    query: str
    owner: str  # criterion ID, "design" or "metadata"
    status: str  # "ok", "no_match" or "error"
    candidates: list[dict] = field(default_factory=list)
    source: str = "live"  # "live", "cache" or "snapshot"
    error: str = ""
    note: str = ""
    created_at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def names(self) -> set[str]:
        """Every name this lookup returned: candidate names and their parents' names."""
        found: set[str] = set()
        for candidate in self.candidates:
            for key in ("name", "symbol"):
                if candidate.get(key):
                    found.add(str(candidate[key]))
            for parent in candidate.get("parents") or []:
                if isinstance(parent, dict) and parent.get("name"):
                    found.add(str(parent["name"]))
        return found


class Ledger:
    def __init__(self, path: Path | None = None, keep_existing: bool = False) -> None:
        """keep_existing: continue the ledger already in the file (a resumed run)."""
        self._records: dict[str, LookupRecord] = {}
        self._path = path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if keep_existing and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = LookupRecord(**json.loads(line))
                    self._records[record.lookup_id] = record
        else:
            path.write_text("", encoding="utf-8")

    def add(
        self,
        registry: str,
        query: str,
        owner: str,
        status: str,
        candidates: list[dict] | None = None,
        source: str = "live",
        error: str = "",
        note: str = "",
    ) -> LookupRecord:
        record = LookupRecord(
            lookup_id=f"lk-{len(self._records) + 1:04d}",
            registry=registry,
            query=query,
            owner=owner,
            status=status,
            candidates=candidates or [],
            source=source,
            error=error,
            note=note,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._records[record.lookup_id] = record
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.as_dict()) + "\n")
        return record

    def get(self, lookup_id: str) -> LookupRecord | None:
        return self._records.get(lookup_id)

    def for_owner(self, owner: str) -> list[LookupRecord]:
        return [record for record in self._records.values() if record.owner == owner]

    def all(self) -> list[LookupRecord]:
        return list(self._records.values())

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for record in self._records.values():
            key = f"{record.registry}:{record.source}"
            result[key] = result.get(key, 0) + 1
        return result
