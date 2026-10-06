"""Command line, HTTP API and the project's hard rules."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.ctml import cli
from app.services.ctml.agents.client import build_chat_client
from app.services.ctml.config import Settings, SettingsError, openai_v1_url
from app.tests.ctml.synthetic import PAGES, build_layout
from app.tests.ctml.test_pipeline_offline import blank_pdf

SERVICE = Path(__file__).resolve().parents[2] / "services" / "ctml"
VALID_CTML = {
    "trial_id": "NCT09999999",
    "long_title": "Title",
    "short_title": "Title",
    "protocol_no": "EXP-123",
    "phase": "II",
    "nct_purpose": "Purpose.",
    "principal_investigator": "",
    "drug_list": {"drug": [{}]},
    "management_group_list": {"management_group": [{}]},
    "site_list": {"site": [{}]},
    "sponsor_list": {"sponsor": [{"sponsor_name": "Sponsor", "is_principal_sponsor": "Y"}]},
    "staff_list": {"protocol_staff": []},
    "treatment_list": {
        "step": [
            {
                "arm": [
                    {
                        "arm_code": "Arm A",
                        "arm_suspended": "N",
                        "arm_description": "Arm A.",
                        "uuid": "u",
                        "dose_level": [{"level_code": "drugex", "level_description": "100 mg PO QD"}],
                        "match": [{"and": [{"clinical": {"age_expression": ">=18"}}]}],
                        "arm_additional_criteria_not_captured": [],
                    }
                ]
            }
        ]
    },
    "additional_criteria_requirements": [],
}


# --- command line -----------------------------------------------------------------------


def test_check_command(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(VALID_CTML), encoding="utf-8")
    assert cli.main(["check", str(good)]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**VALID_CTML, "phase": "2", "status": "Recruiting"}), encoding="utf-8")
    assert cli.main(["check", str(bad)]) == 1
    assert "schema errors: 1; policy errors: 1" in capsys.readouterr().out


def test_parse_command_needs_no_model(tmp_path, monkeypatch, capsys):
    for name in ("FOUNDRY_OPENAI_ENDPOINT", "FOUNDRY_MODEL_DEPLOYMENT", "DOCUMENTINTELLIGENCE_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PMATCH_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.chdir(tmp_path)  # no .env here
    pdf = blank_pdf(tmp_path / "Prot_SYNTHETIC.pdf", len(PAGES))
    layout = tmp_path / "layout.json"
    layout.write_text(json.dumps(build_layout()), encoding="utf-8")
    run_dir = tmp_path / "runs" / "parse1"
    assert cli.main(["parse", str(pdf), "--di-json", str(layout), "--run-dir", str(run_dir)]) == 0
    inventory = (run_dir / "document" / "inventory.md").read_text(encoding="utf-8")
    assert "| EXC-3 | exclusion | all |" in inventory
    assert "## Group titles" in inventory
    assert (run_dir / "run.log").exists()


def test_run_command_requires_model_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FOUNDRY_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("FOUNDRY_MODEL_DEPLOYMENT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", str(tmp_path / "missing.pdf")]) == 2
    assert "FOUNDRY_OPENAI_ENDPOINT" in capsys.readouterr().out


# --- model endpoint ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://r.services.ai.azure.com/openai/v1/", "https://r.services.ai.azure.com/openai/v1/"),
        ("https://r.services.ai.azure.com/openai/v1", "https://r.services.ai.azure.com/openai/v1/"),
        ("https://r.services.ai.azure.com/", "https://r.services.ai.azure.com/openai/v1/"),
        ("https://r.openai.azure.com", "https://r.openai.azure.com/openai/v1/"),
        ("https://r.services.ai.azure.com/api/projects/p1", "https://r.services.ai.azure.com/openai/v1/"),
    ],
)
def test_openai_v1_url(given, expected):
    assert openai_v1_url(given) == expected


@pytest.mark.parametrize(
    "given", ["http://r.openai.azure.com/openai/v1/", "https://r.openai.azure.com/openai/deployments/d", "x"]
)
def test_openai_v1_url_rejects_other_forms(given):
    with pytest.raises(SettingsError):
        openai_v1_url(given)


def test_chat_client_uses_the_v1_endpoint():
    """Builds the client without any network call."""
    settings = Settings(
        foundry_endpoint="https://r.services.ai.azure.com/api/projects/p1",
        foundry_model="deployment-1",
        foundry_api_key="k" * 20,
    )
    client = build_chat_client(settings)
    assert str(client.client.base_url) == "https://r.services.ai.azure.com/openai/v1/"
    assert client.model == "deployment-1"


# --- HTTP API ---------------------------------------------------------------------------


def test_api(tmp_path, monkeypatch):
    monkeypatch.setenv("PMATCH_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("FOUNDRY_OPENAI_ENDPOINT", raising=False)
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.post("/ctml/check", json=VALID_CTML).json()["valid"] is True
    assert client.post("/ctml/check", json={"trial_id": ""}).json()["valid"] is False
    assert client.get("/ctml/runs/..%2Fsecret").status_code in (400, 404)
    assert client.get("/ctml/runs/unknown-run").status_code == 404
    upload = client.post("/ctml/runs", files={"pdf": ("p.pdf", b"%PDF-1.4", "application/pdf")})
    assert upload.status_code == 503  # no model configured


# --- hard rules -------------------------------------------------------------------------


def _runtime_sources() -> list[Path]:
    return [p for p in SERVICE.rglob("*.py")]


def test_runtime_code_never_reads_gold_or_evaluation():
    for path in _runtime_sources():
        text = path.read_text(encoding="utf-8")
        assert "GoldStandard" not in text, path
        assert not re.search(r"^\s*(from|import)\s+evaluation", text, re.M), path


def test_no_term_lists_ship_with_the_service():
    """The only data file of the service is the CTML schema: no vocabulary, synonym or mapping files."""
    data_files = [p.name for p in SERVICE.rglob("*") if p.is_file() and p.suffix not in (".py", ".pyc")]
    assert data_files == ["ctml.schema.json"]


def test_no_credentials_in_code():
    pattern = re.compile(r"(api[_-]?key|secret|password)\s*=\s*['\"][A-Za-z0-9+/=_-]{16,}['\"]", re.I)
    for path in SERVICE.parent.parent.rglob("*.py"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path
