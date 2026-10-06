"""End-to-end run: protocol PDF to a CTML review candidate.

Order: parse (Azure Document Intelligence, cached) -> lines, sections, glossary, criteria
(code) -> registries loaded -> agents -> compile (code) -> QA gate (code) -> outputs.

Agents, by PMATCH_AGENT_LAYOUT:
- "v5" (default): the supervisor workflow of architecture v5 (workflow.py) with the metadata,
  study design, eligibility logic, clinical, genomics, prior therapy, coverage and resolver
  agents;
- "compact": metadata, study design and one criterion agent per criterion, in parallel.

Every run writes its own folder. With resume, accepted agent outputs saved in the folder
are reused and only missing or failed agents run again.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.ctml.agents.loop import LoopOutcome
from app.services.ctml.compiler import CriterionResult, compile_ctml, criterion_evidence
from app.services.ctml.config import Settings
from app.services.ctml.contracts import CriterionSubmission, DesignSubmission, MetadataSubmission
from app.services.ctml.document.di_parser import ParseResult, parse_pdf, pdf_page_count
from app.services.ctml.document.glossary import build_glossary
from app.services.ctml.document.inventory import Criterion, build_inventory
from app.services.ctml.document.model import Document, build_document
from app.services.ctml.document.sections import route
from app.services.ctml.document.segments import Segment, find_segments, parse_pages
from app.services.ctml.metadata_rules import resolve_metadata
from app.services.ctml.outcomes import (
    AgentRunner,
    criterion_fingerprint,
    failure_reason,
    read_json,
    write_json,
)
from app.services.ctml.qa import build_qa, qa_markdown
from app.services.ctml.registries.clinicaltrials import fetch_study, find_nct_id, registry_lines
from app.services.ctml.registries.http import RegistryError, RegistryHttp
from app.services.ctml.registries.ledger import Ledger
from app.services.ctml.registries.oncotree import OncoTreeIndex
from app.services.ctml.registries.terminology import Terminology
from app.services.ctml.runtime import RunContext, ToolRecorder

logger = logging.getLogger(__name__)
PIPELINE_VERSION = "0.1.0"
CURRENT_RUN: contextvars.ContextVar[str] = contextvars.ContextVar("pmatch_run", default="")


@dataclass
class RunOptions:
    di_json: Path | None = None
    pages: str | None = None
    run_dir: Path | None = None
    resume: bool = False  # reuse accepted agent outputs already saved in run_dir
    refresh_di: bool = False
    registry_cache: bool = True
    parse_only: bool = False
    criteria: list[str] | None = None  # run only these criterion IDs (testing)


@dataclass
class RunResult:
    run_dir: Path
    ctml_path: Path | None
    qa: dict
    review: list[str] = field(default_factory=list)


def new_run_dir(settings: Settings, pdf_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return settings.runs_dir / f"{pdf_path.stem}-{stamp}-{secrets.token_hex(2)}"


def choose_pages(
    parsed: ParseResult, pages: str | None
) -> tuple[Document, tuple[int, int] | None, list[Segment], list[str]]:
    """The document to use: all pages, the --pages range, or one protocol of several bound together."""
    review: list[str] = []
    full = build_document(parsed.result, parsed.sha256)
    segments = find_segments(full)
    if pages:
        page_range = parse_pages(pages, full.page_count)
        return build_document(parsed.result, parsed.sha256, page_range), page_range, segments, review
    if len(segments) < 2:
        return full, None, segments, review
    with_criteria = []
    for segment in segments:
        candidate = build_document(parsed.result, parsed.sha256, (segment.first_page, segment.last_page))
        if build_inventory(candidate, route(candidate)).criteria:
            with_criteria.append(segment)
    if not with_criteria:
        return full, None, segments, review
    chosen = with_criteria[-1]
    if len(with_criteria) > 1:
        review.append(
            "several_protocol_documents: "
            + ", ".join(s.label() for s in with_criteria)
            + f" have eligibility criteria; this run used {chosen.label()}. Use --pages to choose another."
        )
    page_range = (chosen.first_page, chosen.last_page)
    return build_document(parsed.result, parsed.sha256, page_range), page_range, segments, review


def document_artifacts(run_dir: Path, document: Document, routing, inventory, glossary) -> None:
    folder = run_dir / "document"
    write_json(
        folder / "lines.json",
        [
            {
                "id": ln.line_id,
                "page": ln.page,
                "kind": ln.kind,
                "paragraph_start": ln.starts_paragraph,
                "text": ln.text,
            }
            for ln in document.lines
        ],
    )
    write_json(
        folder / "tables.json",
        {
            t.table_id: {"caption": t.caption, "page": t.page, "line_id": t.line_id, "rows": t.rows}
            for t in document.tables.values()
        },
    )
    write_json(
        folder / "routing.json",
        {
            "outline": routing.outline(),
            "eligibility": [
                {"section": r.section.label(), "kind": r.kind, "page": r.section.page}
                for r in routing.eligibility
            ],
            "design": [s.label() for s in routing.design],
            "glossary_sections": [s.label() for s in routing.glossary],
            "contents_pages": sorted(routing.contents_pages),
            "demoted_headings": routing.demoted_headings,
            "removed_running_lines": document.removed_running_lines,
        },
    )
    write_json(
        folder / "inventory.json",
        {
            "criteria": [c.as_dict() for c in inventory.criteria],
            "issues": inventory.issues,
            "group_titles": inventory.group_titles,
        },
    )
    (folder / "inventory.md").write_text(inventory_markdown(inventory), encoding="utf-8")
    write_json(folder / "glossary.json", glossary.as_dict())


def inventory_markdown(inventory) -> str:
    """The criteria as found by code, for a check before any model call."""
    rows = ["| id | kind | population | pages | sub-items | tables | text |", "|---|---|---|---|---|---|---|"]
    for c in inventory.criteria:
        text = " ".join(c.text.split())
        text = (text[:160] + "...") if len(text) > 160 else text
        rows.append(
            f"| {c.criterion_id} | {c.kind} | {c.scope_label or 'all'} | {','.join(map(str, c.pages))} | "
            f"{len(c.item_line_ids)} | {','.join(c.table_ids)} | {text.replace('|', '/')} |"
        )
    issues = "\n".join(f"- {issue}" for issue in inventory.issues) or "None."
    titles = (
        "\n".join(f"- {t['line_id']} (page {t['page']}): {t['text']}" for t in inventory.group_titles)
        or "None."
    )
    return (
        "# Eligibility criteria found\n\n"
        + "\n".join(rows)
        + f"\n\n## Issues\n\n{issues}\n"
        + "\n## Group titles\n\nLines read as titles inside the criteria sections. They belong to no "
        + f"criterion. A line here that continues a criterion's sentence is a capture error.\n\n{titles}\n"
    )


LOW_WORD_CONFIDENCE = 0.8
LOW_CONFIDENCE_SHARE = 0.05


def low_confidence_pages(result: dict, page_range: tuple[int, int] | None) -> list[str]:
    """Pages where more than 5% of the words were read with confidence below 0.8 (OCR to check)."""
    pages = []
    for page in result.get("pages", []):
        number = int(page.get("pageNumber", 0))
        if page_range and not page_range[0] <= number <= page_range[1]:
            continue
        words = page.get("words") or []
        low = sum(1 for word in words if float(word.get("confidence", 1.0)) < LOW_WORD_CONFIDENCE)
        if words and low / len(words) > LOW_CONFIDENCE_SHARE:
            pages.append(number)
    if not pages:
        return []
    return [
        f"ocr_low_confidence: pages {', '.join(map(str, pages[:30]))} have many uncertain words; check them"
    ]


def registry_citations(design: DesignSubmission, source: str) -> list[str]:
    """Dose levels that take a value from the registry (the protocol redacts or omits it)."""
    return [
        f"registry_dose:{arm.arm_code}:{level.level_code}: taken from {source or 'the registry'} "
        f"({', '.join(i for i in level.line_ids if i.startswith('R'))}); the protocol does not state it"
        for arm in design.arms
        for level in arm.dose_levels
        if any(line_id.startswith("R") for line_id in level.line_ids)
    ]


def figure_citations(document: Document, design: DesignSubmission) -> list[str]:
    """Design values taken from figure text (OCR of diagrams) need a visual check."""
    cited = [line_id for drug in design.drugs for line_id in drug.line_ids]
    for arm in design.arms:
        cited += arm.line_ids + [line_id for level in arm.dose_levels for line_id in level.line_ids]
    figures = sorted(
        {i for i in cited if document.has_line(i) and document.line(i).kind == "figure"},
        key=lambda i: int(i[1:]),
    )
    return [f"design_cites_figure_text: {', '.join(figures)}; check the diagram"] if figures else []


def check_resume(run_dir: Path, resume: bool, run_info: dict) -> None:
    """Refuse to resume a folder made from another PDF or another page range."""
    manifest = run_dir / "manifest.json"
    if not resume or not manifest.exists():
        return
    previous = read_json(manifest)
    for key in ("pdf_sha256", "page_range"):
        if key in previous and previous[key] != run_info.get(key):
            raise ValueError(
                f"{run_dir} was made from different input ({key} differs). Use a new --run-dir for this PDF."
            )


async def load_registries(
    settings: Settings, run_dir: Path, registry_cache: bool, glossary, resume: bool, transport=None
) -> tuple:
    http = RegistryHttp(
        settings.registry_timeout_seconds,
        settings.cache_dir if registry_cache else None,
        transport=transport,
    )
    ledger = Ledger(run_dir / "lookups.jsonl", keep_existing=resume)
    review: list[str] = []
    oncotree = None
    try:
        oncotree = await OncoTreeIndex.load(http, run_dir / "registry" / "oncotree_tumor_types.json")
    except (RegistryError, ValueError) as error:
        review.append(f"oncotree_unavailable: {error}")
    return http, ledger, oncotree, Terminology(http, ledger, oncotree, glossary), review


async def run_agents(
    client: Any,
    context: RunContext,
    run_dir: Path,
    criteria: list[Criterion],
    resume: bool,
) -> tuple[LoopOutcome, LoopOutcome, dict[str, LoopOutcome]]:
    """The compact layout: metadata, study design and one criterion agent per criterion."""
    from app.services.ctml.agents.criterion import encode_criterion
    from app.services.ctml.agents.design import describe_design
    from app.services.ctml.agents.metadata import extract_metadata

    agents_dir = run_dir / "agents"
    runner = AgentRunner(context.settings.max_parallel_agents, resume)
    metadata_task = runner.run(
        agents_dir / "metadata.json", MetadataSubmission, lambda: extract_metadata(client, context)
    )
    design_task = runner.run(
        agents_dir / "design.json", DesignSubmission, lambda: describe_design(client, context)
    )
    criterion_tasks = [
        runner.run(
            agents_dir / "criteria" / f"{criterion.criterion_id.replace('@', '_')}.json",
            CriterionSubmission,
            lambda criterion=criterion: encode_criterion(client, context, criterion),
            criterion_fingerprint(criterion),
        )
        for criterion in criteria
    ]
    metadata, design, *criterion_outcomes = await asyncio.gather(metadata_task, design_task, *criterion_tasks)
    by_id = {c.criterion_id: o for c, o in zip(criteria, criterion_outcomes, strict=True)}
    return metadata, design, by_id


def compact_results(criteria: list[Criterion], outcomes: dict[str, LoopOutcome]) -> list[CriterionResult]:
    return [
        CriterionResult(
            criterion=criterion,
            status="accepted" if outcomes[criterion.criterion_id].accepted is not None else "failed",
            submission=outcomes[criterion.criterion_id].accepted,
            confirmations=outcomes[criterion.criterion_id].confirmations,
            last_errors=outcomes[criterion.criterion_id].last_errors,
            summary=outcomes[criterion.criterion_id].summary(),
        )
        for criterion in criteria
    ]


async def run_pipeline(
    pdf_path: Path,
    settings: Settings,
    options: RunOptions | None = None,
    client: Any = None,
    http_transport: Any = None,
) -> RunResult:
    """Run one protocol. client and http_transport replace the model and the registries in tests."""
    options = options or RunOptions()
    run_dir = options.run_dir or new_run_dir(settings, pdf_path)
    resume = options.resume and run_dir.exists()
    run_dir.mkdir(parents=True, exist_ok=True)
    package_logger = logging.getLogger("app.services.ctml")
    handler = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(logging.INFO)
    # Runs started in parallel (API) each log only their own records.
    token = CURRENT_RUN.set(str(run_dir))
    handler.addFilter(lambda record: CURRENT_RUN.get() == str(run_dir))
    if package_logger.getEffectiveLevel() > logging.INFO:
        package_logger.setLevel(logging.INFO)
    package_logger.addHandler(handler)
    try:
        return await _execute(pdf_path, settings, options, client, http_transport, run_dir, resume)
    except Exception:
        logger.exception("The run failed")
        raise
    finally:
        package_logger.removeHandler(handler)
        handler.close()
        CURRENT_RUN.reset(token)


async def _execute(
    pdf_path: Path,
    settings: Settings,
    options: RunOptions,
    client: Any,
    http_transport: Any,
    run_dir: Path,
    resume: bool,
) -> RunResult:
    started = time.monotonic()
    review: list[str] = []
    run_info: dict[str, Any] = {
        "pdf": pdf_path.name,
        "run_dir": str(run_dir),
        "pipeline_version": PIPELINE_VERSION,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "resumed": resume,
        "settings": settings.public_view(),
    }

    parsed = await parse_pdf(pdf_path, settings, options.di_json, options.refresh_di)
    run_info.update(
        {"pdf_sha256": parsed.sha256, "document_intelligence": parsed.source, "di_options": parsed.options}
    )
    pdf_pages = pdf_page_count(pdf_path)
    di_pages = len(parsed.result.get("pages", []))
    run_info.update({"pdf_pages": pdf_pages, "di_pages": di_pages})
    if pdf_pages and di_pages and di_pages != pdf_pages:
        review.append(
            f"di_page_count_mismatch: the PDF has {pdf_pages} pages; "
            f"Document Intelligence returned {di_pages}"
        )

    document, page_range, segments, page_review = choose_pages(parsed, options.pages)
    review += page_review
    review += low_confidence_pages(parsed.result, page_range)
    run_info.update(
        {"page_range": list(page_range) if page_range else None, "segments": [s.label() for s in segments]}
    )
    check_resume(run_dir, resume, run_info)
    write_json(run_dir / "manifest.json", run_info)
    routing = route(document)
    inventory = build_inventory(document, routing)
    glossary = build_glossary(document, routing)
    review += [f"inventory:{issue}" for issue in inventory.issues]
    review += [f"routing:{note}" for note in routing.notes]
    review += [f"glossary:{conflict}" for conflict in glossary.conflicts]
    document_artifacts(run_dir, document, routing, inventory, glossary)
    run_info.update(
        {
            "lines": len(document.lines),
            "tables": len(document.tables),
            "criteria_found": len(inventory.criteria),
        }
    )
    if options.parse_only:
        write_json(run_dir / "manifest.json", run_info)
        return RunResult(run_dir, None, {"status": "parse_only"}, review)

    criteria = inventory.criteria
    if options.criteria:
        wanted = set(options.criteria)
        criteria = [c for c in criteria if c.criterion_id in wanted]
        review.append(f"partial_run: only {len(criteria)} of {len(inventory.criteria)} criteria were encoded")

    http, ledger, oncotree, terminology, registry_review = await load_registries(
        settings, run_dir, options.registry_cache, glossary, resume, http_transport
    )
    review += registry_review
    try:
        first_page = document.lines[0].page if document.lines else 1
        first_pages = "\n".join(
            line.text for line in document.page_lines(first_page, first_page + settings.metadata_pages - 1)
        )
        nct_id = find_nct_id(first_pages, pdf_path.name)
        registry = None
        if nct_id:
            try:
                registry, source = await fetch_study(http, nct_id)
                run_info["clinicaltrials_gov"] = source if registry else "not found"
            except RegistryError as error:
                review.append(f"clinicaltrials_unavailable: {error}")
        context = RunContext(
            settings=settings,
            document=document,
            routing=routing,
            inventory=inventory,
            glossary=glossary,
            ledger=ledger,
            terminology=terminology,
            oncotree=oncotree,
            recorder=ToolRecorder(run_dir / "tool_calls.jsonl", keep_existing=resume),
            run_dir=run_dir,
            registry_lines=registry_lines(registry),
            registry_source=f"ClinicalTrials.gov {nct_id}" if registry else "",
        )
        if client is None:
            from app.services.ctml.agents.client import build_chat_client

            client = build_chat_client(settings)
        run_info["agent_layout"] = settings.agent_layout
        workflow_evidence: dict | None = None
        if settings.agent_layout == "v5":
            from app.services.ctml.workflow import run_workflow

            flow = await run_workflow(client, context, run_dir, criteria, resume, registry, nct_id)
            metadata, design = flow.metadata, flow.design
            results = [flow.results[criterion.criterion_id] for criterion in criteria]
            review += flow.review
            workflow_evidence = flow.evidence
        else:
            metadata, design, outcomes = await run_agents(client, context, run_dir, criteria, resume)
            results = compact_results(criteria, outcomes)
    finally:
        run_info["registry_calls"] = {
            "live": http.live_calls,
            "cache": http.cache_hits,
            "lookups": ledger.counts(),
        }
        await http.aclose()

    failed_lookups = [r for r in ledger.all() if r.status == "error"]
    if failed_lookups:
        owners = sorted({r.owner for r in failed_lookups})
        review.append(
            f"registry_lookups_failed: {len(failed_lookups)} lookups failed "
            f"(owners {', '.join(owners[:10])}); their items may be unmapped. See lookups.jsonl."
        )
    fields, metadata_log, metadata_review = resolve_metadata(metadata.accepted, registry, nct_id)
    review += metadata_review
    if metadata.accepted is None:
        review.append(f"metadata_not_accepted: {failure_reason(metadata)}")
    ctml_path = None
    ctml = None
    evidence: dict = {
        "criteria": [criterion_evidence(r, "not placed (no accepted design)", glossary) for r in results]
    }
    if design.accepted is None:
        review.append(f"design_not_accepted: {failure_reason(design)}")
    else:
        ctml, evidence, compile_review = compile_ctml(
            fields, design.accepted, results, settings.omit_categories, glossary, oncotree
        )
        review += compile_review
        review += [f"confirmed_pattern:design:{c['code']}" for c in design.confirmations]
        review += figure_citations(document, design.accepted)
        review += registry_citations(design.accepted, context.registry_source)
        review += [f"agent_note:design:{note[:200]}" for note in design.accepted.review_notes]
        name = (fields.get("trial_id") or pdf_path.stem).replace("/", "_")
        ctml_path = run_dir / f"{name}_CTML.json"
        write_json(ctml_path, ctml)
    evidence["metadata"] = {"fields": fields, "log": metadata_log, "registry": registry}
    evidence["agents"] = {"metadata": metadata.summary(), "design": design.summary()}
    if workflow_evidence is not None:
        evidence["workflow"] = workflow_evidence
    write_json(run_dir / "evidence.json", evidence)
    run_info.update(
        {
            "trial_id": fields.get("trial_id"),
            "ctml": str(ctml_path) if ctml_path else None,
            "model": settings.foundry_model,
            "tool_calls": context.recorder.count,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seconds": round(time.monotonic() - started, 1),
        }
    )
    qa = build_qa(ctml, evidence, review, run_info)
    write_json(run_dir / "qa_report.json", qa)
    (run_dir / "qa_report.md").write_text(qa_markdown(qa, evidence, metadata_log), encoding="utf-8")
    write_json(run_dir / "manifest.json", run_info)
    return RunResult(run_dir, ctml_path, qa, review)
