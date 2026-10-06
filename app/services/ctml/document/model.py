"""The protocol as numbered lines and tables, built from a Document Intelligence layout result.

Every non-empty body line gets a run-stable ID ("L1", "L2", ...). Tables become one
placeholder line ("[Table T3] caption") plus a cell grid. Document Intelligence markdown
separates paragraphs with a blank line and keeps the printed line breaks inside a
paragraph; each line records whether it starts a paragraph. Running headers and footers
(named by Document Intelligence in HTML comments, or repeated at the top or bottom of many
pages) are kept once, where they first appear, as kind "running": they can hold header
fields such as the protocol number, but they never join a criterion or a section.
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.services.ctml.text import clean_line

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HEADING = re.compile(r"^(#{1,12})\s+(.*)$")
_FIGURE_OPEN = re.compile(r"^\s*<figure\b", re.I)
_FIGURE_CLOSE = re.compile(r"</figure>\s*$", re.I)
_NUMBERED = re.compile(r"^\d")
_NON_LETTERS = re.compile(r"[\W\d_]+")
_RUNNING_COMMENT = re.compile(r'<!--\s*Page(?:Header|Footer)="(.*?)"\s*-->', re.S)
# A running line must repeat at the top or bottom of this share of pages to count as noise.
RUNNING_LINE_PAGE_SHARE = 0.3
RUNNING_LINE_MIN_PAGES = 3
EDGE_LINES = 3
# A page-edge line that is a part of a running header ("Clinical Study Protocol - 3.0" when the
# header is "Clinical Study Protocol - 3.0 Drug X - D1234") needs at least this many letters.
MIN_RUNNING_PART_LETTERS = 8


@dataclass(frozen=True)
class Line:
    line_id: str
    text: str
    page: int
    kind: str  # "heading", "text", "table", "figure" or "running"
    offset: int
    heading_level: int = 0
    table_id: str | None = None
    # False for the second and later printed lines of one paragraph.
    starts_paragraph: bool = True


@dataclass(frozen=True)
class Table:
    table_id: str
    caption: str
    rows: list[list[str]]
    page: int
    line_id: str

    def render(self, max_rows: int | None = None) -> str:
        rows = self.rows if max_rows is None else self.rows[:max_rows]
        body = "\n".join(" | ".join(cell for cell in row) for row in rows)
        more = (
            ""
            if max_rows is None or len(self.rows) <= max_rows
            else f"\n... {len(self.rows) - max_rows} more rows"
        )
        title = f"[Table {self.table_id}] {self.caption}".strip()
        return f"{title}\n{body}{more}"


@dataclass
class Document:
    source_sha256: str
    page_count: int
    lines: list[Line]
    tables: dict[str, Table]
    removed_running_lines: list[str] = field(default_factory=list)
    _positions: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._positions = {line.line_id: index for index, line in enumerate(self.lines)}

    def position(self, line_id: str) -> int:
        return self._positions[line_id]

    def has_line(self, line_id: str) -> bool:
        return line_id in self._positions

    def line(self, line_id: str) -> Line:
        return self.lines[self._positions[line_id]]

    def page_lines(self, first: int, last: int) -> list[Line]:
        return [line for line in self.lines if first <= line.page <= last]

    def text_of(self, line_ids: list[str]) -> str:
        return "\n".join(self.line(line_id).text for line_id in line_ids if self.has_line(line_id))

    @staticmethod
    def render(lines: list[Line]) -> str:
        return "\n".join(f"{line.line_id} | {line.text}" for line in lines)


def _page_starts(result: dict) -> tuple[list[int], list[int]]:
    starts, numbers = [], []
    for page in result.get("pages", []):
        spans = page.get("spans") or []
        if spans:
            starts.append(int(spans[0].get("offset", 0)))
            numbers.append(int(page.get("pageNumber", len(numbers) + 1)))
    order = sorted(range(len(starts)), key=starts.__getitem__)
    return [starts[i] for i in order], [numbers[i] for i in order]


def _table_grid(table: dict) -> list[list[str]]:
    rows = int(table.get("rowCount") or 0)
    columns = int(table.get("columnCount") or 0)
    for cell in table.get("cells", []):
        rows = max(rows, int(cell.get("rowIndex", 0)) + 1)
        columns = max(columns, int(cell.get("columnIndex", 0)) + 1)
    grid = [["" for _ in range(columns)] for _ in range(rows)]
    for cell in table.get("cells", []):
        grid[int(cell.get("rowIndex", 0))][int(cell.get("columnIndex", 0))] = clean_line(
            str(cell.get("content", ""))
        )
    return [row for row in grid if any(row)]


def _table_caption(table: dict) -> str:
    caption = table.get("caption")
    if isinstance(caption, dict):
        return clean_line(str(caption.get("content", "")))
    return clean_line(str(caption or ""))


def running_key(text: str) -> str:
    """Key for running headers and footers: letters only, case-folded."""
    return _NON_LETTERS.sub("#", text.casefold()).strip("#")


def _running_keys(lines: list[Line], page_count: int) -> set[str]:
    """Texts repeated at the top or bottom of many pages."""
    by_page: dict[int, list[Line]] = defaultdict(list)
    for line in lines:
        if line.kind != "table":
            by_page[line.page].append(line)
    pages_seen: dict[str, set[int]] = defaultdict(set)
    for page, page_lines in by_page.items():
        edge = page_lines[:EDGE_LINES] + page_lines[-EDGE_LINES:]
        for line in edge:
            pages_seen[running_key(line.text)].add(page)
    threshold = max(RUNNING_LINE_MIN_PAGES, int(page_count * RUNNING_LINE_PAGE_SHARE))
    return {key for key, pages in pages_seen.items() if key and len(pages) >= threshold}


def _edge_indexes(draft: list[tuple]) -> set[int]:
    """Indexes of body lines among the first or last EDGE_LINES body lines of their page."""
    by_page: dict[int, list[int]] = defaultdict(list)
    for index, (_, page, kind, *_rest) in enumerate(draft):
        if kind in ("text", "heading"):
            by_page[page].append(index)
    edges: set[int] = set()
    for indexes in by_page.values():
        edges.update(indexes[:EDGE_LINES] + indexes[-EDGE_LINES:])
    return edges


def _running_part(key: str, running: set[str]) -> bool:
    """True when the key is a whole-word part of a longer running header or footer."""
    if len(key.replace("#", "")) < MIN_RUNNING_PART_LETTERS:
        return False
    return any(key != full and f"#{key}#" in f"#{full}#" for full in running)


def build_document(
    result: dict,
    source_sha256: str,
    page_range: tuple[int, int] | None = None,
) -> Document:
    """Turn a layout result (markdown content) into numbered lines and tables."""
    content = str(result.get("content", ""))
    starts, numbers = _page_starts(result)

    def page_of(offset: int) -> int:
        if not starts:
            return 1
        index = bisect.bisect_right(starts, offset) - 1
        return numbers[max(index, 0)]

    table_spans = []
    for index, table in enumerate(result.get("tables", [])):
        for span in table.get("spans") or []:
            offset = int(span.get("offset", 0))
            table_spans.append((offset, offset + int(span.get("length", 0)), index))
    table_spans.sort()
    span_starts = [span[0] for span in table_spans]

    def table_at(offset: int) -> int | None:
        index = bisect.bisect_right(span_starts, offset) - 1
        if index >= 0 and table_spans[index][0] <= offset < table_spans[index][1]:
            return table_spans[index][2]
        return None

    # text, page, kind, offset, table index or heading level, starts a paragraph
    draft: list[tuple[str, int, str, int, int | None, bool]] = []
    emitted_tables: set[int] = set()
    in_figure = False
    position = 0
    # Without any blank line the content carries no paragraph information: every line
    # then counts as the start of a paragraph.
    has_paragraphs = "\n\n" in content
    new_paragraph = True
    for raw in content.split("\n"):
        offset = position
        position += len(raw) + 1
        table_index = table_at(offset + len(raw) - len(raw.lstrip()))
        if table_index is not None:
            if table_index not in emitted_tables:
                emitted_tables.add(table_index)
                draft.append(("", page_of(offset), "table", offset, table_index, True))
            new_paragraph = True
            continue
        for header in _RUNNING_COMMENT.finditer(raw):
            header_text = clean_line(header.group(1))
            if header_text:
                draft.append((header_text, page_of(offset), "running", offset, None, True))
        line = _COMMENT.sub(" ", raw)
        if _FIGURE_OPEN.match(line):
            in_figure = True
        closing = bool(_FIGURE_CLOSE.search(line))
        heading = _HEADING.match(line.strip())
        text = clean_line(heading.group(2) if heading else line)
        if text:
            kind = "heading" if heading else ("figure" if in_figure else "text")
            level = len(heading.group(1)) if heading else 0
            paragraph_start = new_paragraph or not has_paragraphs or bool(heading)
            draft.append((text, page_of(offset), kind, offset, level if heading else None, paragraph_start))
            new_paragraph = bool(heading)
        else:
            new_paragraph = True  # a blank line, a comment or a page break ends the paragraph
        if closing:
            in_figure = False

    page_count = max(numbers) if numbers else 1
    provisional = [
        Line("", text, page, kind, offset, heading_level=(extra or 0) if kind == "heading" else 0)
        for text, page, kind, offset, extra, _ in draft
        if kind != "table"
    ]
    running = _running_keys(provisional, page_count)
    # Document Intelligence names page headers and footers in comments; the same text
    # in the body (missed on some pages) is running text too.
    running |= {
        running_key(clean_line(match.group(1)))
        for match in _RUNNING_COMMENT.finditer(content)
        if running_key(clean_line(match.group(1)))
    }

    edges = _edge_indexes(draft)
    lines: list[Line] = []
    tables: dict[str, Table] = {}
    removed: list[str] = []
    kept_running: set[str] = set()
    for index, (text, page, kind, offset, extra, paragraph_start) in enumerate(draft):
        if page_range and not page_range[0] <= page <= page_range[1]:
            continue
        line_id = f"L{len(lines) + 1}"
        if kind == "table":
            source = result["tables"][extra]
            table_id = f"T{len(tables) + 1}"
            caption = _table_caption(source)
            grid = _table_grid(source)
            label = caption or " | ".join(cell for cell in (grid[0] if grid else []) if cell)[:160]
            tables[table_id] = Table(table_id, caption, grid, page, line_id)
            lines.append(
                Line(line_id, f"[Table {table_id}] {label}".strip(), page, "table", offset, 0, table_id)
            )
            continue
        is_numbered_heading = kind == "heading" and bool(_NUMBERED.match(text))
        key = running_key(text)
        header_part = index in edges and _running_part(key, running)
        if (kind == "running" or key in running or header_part) and not is_numbered_heading:
            if key in kept_running or not key:
                removed.append(text)
                continue
            kept_running.add(key)
            kind = "running"
        level = extra if kind == "heading" else 0
        lines.append(
            Line(
                line_id, text, page, kind, offset, heading_level=level or 0, starts_paragraph=paragraph_start
            )
        )
    return Document(source_sha256, page_count, lines, tables, sorted(set(removed)))
