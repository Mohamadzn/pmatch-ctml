"""The protocol's own abbreviations, read from its glossary and from inline definitions.

Tier 1: a glossary table row or glossary line ("ANC | absolute neutrophil count").
Tier 2: an inline definition whose letters align ("absolute neutrophil count (ANC)").
Keys are case-sensitive (EGFR and eGFR differ). A conflict is two different tier-1
definitions, or two different tier-2 definitions when no tier-1 entry exists.

Scanned glossaries often come back with rows shifted by one (an abbreviation on one row,
its definition on the next). A row next to such a half-empty row is not trusted, and a
cell holding two abbreviations is skipped. Losing an entry is safer than a wrong one.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from app.services.ctml.document.model import Document
from app.services.ctml.document.sections import Routing, section_lines

_TOKEN = r"[A-Za-z0-9][A-Za-z0-9+\-/.]{0,10}[A-Za-z0-9+\-]"
_INLINE_LONG_FIRST = re.compile(r"\((" + _TOKEN + r")\)")
_INLINE_NESTED = re.compile(r"\(([^()\[\]]{3,200}?)\[\s*(" + _TOKEN + r")\s*\]\s*\)")
_GLOSSARY_LINE = re.compile(r"^(" + _TOKEN + r")\s*(?:[:=\t–-]\s*|\s{2,}|\s)(.{3,200})$")
# A long form never crosses a clause boundary.
_CLAUSE_BREAK = re.compile(r"[()\[\],;:]|\.\s")
_HEADER_CELL = re.compile(r"^(abbreviations?|terms?|abbreviation\s*/\s*term|definitions?|acronyms?)$", re.I)
# Layout words that are never abbreviations.
_LAYOUT_WORDS = {"NOTE", "TABLE", "FIGURE", "SECTION", "APPENDIX", "AND", "OR", "NOT", "PAGE", "PART"}
# English function words that never start a long form.
_FUNCTION_WORDS = {
    "a",
    "an",
    "the",
    "if",
    "of",
    "and",
    "or",
    "with",
    "has",
    "have",
    "in",
    "for",
    "to",
    "on",
    "by",
    "is",
}


@dataclass(frozen=True)
class Definition:
    abbreviation: str
    definition: str
    line_id: str
    tier: int
    page: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Glossary:
    entries: dict[str, list[Definition]] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)

    def lookup(self, abbreviation: str) -> list[Definition]:
        return self.entries.get(abbreviation.strip(), [])

    def for_text(self, text: str) -> list[Definition]:
        """Definitions of the abbreviations that appear in the text, in order of appearance."""
        found: list[tuple[int, Definition]] = []
        for key, definitions in self.entries.items():
            match = re.search(r"(?<![A-Za-z0-9])" + re.escape(key) + r"(?![A-Za-z0-9])", text)
            if match:
                found.extend((match.start(), definition) for definition in definitions)
        return [definition for _, definition in sorted(found, key=lambda item: item[0])]

    def as_dict(self) -> dict:
        return {
            "entries": {key: [d.as_dict() for d in values] for key, values in self.entries.items()},
            "conflicts": self.conflicts,
        }


def is_short_form(token: str) -> bool:
    token = token.strip()
    if not 2 <= len(token) <= 12 or token.upper() in _LAYOUT_WORDS:
        return False
    capitals = sum(ch.isupper() for ch in token)
    digits = sum(ch.isdigit() for ch in token)
    return capitals >= 2 or (capitals >= 1 and digits >= 1)


def align(short_form: str, words: list[str]) -> str | None:
    """Shortest run of preceding words whose letters spell the short form
    (Schwartz-Hearst): each short-form character appears in order, and the first
    one starts a word."""
    characters = [ch.lower() for ch in short_form if ch.isalnum()]
    if not characters:
        return None
    limit = min(len(words), len(characters) + 5)
    for count in range(1, limit + 1):
        candidate = words[-count:]
        text = " ".join(candidate).lower()
        position = len(text) - 1
        index = len(characters) - 1
        while index >= 0 and position >= 0:
            wanted = characters[index]
            at_word_start = position == 0 or not text[position - 1].isalnum()
            if text[position] == wanted and (index > 0 or at_word_start):
                index -= 1
            position -= 1
        if index < 0 and candidate[0][:1].lower() == characters[0]:
            return None if candidate[0].lower() in _FUNCTION_WORDS else " ".join(candidate)
    return None


def _clean_definition(text: str) -> str:
    return " ".join(text.strip(" \t:;,.-–").split())


def trusted_rows(rows: list[list[str]]) -> list[tuple[str, str]]:
    """(abbreviation, definition) pairs from a two-column glossary table.

    A row is used only when both cells are filled, the first cell is one token, and
    neither neighbour is half empty (a sign that the rows around it are shifted).
    """
    pairs = [[cell.strip() for cell in row if cell is not None] for row in rows]
    pairs = [row[:2] if len(row) >= 2 else [*row, ""] for row in pairs]

    def half_empty(index: int) -> bool:
        if not 0 <= index < len(pairs):
            return False
        first, second = pairs[index]
        return bool(first) != bool(second)

    trusted = []
    for index, (first, second) in enumerate(pairs):
        if not first or not second or _HEADER_CELL.match(first) or _HEADER_CELL.match(second):
            continue
        if len(first.split()) != 1 or half_empty(index - 1) or half_empty(index + 1):
            continue
        trusted.append((first, second))
    return trusted


def _clause_words(text_before: str) -> list[str]:
    """Words of the clause that ends right before an opening parenthesis."""
    breaks = list(_CLAUSE_BREAK.finditer(text_before))
    clause = text_before[breaks[-1].end() :] if breaks else text_before
    clause = clause.replace("’s", "").replace("'s", "")
    return re.findall(r"[A-Za-z0-9+\-]+", clause)


def build_glossary(document: Document, routing: Routing) -> Glossary:
    glossary = Glossary()

    def add(abbreviation: str, definition: str, line_id: str, tier: int, page: int) -> None:
        abbreviation = abbreviation.strip()
        definition = _clean_definition(definition)
        if not is_short_form(abbreviation) or len(definition.split()) < 2 or definition == abbreviation:
            return
        current = glossary.entries.setdefault(abbreviation, [])
        if all(d.definition.casefold() != definition.casefold() for d in current):
            current.append(Definition(abbreviation, definition, line_id, tier, page))

    glossary_line_ids: set[str] = set()
    for section in routing.glossary:
        for line in section_lines(document, section, include_heading=False):
            glossary_line_ids.add(line.line_id)
            if line.table_id:
                for abbreviation, definition in trusted_rows(document.tables[line.table_id].rows):
                    add(abbreviation, definition, line.line_id, 1, line.page)
                continue
            match = _GLOSSARY_LINE.match(line.text)
            if match:
                add(match.group(1), match.group(2), line.line_id, 1, line.page)

    for line in document.lines:
        if line.line_id in glossary_line_ids or line.kind in ("table", "running"):
            continue
        text = line.text
        for match in _INLINE_NESTED.finditer(text):
            words = re.findall(r"[A-Za-z0-9+\-/]+", match.group(1))
            aligned = align(match.group(2), words)
            if aligned:
                add(match.group(2), aligned, line.line_id, 2, line.page)
        for match in _INLINE_LONG_FIRST.finditer(text):
            aligned = align(match.group(1), _clause_words(text[: match.start()]))
            if aligned:
                add(match.group(1), aligned, line.line_id, 2, line.page)

    for key, definitions in list(glossary.entries.items()):
        best_tier = min(d.tier for d in definitions)
        best = [d for d in definitions if d.tier == best_tier]
        glossary.entries[key] = best
        distinct = {d.definition.casefold() for d in best}
        if len(distinct) > 1:
            glossary.conflicts.append(f"glossary_conflict:{key}:" + " | ".join(sorted(distinct)))
    return glossary
