"""Protocol documents bound together in one PDF (for example version 2.0 followed by 3.0).

A new document starts where a table of contents appears again after many pages of body
text. The start page is the repeated title page when one is found, otherwise the same
number of front pages as the first document. Which document to use is decided in the
pipeline: the last one that has eligibility criteria, unless --pages is given.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.ctml.document.model import Document
from app.services.ctml.document.sections import contents_pages
from app.services.ctml.text import comparison_key

MIN_DOCUMENT_PAGES = 20
TITLE_LINES = 3
TITLE_SEARCH_PAGES = 8


@dataclass(frozen=True)
class Segment:
    first_page: int
    last_page: int

    def label(self) -> str:
        return f"pages {self.first_page}-{self.last_page}"


def _clusters(pages: list[int]) -> list[list[int]]:
    clusters: list[list[int]] = []
    for page in sorted(pages):
        if clusters and page - clusters[-1][-1] <= 2:
            clusters[-1].append(page)
        else:
            clusters.append([page])
    return clusters


def _title_keys(document: Document, page: int) -> set[str]:
    texts = [line.text for line in document.lines if line.page == page and line.kind != "table"]
    return {comparison_key(text) for text in texts[:TITLE_LINES] if len(text) > 3}


def find_segments(document: Document) -> list[Segment]:
    toc_starts = [cluster[0] for cluster in _clusters(list(contents_pages(document)))]
    starts: list[int] = []
    for page in toc_starts:
        if not starts or page - starts[-1] >= MIN_DOCUMENT_PAGES:
            starts.append(page)
    if len(starts) < 2:
        return [Segment(1, document.page_count)]

    front_pages = starts[0] - 1
    first_title = _title_keys(document, 1)
    boundaries = [1]
    for toc_page in starts[1:]:
        guess = max(boundaries[-1] + 1, toc_page - front_pages)
        repeated = [
            page
            for page in range(max(boundaries[-1] + 1, toc_page - TITLE_SEARCH_PAGES), toc_page + 1)
            if first_title and len(first_title & _title_keys(document, page)) >= min(2, len(first_title))
        ]
        boundaries.append(repeated[0] if repeated else guess)
    segments = []
    for index, first in enumerate(boundaries):
        last = boundaries[index + 1] - 1 if index + 1 < len(boundaries) else document.page_count
        segments.append(Segment(first, last))
    return segments


def parse_pages(value: str, page_count: int) -> tuple[int, int]:
    """ "12-140" -> (12, 140), checked against the page count."""
    first_text, _, last_text = value.partition("-")
    try:
        first = int(first_text)
        last = int(last_text) if last_text else page_count
    except ValueError as error:
        raise ValueError(f"--pages must look like 12-140, not {value!r}") from error
    if not 1 <= first <= last <= page_count:
        raise ValueError(f"--pages {value} is outside the document (1-{page_count})")
    return first, last
