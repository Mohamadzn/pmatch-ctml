"""Shared helpers for the submit checks.

Every problem is a dict {code, path, hint, kind}. kind "reject": the agent must change the
output. kind "confirm": the pattern is usually wrong but can be right; the agent either
changes the output or resubmits with a confirmation {code, path, quote} whose quote is an
exact phrase of the source.
"""

from __future__ import annotations

import difflib
import re

from app.services.ctml.document.model import Document
from app.services.ctml.text import comparison_key, contains_phrase

HINT_QUOTE_CHARS = 200
MIN_QUOTE_WORDS = 3
MIN_QUOTE_CHARS = 12


def reject(code: str, path: str, hint: str) -> dict:
    return {"code": code, "path": path, "hint": hint, "kind": "reject"}


def confirm(code: str, path: str, hint: str) -> dict:
    return {"code": code, "path": path, "hint": hint, "kind": "confirm"}


def closest_span(source: str, phrase: str) -> str:
    """The source line most similar to the phrase, for error hints."""
    target = comparison_key(phrase)
    best, best_ratio = "", 0.0
    for line in source.splitlines():
        ratio = difflib.SequenceMatcher(None, target, comparison_key(line)).ratio()
        if ratio > best_ratio:
            best, best_ratio = line, ratio
    return best[:HINT_QUOTE_CHARS]


def line_texts(document: Document, line_ids: list[str] | set[str]) -> str:
    """Text of the given lines; a table line brings its whole table."""
    parts = []
    for line_id in line_ids:
        if not document.has_line(line_id):
            continue
        line = document.line(line_id)
        if line.table_id and line.table_id in document.tables:
            parts.append(document.tables[line.table_id].render())
        else:
            parts.append(line.text)
    return "\n".join(parts)


def unknown_lines(document: Document, line_ids: list[str]) -> list[str]:
    return [line_id for line_id in line_ids if not document.has_line(line_id)]


def apply_confirmations(
    problems: list[dict], confirmations: list, source: str
) -> tuple[list[dict], list[dict]]:
    """(remaining problems, accepted confirmations). A confirm problem is cleared by a
    confirmation with the same code and path whose quote is in the source."""
    remaining: list[dict] = []
    accepted: list[dict] = []
    used = set()
    for index, confirmation in enumerate(confirmations):
        words = confirmation.quote.split()
        if len(words) < MIN_QUOTE_WORDS or len(confirmation.quote.strip()) < MIN_QUOTE_CHARS:
            remaining.append(
                reject(
                    "confirmation_quote_too_short",
                    f"/confirmations/{index}",
                    f"Quote at least {MIN_QUOTE_WORDS} words that state the pattern.",
                )
            )
            used.add(index)
        elif not contains_phrase(source, confirmation.quote):
            remaining.append(
                reject(
                    "confirmation_quote_not_in_source",
                    f"/confirmations/{index}",
                    "The quote must be exact source words. Closest: "
                    + closest_span(source, confirmation.quote),
                )
            )
            used.add(index)
    for problem in problems:
        if problem["kind"] != "confirm":
            remaining.append(problem)
            continue
        match = next(
            (
                (index, c)
                for index, c in enumerate(confirmations)
                if index not in used and c.code == problem["code"] and c.path == problem["path"]
            ),
            None,
        )
        if match is None:
            remaining.append(problem)
        else:
            used.add(match[0])
            accepted.append({**problem, "quote": match[1].quote})
    return remaining, accepted


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> set[str]:
    """Numbers as written, with thousands separators removed ("1,200" -> "1200")."""
    found = set()
    for raw in _NUMBER.findall(text):
        value = raw.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", raw) else raw.replace(",", ".")
        found.add(value.rstrip(".").lstrip("0") or "0")
    return found
