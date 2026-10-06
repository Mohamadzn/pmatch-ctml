"""HTTP routes: check a CTML document, start a run from an uploaded PDF, read a run's status."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, File, HTTPException, UploadFile

from app.services.ctml.config import Settings, SettingsError
from app.services.ctml.pipeline import RunOptions, new_run_dir, run_pipeline
from app.services.ctml.qa import policy_errors
from app.services.ctml.schema.loader import validation_errors

router = APIRouter(prefix="/ctml", tags=["ctml"])

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,150}$")


def _safe_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(name or "protocol.pdf").name)
    return stem if stem.lower().endswith(".pdf") else f"{stem}.pdf"


@router.post("/check")
def check_document(document: Annotated[dict, Body()]) -> dict:
    """Schema and output-policy check of a CTML document."""
    schema = validation_errors(document, limit=200)
    policy = policy_errors(document)
    return {"valid": not schema and not policy, "schema_errors": schema, "policy_errors": policy}


@router.post("/runs", status_code=202)
async def start_run(background: BackgroundTasks, pdf: Annotated[UploadFile, File()]) -> dict:
    """Save the PDF and start a full run in the background. Poll /ctml/runs/{run}."""
    settings = Settings.from_env()
    try:
        settings.require_model()
    except SettingsError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    uploads = settings.runs_dir / "uploads" / secrets.token_hex(4)  # uploads with one name never collide
    uploads.mkdir(parents=True, exist_ok=True)
    target = uploads / _safe_name(pdf.filename or "protocol.pdf")
    size = 0
    with target.open("wb") as handle:
        while chunk := await pdf.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                handle.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="The PDF is larger than 200 MB.")
            handle.write(chunk)
    run_dir = new_run_dir(settings, target)
    background.add_task(run_pipeline, target, settings, RunOptions(run_dir=run_dir))
    return {"run": run_dir.name, "status_url": f"/ctml/runs/{run_dir.name}"}


@router.get("/runs/{name}")
def run_status(name: str) -> dict:
    if not _SAFE_NAME.match(name) or ".." in name:
        raise HTTPException(status_code=400, detail="Invalid run name.")
    run_dir = Settings.from_env().runs_dir / name
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="No such run.")
    qa_file = run_dir / "qa_report.json"
    if not qa_file.exists():
        return {"run": name, "state": "running or failed; see run.log"}
    qa = json.loads(qa_file.read_text(encoding="utf-8"))
    return {
        "run": name,
        "state": "finished",
        "status": qa.get("status"),
        "blocked": qa.get("blocked"),
        "counts": qa.get("counts"),
        "ctml": qa.get("run", {}).get("ctml"),
    }
