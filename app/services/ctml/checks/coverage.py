"""Checks run by submit_coverage (the coverage checker)."""

from __future__ import annotations

from app.services.ctml.checks.common import closest_span, reject
from app.services.ctml.checks.criterion import criterion_source
from app.services.ctml.contracts import CoverageSubmission
from app.services.ctml.document.inventory import Criterion
from app.services.ctml.runtime import RunContext
from app.services.ctml.text import contains_phrase

MAX_ITEMS = 40


def check_coverage(
    submission: CoverageSubmission, criterion: Criterion, context: RunContext
) -> tuple[list[dict], list[dict]]:
    source = criterion_source(context, criterion, None)
    problems = []
    if len(submission.items) > MAX_ITEMS:
        problems.append(reject("too_many_items", "/items", f"List at most {MAX_ITEMS} items."))
    for index, item in enumerate(submission.items):
        if not item.quote.strip() or not contains_phrase(source, item.quote):
            problems.append(
                reject(
                    "quote_not_in_criterion",
                    f"/items/{index}/quote",
                    "Quote exact words of the criterion. Closest: " + closest_span(source, item.quote),
                )
            )
    if not any(item.kind in ("requirement", "exception") for item in submission.items):
        problems.append(
            reject("no_requirement", "/items", "List the criterion's requirements (kind requirement).")
        )
    return problems, []
