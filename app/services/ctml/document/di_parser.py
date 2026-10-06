"""Azure Document Intelligence parsing (prebuilt-layout, markdown output) with a local cache.

A result is cached under the PDF's SHA-256 and the analysis options, so running the same
PDF again makes no new Azure call. Every result says where it came from: "live" (a new
Azure call in this run), "cache" (an earlier live call) or "import" (a JSON file given
with --di-json).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from app.services.ctml.config import Settings

logger = logging.getLogger(__name__)

MODEL_ID = "prebuilt-layout"
CONTENT_FORMAT = "markdown"
STRING_INDEX = "unicodeCodePoint"


class ParseError(RuntimeError):
    """The PDF or an imported layout result cannot be used."""


@dataclass(frozen=True)
class ParseResult:
    result: dict
    source: str  # "live", "cache" or "import"
    sha256: str
    options: dict
    cache_file: str = ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def pdf_page_count(path: Path) -> int | None:
    """Page count from the PDF itself, or None when the file cannot be read."""
    try:
        return len(PdfReader(str(path)).pages)
    except Exception as error:  # pypdf raises many types for damaged files
        logger.warning("Could not read page count from %s: %s", path.name, error)
        return None


def analysis_options(settings: Settings) -> dict:
    return {
        "model_id": MODEL_ID,
        "output_content_format": CONTENT_FORMAT,
        "string_index_type": STRING_INDEX,
        "features": sorted(settings.di_features),
    }


def _options_key(options: dict) -> str:
    return hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()[:12]


def cache_file(settings: Settings, sha256: str, options: dict) -> Path:
    return settings.cache_dir / "di" / f"{sha256}.{_options_key(options)}.json"


def unwrap_layout(data: dict) -> dict:
    """The layout result from a raw result, a REST response or a pmatch document file."""
    for key in ("analyzeResult", "layout", "result"):
        inner = data.get(key)
        if isinstance(inner, dict) and "content" in inner:
            return unwrap_layout(inner)
    if "content" not in data or "pages" not in data:
        raise ParseError("The JSON file is not a Document Intelligence layout result (no content or pages).")
    return data


def check_layout(result: dict) -> None:
    content_format = str(result.get("contentFormat", CONTENT_FORMAT)).lower()
    if content_format != CONTENT_FORMAT:
        raise ParseError(
            f"The layout result uses '{content_format}' content. "
            "Re-run Document Intelligence with markdown output."
        )


def load_di_json(path: Path, pdf_sha256: str) -> ParseResult:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ParseError(f"Cannot read {path.name}: {error}") from error
    result = unwrap_layout(data)
    check_layout(result)
    return ParseResult(result, "import", pdf_sha256, {"imported_from": path.name})


def _read_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        result = unwrap_layout(record)
        check_layout(result)
        return result
    except (OSError, json.JSONDecodeError, ParseError) as error:
        logger.warning("Ignoring unusable cache file %s: %s", path.name, error)
        return None


async def _analyze_live(pdf_path: Path, settings: Settings, options: dict) -> dict:
    # Imported here so that offline commands work without the Azure packages configured.
    from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
    from azure.core.credentials import AzureKeyCredential
    from azure.identity.aio import DefaultAzureCredential

    settings.require_document_intelligence()
    entra = None
    if settings.di_key is not None:
        credential = AzureKeyCredential(settings.di_key.get_secret_value())
    else:
        entra = DefaultAzureCredential()
        credential = entra
    try:
        async with DocumentIntelligenceClient(settings.di_endpoint, credential) as client:
            with pdf_path.open("rb") as handle:
                poller = await client.begin_analyze_document(
                    options["model_id"],
                    body=handle,
                    content_type="application/octet-stream",
                    output_content_format=options["output_content_format"],
                    string_index_type=options["string_index_type"],
                    features=options["features"] or None,
                )
                analyzed = await poller.result()
    finally:
        if entra is not None:
            await entra.close()
    return analyzed.as_dict()


async def parse_pdf(
    pdf_path: Path,
    settings: Settings,
    di_json: Path | None = None,
    refresh: bool = False,
) -> ParseResult:
    """Layout result for a PDF: imported JSON, the cache, or a live Azure call."""
    if not pdf_path.exists():
        raise ParseError(f"PDF not found: {pdf_path}")
    sha256 = file_sha256(pdf_path)
    if di_json is not None:
        return load_di_json(di_json, sha256)
    options = analysis_options(settings)
    path = cache_file(settings, sha256, options)
    if not refresh:
        cached = _read_cache(path)
        if cached is not None:
            logger.info("Document Intelligence: using cached result %s (no Azure call)", path.name)
            return ParseResult(cached, "cache", sha256, options, str(path))
    logger.info("Document Intelligence: live call for %s (%s)", pdf_path.name, options["model_id"])
    result = await _analyze_live(pdf_path, settings, options)
    check_layout(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "sha256": sha256,
        "options": options,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "result": result,
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return ParseResult(result, "live", sha256, options, str(path))
