from __future__ import annotations

import os

import pytest

from app.services.ctml.config import Settings
from app.services.ctml.document.glossary import build_glossary
from app.services.ctml.document.inventory import build_inventory
from app.services.ctml.document.model import build_document
from app.services.ctml.document.sections import route
from app.services.ctml.registries.ledger import Ledger
from app.services.ctml.registries.oncotree import OncoTreeIndex
from app.services.ctml.runtime import AgentState, RunContext
from app.tests.ctml.synthetic import ONCOTREE_NODES, build_layout


def make_context() -> RunContext:
    document = build_document(build_layout(), "sha")
    routing = route(document)
    return RunContext(
        settings=Settings(),
        document=document,
        routing=routing,
        inventory=build_inventory(document, routing),
        glossary=build_glossary(document, routing),
        ledger=Ledger(),
        oncotree=OncoTreeIndex.from_nodes(ONCOTREE_NODES),
    )


@pytest.fixture
def context() -> RunContext:
    return make_context()


@pytest.fixture
def state_for():
    def build(owner: str) -> AgentState:
        return AgentState(owner=owner)

    return build


@pytest.fixture(autouse=True)
def no_azure_settings(monkeypatch):
    """Tests never reach Azure, even when a .env with real settings was loaded."""
    for name in list(os.environ):
        if name.startswith(("FOUNDRY_", "DOCUMENTINTELLIGENCE_", "DI_", "PMATCH_")):
            monkeypatch.delenv(name, raising=False)
