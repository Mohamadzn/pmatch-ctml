"""Checks run by submit_review (the resolver / critic): every issue quotes the protocol."""

from __future__ import annotations

from app.services.ctml.checks.common import closest_span, line_texts, reject, unknown_lines
from app.services.ctml.contracts import ResolverSubmission
from app.services.ctml.runtime import RunContext
from app.services.ctml.text import contains_phrase

MAX_ISSUES = 15
TRIAL_TARGETS = ("design", "metadata")


def check_review(
    submission: ResolverSubmission, context: RunContext, criterion_ids: set[str]
) -> tuple[list[dict], list[dict]]:
    problems = []
    if len(submission.issues) > MAX_ISSUES:
        problems.append(reject("too_many_issues", "/issues", f"Report at most {MAX_ISSUES} problems."))
    for index, issue in enumerate(submission.issues):
        path = f"/issues/{index}"
        if issue.target not in criterion_ids and issue.target not in TRIAL_TARGETS:
            problems.append(
                reject(
                    "unknown_target",
                    f"{path}/target",
                    "Use a criterion ID from the candidate, or design or metadata.",
                )
            )
        registry = [line_id for line_id in issue.line_ids if line_id in context.registry_lines]
        missing = [
            i for i in unknown_lines(context.document, issue.line_ids) if i not in context.registry_lines
        ]
        if missing:
            problems.append(
                reject("unknown_line_id", f"{path}/line_ids", "Unknown line IDs: " + ", ".join(missing))
            )
            continue
        cited = "\n".join(
            [line_texts(context.document, issue.line_ids), *(context.registry_lines[i] for i in registry)]
        )
        for number, quote in enumerate(issue.quotes):
            if not contains_phrase(cited, quote):
                problems.append(
                    reject(
                        "quote_not_in_cited_lines",
                        f"{path}/quotes/{number}",
                        "Quote exact words of the cited lines. Closest: " + closest_span(cited, quote),
                    )
                )
    return problems, []
