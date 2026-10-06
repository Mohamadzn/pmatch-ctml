"""Command line: python -m app.services.ctml <command> ...

Commands:
  run    <pdf>      Full run: PDF to CTML review candidate (Azure Document Intelligence + model).
  parse  <pdf>      Document lane only: lines, sections, glossary and criteria. No model calls.
  batch  <folder>   Run every PDF in a folder, one after another.
  check  <ctml>     Schema and output-policy check of a CTML file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from app.services.ctml.config import Settings, SettingsError
from app.services.ctml.document.di_parser import ParseError
from app.services.ctml.pipeline import RunOptions, new_run_dir, run_pipeline

logger = logging.getLogger("app.services.ctml")


def setup_logging() -> None:
    """Console logging. Each run also writes run.log in its own folder (pipeline.py)."""
    for stream in (sys.stdout, sys.stderr):
        # Windows consoles may not be UTF-8; protocol text holds signs such as ≥.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    level = os.environ.get("PMATCH_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    # HTTP client logs can include request URLs with query text; keep them quiet.
    for noisy in ("httpx", "httpcore", "azure", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load_env() -> None:
    """Settings from .env in the folder the command runs in (the project folder)."""
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True), override=False)


def _options(args: argparse.Namespace) -> RunOptions:
    run_dir = Path(args.run_dir) if getattr(args, "run_dir", None) else None
    return RunOptions(
        di_json=Path(args.di_json) if getattr(args, "di_json", None) else None,
        pages=getattr(args, "pages", None),
        run_dir=run_dir,
        resume=run_dir is not None and run_dir.exists(),
        refresh_di=getattr(args, "refresh_di", False),
        registry_cache=not getattr(args, "no_registry_cache", False),
        parse_only=args.command == "parse",
        criteria=[c.strip() for c in args.criteria.split(",")] if getattr(args, "criteria", None) else None,
    )


def _summary(result) -> str:
    qa = result.qa
    lines = [f"Run folder: {result.run_dir}"]
    if qa.get("status") == "parse_only":
        lines.append(f"Criteria found: see {result.run_dir / 'document' / 'inventory.md'}")
    if result.ctml_path:
        lines.append(f"CTML: {result.ctml_path}")
    lines.append(f"Status: {qa.get('status')}{' (blocked)' if qa.get('blocked') else ''}")
    if qa.get("counts"):
        lines.append("Counts: " + json.dumps(qa["counts"]))
    if qa.get("blocking"):
        lines.append(f"Blocking issues: {len(qa['blocking'])} (see qa_report.md)")
    return "\n".join(lines)


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    pdf = Path(args.pdf)
    options = _options(args)
    options.run_dir = options.run_dir or new_run_dir(settings, pdf)
    setup_logging()
    logger.info("Settings: %s", json.dumps(settings.public_view(), default=str))
    try:
        result = await run_pipeline(pdf, settings, options)
    except SettingsError as error:
        print(f"Settings error: {error}. Copy .env.example to .env and fill it in.")
        return 2
    except (ParseError, ValueError) as error:
        print(f"Error: {error}")
        return 2
    print(_summary(result))
    return 0


async def _batch(args: argparse.Namespace, settings: Settings) -> int:
    setup_logging()
    pdfs = sorted(Path(args.folder).glob("*.pdf"))
    if not pdfs:
        print(f"No PDF files in {args.folder}")
        return 1
    failures = 0
    for pdf in pdfs:
        options = RunOptions(registry_cache=not args.no_registry_cache)
        try:
            result = await run_pipeline(pdf, settings, options)
            print(_summary(result))
        except Exception as error:  # one failed protocol must not stop the batch
            failures += 1
            logger.exception("%s failed: %s", pdf.name, error)
    return 1 if failures else 0


def _check(args: argparse.Namespace) -> int:
    from app.services.ctml.qa import policy_errors
    from app.services.ctml.schema.loader import validation_errors

    document = json.loads(Path(args.ctml).read_text(encoding="utf-8"))
    schema = validation_errors(document, limit=200)
    policy = policy_errors(document)
    for error in schema:
        print(f"schema:{error['path']}: {error['message']}")
    for problem in policy:
        print(f"policy:{problem}")
    print(f"schema errors: {len(schema)}; policy errors: {len(policy)}")
    return 1 if schema or policy else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.services.ctml", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "parse"):
        command = sub.add_parser(name)
        command.add_argument("pdf", help="Protocol PDF")
        command.add_argument(
            "--di-json", help="Use this saved Document Intelligence layout JSON (no Azure call)"
        )
        command.add_argument("--pages", help="Use only these pages, for example 12-140")
        command.add_argument("--run-dir", help="Run folder; an existing folder resumes that run")
        command.add_argument(
            "--refresh-di", action="store_true", help="Ignore the Document Intelligence cache"
        )
        if name == "run":
            command.add_argument("--no-registry-cache", action="store_true", help="Call registries live only")
            command.add_argument("--criteria", help="Encode only these criterion IDs, e.g. INC-1,EXC-3")
    batch = sub.add_parser("batch")
    batch.add_argument("folder")
    batch.add_argument("--no-registry-cache", action="store_true")
    check = sub.add_parser("check")
    check.add_argument("ctml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return _check(args)
    load_env()
    try:
        settings = Settings.from_env()
        # Document Intelligence settings are checked only when a live call is needed
        # (a cached or imported layout needs none).
        if args.command in ("run", "batch"):
            settings.require_model()
    except SettingsError as error:
        print(f"Settings error: {error}. Copy .env.example to .env and fill it in.")
        return 2
    if args.command == "batch":
        return asyncio.run(_batch(args, settings))
    return asyncio.run(_run(args, settings))
