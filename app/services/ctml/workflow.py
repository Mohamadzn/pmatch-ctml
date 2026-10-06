"""The v5 supervisor workflow: one Microsoft Agent Framework workflow per run.

The supervisor is a workflow executor written in code. Each specialist is an executor that
runs one kind of agent. Every message goes from the supervisor to one specialist and back
(hub and spoke), and the supervisor decides each next step: the dependency gates, the
fan-out per criterion, the bounded repairs and what goes to human review. No model chooses
the process.

1. Metadata, study design and the coverage checker (one listing per criterion) start together.
2. After the study design, the eligibility logic agent structures each criterion.
3. Structure gate (code): the structure is compared with the coverage listing. A criterion
   with omissions goes back to the eligibility logic agent with the omitted words, at most
   max_repair_rounds times. An omission that is still there after the repair goes to review
   (a finding that repeats is for a person, not for another model call).
4. The clinical, genomics and prior therapy agents fill the slots of their domain.
5. Merge (code, merge.py): slots and values are joined into the criterion encoding.
6. The resolver audits the compiled candidate once. A "repair" problem on a criterion, with
   verified protocol quotes, sends that criterion through steps 2 to 5 once more with the
   problem as a note. Every other problem goes to review.
7. The workflow returns the outcomes. The pipeline compiles the CTML and runs the QA gate.

Agent outcomes are saved in the run folder with a fingerprint of their input. On resume, an
accepted outcome with the same fingerprint is reused (these files are the run's checkpoints).
Executors in one workflow step run at the same time, and the runner limits how many agents
talk to the model at once (PMATCH_MAX_PARALLEL_AGENTS).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler

from app.services.ctml.agents.clinical import CLINICAL_AGENT
from app.services.ctml.agents.coverage import list_conditions
from app.services.ctml.agents.design import describe_design
from app.services.ctml.agents.eligibility import structure_criterion
from app.services.ctml.agents.genomics import GENOMICS_AGENT
from app.services.ctml.agents.loop import LoopOutcome
from app.services.ctml.agents.metadata import extract_metadata
from app.services.ctml.agents.prior_therapy import PRIOR_THERAPY_AGENT
from app.services.ctml.agents.resolver import review_candidate
from app.services.ctml.agents.specialist import Specialist, fill_slots
from app.services.ctml.checks.criterion import criterion_source
from app.services.ctml.compiler import CriterionResult, compile_ctml
from app.services.ctml.contracts import (
    CoverageItem,
    CoverageSubmission,
    DesignSubmission,
    EligibilitySubmission,
    GroupNode,
    LeafNode,
    MetadataSubmission,
    ResolverIssue,
    ResolverSubmission,
)
from app.services.ctml.document.inventory import Criterion
from app.services.ctml.matching import compile_tree, leaf_payload
from app.services.ctml.merge import coverage_omissions, merge_criterion, render_logic, slots_of
from app.services.ctml.metadata_rules import resolve_metadata
from app.services.ctml.outcomes import (
    AgentRunner,
    criterion_fingerprint,
    failure_reason,
    fingerprint,
)
from app.services.ctml.runtime import RunContext

logger = logging.getLogger(__name__)

SPECIALISTS: dict[str, Specialist] = {
    agent.role: agent for agent in (CLINICAL_AGENT, GENOMICS_AGENT, PRIOR_THERAPY_AGENT)
}
ROLES = ("metadata", "design", "coverage", "eligibility", *SPECIALISTS, "resolver")
SUPERVISOR = "supervisor"
# At most this many criteria go back for repair after the resolver.
MAX_RESOLVER_REPAIRS = 10
# Workflow steps (supersteps); a run needs about 20.
MAX_ITERATIONS = 200
VIEW_TEXT_CHARS = 1500


# --- messages -------------------------------------------------------------------------------


@dataclass
class Start:
    """Starts the workflow. The run folder is the job; its name is the job ID."""

    job_id: str = ""


@dataclass
class Job:
    """One agent run."""

    key: str  # the criterion ID, or the role of a trial-level agent
    criterion: Criterion | None = None
    arms: str = ""
    structure: EligibilitySubmission | None = None
    note: str = ""  # a repair request from the supervisor
    tag: str = ""  # file name suffix of a repeated run ("attempt2", ...)
    view: str = ""
    criterion_ids: frozenset[str] = frozenset()


@dataclass
class Task:
    role: str
    jobs: list[Job]


@dataclass
class Done:
    role: str
    outcomes: dict[str, LoopOutcome]  # by Job.key


@dataclass
class WorkflowOutcome:
    metadata: LoopOutcome
    design: LoopOutcome
    results: dict[str, CriterionResult]
    review: list[str]
    evidence: dict


# --- the agent runs behind the specialist executors ---------------------------------------


def _file_name(key: str, tag: str = "") -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", key)
    return f"{name}.{tag}.json" if tag else f"{name}.json"


class AgentCalls:
    """Starts each kind of agent, with its saved outcome file and input fingerprint."""

    def __init__(self, client: Any, context: RunContext, runner: AgentRunner, agents_dir: Path) -> None:
        self.client = client
        self.context = context
        self.runner = runner
        self.agents_dir = agents_dir

    def work(self, role: str) -> Callable[[Job], Awaitable[LoopOutcome]]:
        if role in SPECIALISTS:
            return lambda job: self.specialist(SPECIALISTS[role], job)
        return getattr(self, role)

    async def metadata(self, job: Job) -> LoopOutcome:
        return await self.runner.run(
            self.agents_dir / "metadata.json",
            MetadataSubmission,
            lambda: extract_metadata(self.client, self.context),
        )

    async def design(self, job: Job) -> LoopOutcome:
        return await self.runner.run(
            self.agents_dir / "design.json",
            DesignSubmission,
            lambda: describe_design(self.client, self.context),
        )

    async def coverage(self, job: Job) -> LoopOutcome:
        criterion = job.criterion
        return await self.runner.run(
            self.agents_dir / "coverage" / _file_name(job.key),
            CoverageSubmission,
            lambda: list_conditions(self.client, self.context, criterion),
            criterion_fingerprint(criterion),
        )

    async def eligibility(self, job: Job) -> LoopOutcome:
        criterion = job.criterion
        return await self.runner.run(
            self.agents_dir / "eligibility" / _file_name(job.key, job.tag),
            EligibilitySubmission,
            lambda: structure_criterion(self.client, self.context, criterion, job.arms, job.note),
            fingerprint(criterion_fingerprint(criterion), job.arms, job.note),
        )

    async def specialist(self, specialist: Specialist, job: Job) -> LoopOutcome:
        criterion, structure = job.criterion, job.structure
        return await self.runner.run(
            self.agents_dir / specialist.role / _file_name(job.key, job.tag),
            specialist.submission,
            lambda: fill_slots(self.client, self.context, criterion, structure, specialist, job.note),
            fingerprint(criterion_fingerprint(criterion), structure.model_dump(mode="json"), job.note),
        )

    async def resolver(self, job: Job) -> LoopOutcome:
        (self.agents_dir / "resolver_view.md").parent.mkdir(parents=True, exist_ok=True)
        (self.agents_dir / "resolver_view.md").write_text(job.view, encoding="utf-8")
        return await self.runner.run(
            self.agents_dir / "resolver.json",
            ResolverSubmission,
            lambda: review_candidate(self.client, self.context, job.view, set(job.criterion_ids)),
            fingerprint(job.view),
        )


class AgentWorker(Executor):
    """A specialist executor: runs its agent for every job of a task, at the same time."""

    def __init__(self, role: str, work: Callable[[Job], Awaitable[LoopOutcome]]) -> None:
        super().__init__(id=role)
        self.role = role
        self.work = work

    async def _one(self, job: Job) -> LoopOutcome:
        try:
            return await self.work(job)
        except Exception as error:  # one failed agent must not stop the run
            logger.exception("%s/%s failed", job.key, self.role)
            return LoopOutcome(
                owner=f"{job.key}/{self.role}", error=f"{type(error).__name__}: {str(error)[:500]}"
            )

    @handler
    async def run(self, task: Task, ctx: WorkflowContext[Done]) -> None:
        outcomes = await asyncio.gather(*(self._one(job) for job in task.jobs))
        await ctx.send_message(
            Done(self.role, {job.key: outcome for job, outcome in zip(task.jobs, outcomes, strict=True)})
        )


# --- readable forms for notes and the resolver ----------------------------------------------


def arms_text(design: LoopOutcome | None) -> str:
    if design is None or design.accepted is None:
        return "(The study design agent did not finish. Use the criterion's population only.)"
    lines = []
    for arm in design.accepted.arms:
        labels = ", ".join(f'"{label}"' for label in arm.scope_labels) or "none (trial-wide criteria only)"
        lines.append(f"- {arm.arm_code}: population labels {labels}")
    return "\n".join(lines)


def describe_condition(payload: dict) -> str:
    """One CTML condition in words: what an eligible patient has, or must not have (NOT)."""
    kind = next(iter(payload), "")
    fields = payload.get(kind, {})

    def plain(value) -> str:
        return str(value).lstrip("!")

    def negated(*names: str) -> bool:
        return any(str(fields.get(name, "")).startswith("!") for name in names)

    if kind == "clinical":
        parts = []
        if fields.get("oncotree_primary_diagnosis"):
            parts.append(f"diagnosis {plain(fields['oncotree_primary_diagnosis'])}")
        for name, label in (("her2_status", "HER2"), ("er_status", "ER"), ("pr_status", "PR")):
            if fields.get(name):
                parts.append(f"{label} {fields[name]}")
        if fields.get("age_expression"):
            parts.append(f"age {fields['age_expression']}")
        sign = "NOT " if negated("oncotree_primary_diagnosis") else ""
        return sign + ", ".join(parts)
    if kind == "genomic":
        detail = [
            str(fields[name])
            for name in ("protein_change", "wildcard_protein_change", "fusion_partner_hugo_symbol")
            if fields.get(name)
        ]
        if fields.get("cnv_call"):
            detail.append(plain(fields["cnv_call"]))
        sign = "NOT " if negated("variant_category", "cnv_call", "hugo_symbol") else ""
        text = f"{plain(fields.get('hugo_symbol', '?'))} {plain(fields.get('variant_category', ''))}"
        return sign + text + (f" ({', '.join(detail)})" if detail else "")
    if kind == "prior_treatment":
        named = [
            plain(fields[name]) for name in ("agent_class", "agent", "transplant_type") if fields.get(name)
        ]
        sign = "NOT " if negated("agent_class", "agent", "transplant_type") else ""
        what = f"prior {fields.get('treatment_category', '?')}"
        return sign + what + (f": {', '.join(named)}" if named else " (any)")
    return json.dumps(payload)


def render_match(node: dict, indent: int = 0) -> str:
    """A compiled CTML rule in words (ALL of / ANY of / conditions)."""
    pad = "  " * indent
    for key, label in (("and", "ALL of:"), ("or", "ANY of:")):
        if key in node:
            return "\n".join([pad + label, *(render_match(child, indent + 1) for child in node[key])])
    return f"{pad}- {describe_condition(node)}"


def render_tree(node: GroupNode | LeafNode | None, indent: int = 0) -> str:
    """An encoding in words: each condition with the protocol words and lines it encodes."""
    pad = "  " * indent
    if node is None:
        return f"{pad}(no rule)"
    if isinstance(node, LeafNode):
        payload = leaf_payload(node) if node.kind() else {}
        return (
            f'{pad}- {describe_condition(payload)}  <- "{node.source_concept}" ({", ".join(node.line_ids)})'
        )
    label = "ALL of:" if node.type == "and" else "ANY of:"
    return "\n".join([pad + label, *(render_tree(child, indent + 1) for child in node.items)])


def render_encoding(result: CriterionResult | None) -> str:
    if result is None or result.submission is None:
        return "(no accepted encoding: the whole criterion is kept as text)"
    submission = result.submission
    parts = [f"representable: {submission.representable}", "Rule:", render_tree(submission.tree, 1)]
    for item in submission.unmapped_items:
        parts.append(f'Kept as text: "{item.quote}" ({item.reason})')
    return "\n".join(parts)


def omission_note(structure: EligibilitySubmission, omissions: list[CoverageItem]) -> str:
    listed = "\n".join(f'- "{item.quote}" ({item.kind})' for item in omissions)
    unmapped = "\n".join(f'- "{item.quote}"' for item in structure.unmapped_items) or "- (none)"
    return (
        "Repair request from the supervisor. An independent listing of this criterion's conditions found "
        "words that your earlier structure neither states in a slot nor keeps as an unmapped item:\n"
        f"{listed}\n\nYour earlier structure (representable: {structure.representable}):\n"
        f"{render_logic(structure.logic)}\nUnmapped items:\n{unmapped}\n\n"
        "Submit a new structure that covers each listed condition with a slot or an unmapped item. Keep what "
        "was right. If listed words do not decide eligibility for this criterion, leave them out and say why "
        "in review_notes."
    )


def resolver_note(issues: list[ResolverIssue], earlier: CriterionResult | None) -> str:
    problems = "\n".join(
        f"- {issue.problem} Protocol words: "
        + "; ".join(f'"{quote}"' for quote in issue.quotes)
        + f" ({', '.join(issue.line_ids)})"
        for issue in issues
    )
    return (
        "Repair request from the supervisor. The resolver, which audits the assembled CTML candidate, "
        f"reported a problem with this criterion's encoding:\n{problems}\n\n"
        f"The earlier encoding:\n{render_encoding(earlier)}\n\n"
        "Check the report against the protocol. Correct your part of the work when the protocol's words "
        "support it, and keep what was right. When the report is wrong, give the same answer as before and "
        "say why in review_notes."
    )


READING_GUIDE = (
    "How to read the rules: an arm matches a patient who meets ALL of the rules of the criteria that apply "
    "to the arm. 'NOT X' means the patient must not have X (an exclusion). 'ANY of' lists alternatives. "
    "Text kept for a criterion is not matched automatically: a reviewer reads it."
)


def candidate_view(
    fields: dict,
    design: DesignSubmission,
    results: list[CriterionResult],
    placements: dict,
    arm_rules: dict[str, list] | None = None,
) -> str:
    """What the resolver audits: trial details, arms with their rules, and every criterion."""
    parts = ["# CTML candidate", "", READING_GUIDE, "", "## Trial details"]
    for key in (
        "trial_id",
        "long_title",
        "short_title",
        "protocol_no",
        "protocol_version_no",
        "protocol_version_date",
        "phase",
        "sponsor_name",
        "nct_purpose",
    ):
        if fields.get(key):
            parts.append(f"- {key}: {fields[key]}")
    parts += ["", "## Arms (study design)"]
    for arm in design.arms:
        labels = ", ".join(f'"{label}"' for label in arm.scope_labels) or "none"
        parts.append(f"- {arm.arm_code} (population labels: {labels}; lines {', '.join(arm.line_ids)})")
        for level in arm.dose_levels:
            parts.append(
                f"  - dose level {level.level_code}: {level.level_description} ({', '.join(level.line_ids)})"
            )
        if arm_rules is not None:
            match = arm_rules.get(arm.arm_code) or []
            parts.append("  Rule of this arm:")
            parts.append(render_match(match[0], 2) if match else "    (no rule)")
    parts += [
        "",
        "## Criteria",
        "A criterion with a population label applies only to the arms with that label. Every arm's rule is "
        "the AND of the rules of the criteria that apply to it.",
    ]
    for result in results:
        criterion = result.criterion
        text = " ".join(criterion.text.split())
        if len(text) > VIEW_TEXT_CHARS:
            text = text[:VIEW_TEXT_CHARS] + " [...]"
        population = criterion.scope_label or "all participants"
        lines = f"{criterion.line_ids[0]}-{criterion.line_ids[-1]}" if criterion.line_ids else "?"
        parts += [
            "",
            f"### {criterion.criterion_id} ({criterion.kind}; population: {population}; lines {lines})",
            f"Text: {text}",
            render_encoding(result if result.status == "accepted" else None),
            f"Criterion text placed in: {placements.get(criterion.criterion_id, '?')}",
        ]
    return "\n".join(parts)


# --- the supervisor -------------------------------------------------------------------------


@dataclass
class Plan:
    client: Any
    context: RunContext
    criteria: list[Criterion]
    registry: dict | None = None
    nct_id: str | None = None


@dataclass
class Track:
    """Where one criterion is in the workflow."""

    criterion: Criterion
    coverage: LoopOutcome | None = None
    structure: LoopOutcome | None = None  # the latest eligibility logic outcome
    # The latest accepted structure of the current round (the first encoding, or the resolver
    # repair). A failed coverage repair falls back to it.
    round_structure: LoopOutcome | None = None
    attempts: list[dict] = field(default_factory=list)
    gated: int = 0  # number of attempts the structure gate has seen
    coverage_repairs: int = 0
    tag: str = ""  # tag of the latest eligibility run
    note: str = ""  # the resolver's repair request, when there is one
    used: EligibilitySubmission | None = None  # the structure the domain agents fill
    omissions: list[CoverageItem] = field(default_factory=list)  # left after the repairs
    fills: dict[str, LoopOutcome] = field(default_factory=dict)
    waiting: set[str] = field(default_factory=set)
    gate_review: list[str] = field(default_factory=list)
    result: CriterionResult | None = None
    result_review: list[str] = field(default_factory=list)
    resolver: dict | None = None  # the issues and the earlier result of a resolver repair


class Supervisor(Executor):
    """Code that owns the run: gates, fan-out, repairs and review routing."""

    def __init__(self, plan: Plan) -> None:
        super().__init__(id=SUPERVISOR)
        self.plan = plan
        self.settings = plan.context.settings
        self.tracks = {criterion.criterion_id: Track(criterion) for criterion in plan.criteria}
        self.trial: dict[str, LoopOutcome] = {}
        self.pending: dict[str, int] = dict.fromkeys(ROLES, 0)
        self.stage = "criteria"
        self.arms = ""
        self.review: list[str] = []
        self.resolver_issues: list[dict] = []
        self.timeline: list[dict] = []
        self.started = time.monotonic()

    # -- bookkeeping --

    def _event(self, text: str) -> None:
        self.timeline.append({"seconds": round(time.monotonic() - self.started, 1), "event": text})
        logger.info("workflow: %s", text)

    async def _send(self, ctx: WorkflowContext, outbox: dict[str, list[Job]]) -> None:
        for role, jobs in outbox.items():
            self.pending[role] += 1
            self._event(f"{role}: {len(jobs)} job(s) sent")
            await ctx.send_message(Task(role, jobs), target_id=role)

    def _structure_job(self, track: Track, note: str) -> Job:
        track.tag = f"attempt{len(track.attempts) + 1}" if track.attempts else ""
        return Job(
            track.criterion.criterion_id,
            criterion=track.criterion,
            arms=self.arms,
            note=note,
            tag=track.tag,
        )

    # -- handlers --

    @handler
    async def start(self, message: Start, ctx: WorkflowContext[Task, WorkflowOutcome]) -> None:
        self.started = time.monotonic()
        outbox: dict[str, list[Job]] = {"metadata": [Job("metadata")], "design": [Job("design")]}
        if self.tracks:
            outbox["coverage"] = [Job(cid, criterion=track.criterion) for cid, track in self.tracks.items()]
        self._event(f"start {message.job_id}: {len(self.tracks)} criteria")
        await self._send(ctx, outbox)

    @handler
    async def collect(self, message: Done, ctx: WorkflowContext[Task, WorkflowOutcome]) -> None:
        role = message.role
        self.pending[role] -= 1
        accepted = sum(1 for outcome in message.outcomes.values() if outcome.accepted is not None)
        self._event(f"{role}: {accepted} of {len(message.outcomes)} accepted")
        outbox: dict[str, list[Job]] = {}
        if role in ("metadata", "design"):
            self.trial[role] = message.outcomes[role]
            if role == "design":
                # Dependency gate: the eligibility logic agent needs the arms and their populations.
                self.arms = arms_text(self.trial["design"])
                for track in self.tracks.values():
                    outbox.setdefault("eligibility", []).append(self._structure_job(track, ""))
        elif role == "coverage":
            for key, outcome in message.outcomes.items():
                self.tracks[key].coverage = outcome
                self._gate(self.tracks[key], outbox)
        elif role == "eligibility":
            for key, outcome in message.outcomes.items():
                track = self.tracks[key]
                track.structure = outcome
                track.attempts.append(
                    {
                        "tag": track.tag or "first",
                        "accepted": outcome.accepted is not None,
                        "error": "" if outcome.accepted is not None else failure_reason(outcome),
                        "agent": outcome.summary(),
                    }
                )
                self._gate(track, outbox)
        elif role in SPECIALISTS:
            for key, outcome in message.outcomes.items():
                track = self.tracks[key]
                track.fills[role] = outcome
                track.waiting.discard(role)
                if not track.waiting:
                    self._merge(track)
        elif role == "resolver":
            self.trial["resolver"] = message.outcomes["resolver"]
            self._route_resolver(outbox)
        if outbox:
            await self._send(ctx, outbox)
        else:
            await self._advance(ctx)

    # -- structure gate --

    def _gate(self, track: Track, outbox: dict[str, list[Job]]) -> None:
        if track.structure is None or track.coverage is None or track.gated == len(track.attempts):
            return
        track.gated = len(track.attempts)
        cid = track.criterion.criterion_id
        latest = track.structure
        if latest.accepted is not None:
            track.round_structure = latest
        chosen = track.round_structure
        if chosen is None:
            if track.resolver is not None:
                track.result = track.resolver["earlier"]
                track.result_review = [
                    *track.resolver["earlier_review"],
                    f"resolver_repair_not_accepted:{cid}: {failure_reason(latest)}; "
                    "the earlier encoding is kept",
                ]
            else:
                track.result = self._failed(track)
                track.result_review = []
            return
        structure = chosen.accepted
        review: list[str] = []
        if latest is not chosen:
            review.append(
                f"coverage_repair_not_accepted:{cid}: {failure_reason(latest)}; the earlier structure is used"
            )
        omissions: list[CoverageItem] = []
        if track.coverage.accepted is not None:
            source = criterion_source(self.plan.context, track.criterion, None)
            omissions = coverage_omissions(source, structure, track.coverage.accepted.items)
        if omissions and latest is chosen and track.coverage_repairs < self.settings.max_repair_rounds:
            track.coverage_repairs += 1
            note = "\n\n".join(part for part in (track.note, omission_note(structure, omissions)) if part)
            outbox.setdefault("eligibility", []).append(self._structure_job(track, note))
            return
        track.omissions = omissions
        track.gate_review = review
        track.used = structure
        roles = [role for role, agent in SPECIALISTS.items() if slots_of(structure, agent.domain)]
        track.fills = {}
        track.waiting = set(roles)
        if not roles:
            self._merge(track)
            return
        for role in roles:
            outbox.setdefault(role, []).append(
                Job(cid, criterion=track.criterion, structure=structure, note=track.note, tag=track.tag)
            )

    # -- merge --

    def _failed(self, track: Track) -> CriterionResult:
        outcome = track.structure
        return CriterionResult(
            criterion=track.criterion,
            status="failed",
            last_errors=outcome.last_errors if outcome else [],
            summary=self._summaries(track),
            details=self._details(track, None),
        )

    def _summaries(self, track: Track) -> dict:
        summary = {"layout": "v5"}
        if track.coverage is not None:
            summary["coverage"] = track.coverage.summary()
        if track.structure is not None:
            summary["eligibility"] = track.structure.summary()
        for role, outcome in track.fills.items():
            summary[role] = outcome.summary()
        return summary

    def _details(self, track: Track, dropped: list[dict] | None) -> dict:
        coverage = track.coverage
        details: dict = {
            "structure": track.used.model_dump(mode="json") if track.used is not None else None,
            "structure_attempts": track.attempts,
            "coverage_listing": (
                [item.model_dump() for item in coverage.accepted.items]
                if coverage is not None and coverage.accepted is not None
                else {"error": failure_reason(coverage) if coverage is not None else "not run"}
            ),
            "coverage_omissions": [item.model_dump() for item in track.omissions],
            "fills": {
                role: (
                    [fill.model_dump(mode="json", exclude_none=True) for fill in outcome.accepted.fills]
                    if outcome.accepted is not None
                    else {"error": failure_reason(outcome)}
                )
                for role, outcome in track.fills.items()
            },
        }
        if dropped:
            details["merge_dropped"] = dropped
        if track.resolver is not None:
            details["resolver_repair"] = track.resolver["issues"]
        return details

    def _merge(self, track: Track) -> None:
        cid = track.criterion.criterion_id
        review: list[str] = list(track.gate_review)
        if track.coverage is not None and track.coverage.accepted is None:
            review.append(f"coverage_not_checked:{cid}: {failure_reason(track.coverage)}")
        for item in track.omissions:
            review.append(
                f'coverage_omission:{cid}: the coverage checker lists "{item.quote[:150]}" ({item.kind}); '
                "neither the rule nor the text items state it"
            )
        fills = {}
        confirmations = list(track.round_structure.confirmations)
        for role, outcome in track.fills.items():
            if outcome.accepted is None:
                review.append(f"agent_not_accepted:{cid}/{role}: {failure_reason(outcome)}")
                continue
            confirmations += outcome.confirmations
            for fill in outcome.accepted.fills:
                fills[fill.slot_id] = fill
            review += [f"agent_note:{cid}/{role}: {note[:200]}" for note in outcome.accepted.review_notes]
        merged = merge_criterion(track.used, fills)
        for item in merged.dropped:
            if item.get("alternative"):
                review.append(
                    f'or_alternative_not_encoded:{cid}:{item["slot_id"]}: "{item["concept"][:100]}" stays as '
                    "text; patients who qualify only through it are not matched automatically "
                    f"({item['reason'][:120]})"
                )
            else:
                review.append(
                    f'slot_not_encoded:{cid}:{item["slot_id"]}: "{item["concept"][:100]}" kept as text '
                    f"({item['reason'][:150]})"
                )
        if track.resolver is not None:
            problems = " | ".join(issue["problem"][:200] for issue in track.resolver["issues"])
            earlier = track.resolver["earlier"]
            same = (
                earlier is not None
                and earlier.submission is not None
                and earlier.submission.representable == merged.submission.representable
                and earlier.compiled
                == (compile_tree(merged.submission.tree) if merged.submission.tree else None)
            )
            if same:
                review.append(
                    f"resolver_finding_not_adopted:{cid}: the agents kept the encoding after checking: "
                    + problems
                )
            else:
                review.append(
                    f"resolver_repaired:{cid}: encoded again after the resolver reported: {problems}"
                )
        track.result = CriterionResult(
            criterion=track.criterion,
            status="accepted",
            submission=merged.submission,
            confirmations=confirmations,
            summary=self._summaries(track),
            details=self._details(track, merged.dropped),
        )
        track.result_review = review

    # -- resolver --

    def _candidate(self) -> tuple[str, set[str]]:
        metadata = self.trial.get("metadata")
        fields, _, _ = resolve_metadata(
            metadata.accepted if metadata else None, self.plan.registry, self.plan.nct_id
        )
        results = [
            self.tracks[c.criterion_id].result or self._failed(self.tracks[c.criterion_id])
            for c in self.plan.criteria
        ]
        design = self.trial["design"].accepted
        ctml, evidence, _ = compile_ctml(
            fields,
            design,
            results,
            self.settings.omit_categories,
            self.plan.context.glossary,
            self.plan.context.oncotree,
        )
        placements = {entry["criterion_id"]: entry["text_placement"] for entry in evidence["criteria"]}
        arm_rules = {arm["arm_code"]: arm["match"] for arm in ctml["treatment_list"]["step"][0]["arm"]}
        return candidate_view(fields, design, results, placements, arm_rules), set(self.tracks)

    def _route_resolver(self, outbox: dict[str, list[Job]]) -> None:
        outcome = self.trial["resolver"]
        if outcome.accepted is None:
            self.review.append(f"resolver_not_accepted: {failure_reason(outcome)}")
            return
        targets: dict[str, list[ResolverIssue]] = {}
        for issue in outcome.accepted.issues:
            entry = issue.model_dump()
            repair = (
                issue.action == "repair"
                and issue.target in self.tracks
                and self.settings.max_repair_rounds > 0
                and (issue.target in targets or len(targets) < MAX_RESOLVER_REPAIRS)
            )
            if repair:
                targets.setdefault(issue.target, []).append(issue)
                entry["routed_to"] = "repair"
            else:
                lines = ", ".join(issue.line_ids[:6])
                self.review.append(f"resolver:{issue.target}: {issue.problem[:300]} ({lines})")
                entry["routed_to"] = "review"
            self.resolver_issues.append(entry)
        for cid, issues in targets.items():
            track = self.tracks[cid]
            track.resolver = {
                "issues": [issue.model_dump() for issue in issues],
                "earlier": track.result,
                "earlier_review": track.result_review,
            }
            track.note = resolver_note(issues, track.result)
            track.result = None
            track.round_structure = None
            outbox.setdefault("eligibility", []).append(self._structure_job(track, track.note))
        if targets:
            self.stage = "resolver_repair"

    # -- progress --

    async def _advance(self, ctx: WorkflowContext[Task, WorkflowOutcome]) -> None:
        if any(self.pending.values()):
            return
        if self.stage == "criteria":
            design = self.trial.get("design")
            if self.settings.resolver and self.tracks and design is not None and design.accepted is not None:
                self.stage = "resolver"
                try:
                    view, criterion_ids = self._candidate()
                except Exception as error:  # the resolver is an audit: its failure must not stop the run
                    logger.exception("The candidate for the resolver could not be built")
                    self.review.append(
                        f"resolver_skipped: the candidate could not be built ({type(error).__name__})"
                    )
                else:
                    job = Job("resolver", view=view, criterion_ids=frozenset(criterion_ids))
                    await self._send(ctx, {"resolver": [job]})
                    return
            elif self.settings.resolver and self.tracks:
                self.review.append("resolver_skipped: no accepted study design to compile a candidate")
        await self._finish(ctx)

    async def _finish(self, ctx: WorkflowContext[Task, WorkflowOutcome]) -> None:
        self.stage = "done"
        review = list(self.review)
        results = {}
        for cid, track in self.tracks.items():
            if track.result is None:  # never expected: the gates above always give a result
                track.result = self._failed(track)
                review.append(f"workflow_incomplete:{cid}")
            results[cid] = track.result
            review += track.result_review
        resolver = self.trial.get("resolver")
        evidence = {
            "layout": "v5",
            "timeline": self.timeline,
            "resolver": None
            if resolver is None
            else {
                "agent": resolver.summary(),
                "summary": resolver.accepted.summary if resolver.accepted is not None else "",
                "issues": self.resolver_issues,
            },
            "repairs": {cid: track.attempts for cid, track in self.tracks.items() if len(track.attempts) > 1},
        }
        self._event("finished")
        missing = LoopOutcome(owner="", error="not run")
        await ctx.yield_output(
            WorkflowOutcome(
                metadata=self.trial.get("metadata", missing),
                design=self.trial.get("design", missing),
                results=results,
                review=review,
                evidence=evidence,
            )
        )


def build_workflow(plan: Plan, runner: AgentRunner, agents_dir: Path):
    calls = AgentCalls(plan.client, plan.context, runner, agents_dir)
    supervisor = Supervisor(plan)
    builder = WorkflowBuilder(start_executor=supervisor, max_iterations=MAX_ITERATIONS)
    for role in ROLES:
        worker = AgentWorker(role, calls.work(role))
        builder.add_edge(supervisor, worker).add_edge(worker, supervisor)
    return builder.build()


async def run_workflow(
    client: Any,
    context: RunContext,
    run_dir: Path,
    criteria: list[Criterion],
    resume: bool,
    registry: dict | None = None,
    nct_id: str | None = None,
) -> WorkflowOutcome:
    """Run the v5 agents for one protocol and return their outcomes."""
    plan = Plan(client=client, context=context, criteria=criteria, registry=registry, nct_id=nct_id)
    runner = AgentRunner(context.settings.max_parallel_agents, resume)
    workflow = build_workflow(plan, runner, run_dir / "agents")
    result = await workflow.run(Start(job_id=run_dir.name))
    outputs = result.get_outputs()
    if not outputs:
        raise RuntimeError("The supervisor workflow ended without an outcome (see run.log)")
    return outputs[-1]
