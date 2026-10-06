"""Settings read from environment variables (a local .env file is loaded by the CLI)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, SecretStr

DEFAULT_OMIT_CATEGORIES = (
    "consent",
    "oral administration",
    "laboratory value",
    "contraception/pregnancy",
    "investigator judgment",
    "optional sub-study",
)


class SettingsError(ValueError):
    """A required setting is missing or invalid."""


V1_PATH = "/openai/v1/"


def openai_v1_url(endpoint: str) -> str:
    """The OpenAI v1 base URL of an Azure AI Foundry or Azure OpenAI resource.

    Accepts the v1 URL itself, the resource URL, or a Foundry project URL
    (https://<resource>.services.ai.azure.com/api/projects/<project>).
    """
    parts = urlsplit(endpoint.strip())
    if parts.scheme != "https" or not parts.netloc:
        raise SettingsError(f"FOUNDRY_OPENAI_ENDPOINT must be an https URL ending with {V1_PATH}")
    path = parts.path.rstrip("/")
    if path.endswith("/openai/v1"):
        return urlunsplit(("https", parts.netloc, path + "/", "", ""))
    if path in ("", "/openai") or path.startswith("/api/projects/"):
        return urlunsplit(("https", parts.netloc, V1_PATH, "", ""))
    raise SettingsError(f"FOUNDRY_OPENAI_ENDPOINT must end with {V1_PATH} (the path given is '{parts.path}')")


class Settings(BaseModel):
    di_endpoint: str = ""
    di_key: SecretStr | None = None
    di_features: tuple[str, ...] = ()
    foundry_endpoint: str = ""
    foundry_api_key: SecretStr | None = None
    foundry_model: str = ""
    reasoning_effort: str = ""
    runs_dir: Path = Path("runs")
    cache_dir: Path = Path(".cache/pmatch")
    max_parallel_agents: int = Field(default=4, ge=1, le=16)
    agent_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    metadata_pages: int = Field(default=5, ge=1, le=20)
    design_context_chars: int = Field(default=60000, ge=5000, le=400000)
    registry_timeout_seconds: float = Field(default=30.0, gt=0)
    omit_categories: tuple[str, ...] = DEFAULT_OMIT_CATEGORIES
    # "v5": the agents of architecture v5 (eligibility logic, clinical, genomics, prior therapy,
    # coverage and resolver beside metadata and study design) under a supervisor workflow.
    # "compact": one criterion agent per criterion (the earlier layout), for comparison.
    agent_layout: Literal["v5", "compact"] = "v5"
    # Targeted repair rounds after the coverage comparison and after the resolver (v5 only).
    max_repair_rounds: int = Field(default=1, ge=0, le=3)
    resolver: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        def env(*names: str) -> str:
            for name in names:
                value = os.environ.get(name, "").strip()
                if value:
                    return value
            return ""

        def secret(*names: str) -> SecretStr | None:
            value = env(*names)
            return SecretStr(value) if value else None

        def csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = os.environ.get(name)
            if raw is None:
                return default
            return tuple(part.strip() for part in raw.split(",") if part.strip())

        values: dict = {
            "di_endpoint": env("DOCUMENTINTELLIGENCE_ENDPOINT", "DI_ENDPOINT"),
            "di_key": secret("DOCUMENTINTELLIGENCE_KEY", "DI_KEY"),
            "di_features": csv("DI_FEATURES", ()),
            "foundry_endpoint": env("FOUNDRY_OPENAI_ENDPOINT"),
            "foundry_api_key": secret("FOUNDRY_API_KEY"),
            "foundry_model": env("FOUNDRY_MODEL_DEPLOYMENT"),
            "reasoning_effort": env("FOUNDRY_REASONING_EFFORT"),
            "omit_categories": csv("PMATCH_OMIT_CATEGORIES", DEFAULT_OMIT_CATEGORIES),
        }
        resolver = os.environ.get("PMATCH_RESOLVER", "").strip().casefold()
        if resolver:
            values["resolver"] = resolver not in ("0", "false", "no", "off")
        optional = {
            "runs_dir": "PMATCH_RUNS_DIR",
            "cache_dir": "PMATCH_CACHE_DIR",
            "max_parallel_agents": "PMATCH_MAX_PARALLEL_AGENTS",
            "agent_timeout_seconds": "PMATCH_AGENT_TIMEOUT_SECONDS",
            "metadata_pages": "PMATCH_METADATA_PAGES",
            "design_context_chars": "PMATCH_DESIGN_CONTEXT_CHARS",
            "registry_timeout_seconds": "PMATCH_REGISTRY_TIMEOUT_SECONDS",
            "agent_layout": "PMATCH_AGENT_LAYOUT",
            "max_repair_rounds": "PMATCH_MAX_REPAIR_ROUNDS",
        }
        for field_name, env_name in optional.items():
            raw = os.environ.get(env_name, "").strip()
            if raw:
                values[field_name] = raw.casefold() if field_name == "agent_layout" else raw
        return cls.model_validate(values)

    def require_document_intelligence(self) -> None:
        if not self.di_endpoint:
            raise SettingsError("DOCUMENTINTELLIGENCE_ENDPOINT is not set")

    def require_model(self) -> None:
        missing = [
            name
            for name, value in (
                ("FOUNDRY_OPENAI_ENDPOINT", self.foundry_endpoint),
                ("FOUNDRY_MODEL_DEPLOYMENT", self.foundry_model),
            )
            if not value
        ]
        if missing:
            raise SettingsError("Missing settings: " + ", ".join(missing))
        openai_v1_url(self.foundry_endpoint)  # a wrong URL form fails here, before any Azure call

    def public_view(self) -> dict:
        """Settings for the run manifest, without secrets."""
        data = self.model_dump(mode="json", exclude={"di_key", "foundry_api_key"})
        data["di_auth"] = "key" if self.di_key else "entra"
        data["foundry_auth"] = "key" if self.foundry_api_key else "entra"
        return data
