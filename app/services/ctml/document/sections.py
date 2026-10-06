"""Section tree and deterministic routing (no retrieval, no model).

Headings come from Document Intelligence markdown. Numbered headings build the tree; a
numbered heading that breaks the document's numbering order (for example a list item that
Document Intelligence marked as a heading) is demoted to text. Headings on table-of-contents
pages are ignored. Routing uses the protocol's own section titles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import pairwise

from app.services.ctml.document.model import Document, Line

_NUMBERED_HEADING = re.compile(r"^(?:section\s+)?(\d{1,2}(?:\.\d{1,2}){0,6})\.?(?:\s+|$)(.*)$", re.I)
_TOC_ENTRY = re.compile(r"(?:\.{4,}|…{2,})\s*\d{1,3}$|^\d{1,2}(?:\.\d{1,2})*\.?\s+\S.*\s\d{1,3}$")
_TOC_TITLE = re.compile(r"^(?:table\s+of\s+)?contents$", re.I)
TOC_MIN_ENTRIES = 6
TOC_MIN_SHARE = 0.4

# Section-title words used for routing. They describe protocol layout, not medical terms.
_ELIGIBILITY = re.compile(r"\b(inclusion|exclusion|eligibility)\b", re.I)
# "Inclusion Criteria" or "Eligibility", not "Inclusion of Women and Minorities".
_CRITERIA_TITLE = re.compile(r"\b(criteria|criterion|eligibility|eligible)\b", re.I)
_INCLUSION = re.compile(r"\binclusion\b", re.I)
_EXCLUSION = re.compile(r"\bexclusion\b", re.I)
_SYNOPSIS = re.compile(
    r"\b(synopsis|protocol summary|trial summary|study summary|summary of the study)\b", re.I
)
_DESIGN = re.compile(
    r"\b(study design|trial design|overall design|design of the study|schema|study plan)\b", re.I
)
_TREATMENT = re.compile(
    r"\b(study treatments?|trial treatments?|study interventions?|investigational (medicinal )?products?|"
    r"study drugs?|treatments? administered|dosing|dose selection|dose escalation|dose levels?|"
    r"treatment plan|treatment arms?|cohorts?)\b",
    re.I,
)
_GLOSSARY = re.compile(r"\b(abbreviations|glossary|definitions? of terms|acronyms)\b", re.I)
_HISTORICAL = re.compile(
    r"\b(summary of changes|document history|revision (history|chronology)|amendment (history|summary)|"
    r"protocol amendment summary|list of changes|protocol history|version history)\b",
    re.I,
)


@dataclass(frozen=True)
class Section:
    section_id: str
    number: str
    title: str
    start: int  # index of the heading line in Document.lines
    end: int  # exclusive
    page: int
    depth: int
    historical: bool = False

    def label(self) -> str:
        return f"{self.number} {self.title}".strip()


@dataclass(frozen=True)
class EligibilityRegion:
    section: Section
    kind: str  # "inclusion", "exclusion" or "mixed"


@dataclass
class Routing:
    sections: list[Section]
    eligibility: list[EligibilityRegion]
    design: list[Section]
    glossary: list[Section]
    contents_pages: set[int]
    demoted_headings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def outline(self) -> list[dict]:
        return [
            {"section_id": s.section_id, "title": s.label(), "page": s.page, "lines": s.end - s.start}
            for s in self.sections
            if not s.historical
        ]

    def section(self, section_id: str) -> Section | None:
        return next((s for s in self.sections if s.section_id == section_id), None)


def parse_heading(text: str) -> tuple[str, str]:
    """("5.1.2", "Subject Inclusion Criteria") for a numbered heading, ("", text) otherwise."""
    match = _NUMBERED_HEADING.match(text.strip())
    if not match:
        return "", text.strip()
    return match.group(1), match.group(2).strip()


def _number_key(number: str) -> tuple[int, ...]:
    return tuple(int(part) for part in number.split("."))


def _contents_rows(rows: list[list[str]]) -> int:
    """Rows of a table that reads as a table of contents: a title, then a page number
    that never decreases down the table (a dose or schedule table fails this)."""
    numbers = []
    for row in rows:
        cells = [cell.strip() for cell in row if cell and cell.strip()]
        titled = any(re.search(r"[A-Za-z]{3}", cell) for cell in cells[:-1])
        if len(cells) >= 2 and titled and re.fullmatch(r"\d{1,3}", cells[-1]):
            numbers.append(int(cells[-1]))
    pairs = list(pairwise(numbers))
    ordered = sum(a <= b for a, b in pairs) >= 0.8 * len(pairs)
    return len(numbers) if ordered and len(set(numbers)) >= 3 else 0


def contents_pages(document: Document) -> set[int]:
    """Pages that hold a table of contents (entries ending in page numbers)."""
    pages: set[int] = set()
    by_page: dict[int, list[Line]] = {}
    for line in document.lines:
        by_page.setdefault(line.page, []).append(line)
    for page, lines in by_page.items():
        entries = sum(1 for line in lines if _TOC_ENTRY.search(line.text))
        for line in lines:
            if line.table_id:
                entries += _contents_rows(document.tables[line.table_id].rows)
        texts = [line for line in lines if line.kind != "table"]
        share = entries / max(len(texts), 1)
        titled = any(_TOC_TITLE.match(line.text.strip()) for line in lines)
        if entries >= TOC_MIN_ENTRIES and (share >= TOC_MIN_SHARE or titled):
            pages.add(page)
    return pages


def _longest_ordered(candidates: list[tuple[int, tuple[int, ...]]]) -> set[int]:
    """Indexes of the longest subsequence whose section numbers never decrease."""
    count = len(candidates)
    best = [1] * count
    previous = [-1] * count
    for i in range(count):
        for j in range(i):
            if candidates[j][1] <= candidates[i][1] and best[j] + 1 > best[i]:
                best[i], previous[i] = best[j] + 1, j
    if not count:
        return set()
    index = max(range(count), key=best.__getitem__)
    kept = set()
    while index != -1:
        kept.add(candidates[index][0])
        index = previous[index]
    return kept


def build_sections(document: Document, toc_pages: set[int]) -> tuple[list[Section], list[str]]:
    """Sections from headings; returns (sections, demoted heading texts)."""
    numbered: list[tuple[int, tuple[int, ...]]] = []
    heading_positions: list[int] = []
    for index, line in enumerate(document.lines):
        if line.kind != "heading" or line.page in toc_pages:
            continue
        heading_positions.append(index)
        number, _ = parse_heading(line.text)
        if number:
            numbered.append((index, _number_key(number)))
    kept_numbered = _longest_ordered(numbered)
    demoted = [document.lines[i].text for i, _ in numbered if i not in kept_numbered]
    headings = [
        i for i in heading_positions if i in kept_numbered or not parse_heading(document.lines[i].text)[0]
    ]

    raw: list[tuple[int, str, str, int, int]] = []  # index, number, title, depth, end
    for order, index in enumerate(headings):
        number, title = parse_heading(document.lines[index].text)
        depth = len(number.split(".")) if number else 0
        end = len(document.lines)
        for later in headings[order + 1 :]:
            later_number = parse_heading(document.lines[later].text)[0]
            if not number or (later_number and len(later_number.split(".")) <= depth):
                end = later
                break
        raw.append((index, number, title, depth, end))

    # A history block (summary of changes, revision chronology) runs to the next numbered
    # heading when its own heading is unnumbered; everything inside it is historical.
    historical_ranges = []
    for index, number, title, _, end in raw:
        if _HISTORICAL.search(title):
            stop = end
            if not number:
                stop = next((i for i, n, _, _, _ in raw if n and i > index), len(document.lines))
            historical_ranges.append((index, stop))

    sections = [
        Section(
            f"S{position + 1}",
            number,
            title,
            index,
            end,
            document.lines[index].page,
            depth,
            any(low <= index < high for low, high in historical_ranges),
        )
        for position, (index, number, title, depth, end) in enumerate(raw)
    ]
    return sections, demoted


def _is_subheading(text: str) -> bool:
    words = text.split()
    return 0 < len(words) <= 4 and not text.rstrip().endswith((".", ":", ";", ","))


def _extend_unnumbered(document: Document, section: Section, sections: list[Section]) -> Section:
    """An unnumbered eligibility heading keeps short subheadings (for example
    "Medical Conditions") inside its region instead of ending at them."""
    if section.number:
        return section
    level = document.lines[section.start].heading_level
    end = len(document.lines)
    for other in sections:
        if other.start <= section.start:
            continue
        line = document.lines[other.start]
        if _ELIGIBILITY.search(other.title) or other.number:
            end = other.start
            break
        if line.heading_level <= level and not _is_subheading(other.title):
            end = other.start
            break
    return Section(
        section.section_id, "", section.title, section.start, end, section.page, 0, section.historical
    )


def route(document: Document) -> Routing:
    toc = contents_pages(document)
    sections, demoted = build_sections(document, toc)
    current = [s for s in sections if not s.historical]

    eligibility: list[EligibilityRegion] = []
    for section in current:
        if not (_ELIGIBILITY.search(section.title) and _CRITERIA_TITLE.search(section.title)):
            continue
        inclusion = bool(_INCLUSION.search(section.title))
        exclusion = bool(_EXCLUSION.search(section.title))
        kind = (
            "inclusion"
            if inclusion and not exclusion
            else "exclusion"
            if exclusion and not inclusion
            else "mixed"
        )
        eligibility.append(EligibilityRegion(_extend_unnumbered(document, section, current), kind))
    # "5 Eligibility" holding "5.1 Inclusion" and "5.2 Exclusion": keep the specific inner
    # regions. A region inside an outer region of the same kind is covered by the outer one.
    kept: list[EligibilityRegion] = []
    for region in eligibility:
        start, end = region.section.start, region.section.end
        inner = [o for o in eligibility if o is not region and start < o.section.start < end]
        outer = [o for o in eligibility if o is not region and o.section.start < start < o.section.end]
        if region.kind == "mixed" and any(o.kind != "mixed" for o in inner):
            continue
        if any(o.kind == region.kind for o in outer):
            continue
        kept.append(region)

    # A synopsis often repeats the key criteria. When the full criteria exist elsewhere, the
    # synopsis copy is skipped (and reported).
    notes: list[str] = []
    synopsis = [(s.start, s.end) for s in current if _SYNOPSIS.search(s.title)]

    def in_synopsis(region: EligibilityRegion) -> bool:
        return any(low < region.section.start < high for low, high in synopsis)

    full_kinds = {r.kind for r in kept if not in_synopsis(r)}
    regions = []
    for region in kept:
        if in_synopsis(region) and (region.kind in full_kinds or (region.kind == "mixed" and full_kinds)):
            notes.append(f"synopsis_criteria_skipped:{region.section.label()}")
            continue
        regions.append(region)

    design = [
        s
        for s in current
        if (_SYNOPSIS.search(s.title) or _DESIGN.search(s.title) or _TREATMENT.search(s.title))
        and not _ELIGIBILITY.search(s.title)
    ]
    glossary = [s for s in sections if _GLOSSARY.search(s.title)]
    return Routing(sections, regions, design, glossary, toc, demoted, notes)


def section_lines(document: Document, section: Section, include_heading: bool = True) -> list[Line]:
    start = section.start if include_heading else section.start + 1
    return document.lines[start : section.end]
