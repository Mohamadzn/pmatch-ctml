"""Registry clients, the lookup ledger and the search facade, with mocked HTTP."""

from __future__ import annotations

from http.client import IncompleteRead

import httpx
import pytest

from app.services.ctml.document.glossary import Definition, Glossary
from app.services.ctml.registries.clinicaltrials import (
    fetch_study,
    find_nct_id,
    registry_fields,
    registry_lines,
)
from app.services.ctml.registries.hgnc import search_genes
from app.services.ctml.registries.http import RegistryError, RegistryHttp
from app.services.ctml.registries.ledger import Ledger
from app.services.ctml.registries.ncit import concept_summary, search_concepts
from app.services.ctml.registries.oncotree import OncoTreeIndex, words
from app.services.ctml.registries.terminology import Terminology
from app.tests.ctml.synthetic import NCIT_CONCEPTS, ONCOTREE_NODES, mock_transport


@pytest.fixture
def oncotree() -> OncoTreeIndex:
    return OncoTreeIndex.from_nodes(ONCOTREE_NODES)


@pytest.fixture
async def http():
    client = RegistryHttp(transport=mock_transport())
    yield client
    await client.aclose()


# --- OncoTree ---------------------------------------------------------------------------


def test_words_folds_spelling_plurals_and_hyphenated_numbers():
    assert words("Tumours of the PD-1 pathway") == {"tumor", "pd1", "pathway"}
    assert words("PD-L1") == {"pd", "l1"}


def test_oncotree_search_hierarchy_and_tissue(oncotree):
    assert oncotree.search("melanoma")[0]["name"] == "Melanoma"
    assert oncotree.search("ACRAL")[0]["match"] == "code"
    first = oncotree.search("acral melanoma")[0]
    assert first["name"] == "Acral Melanoma"
    assert [p["name"] for p in first["parents"]] == ["Melanoma", "Skin"]
    assert oncotree.is_under("Acral Melanoma", "Melanoma") is True
    assert oncotree.is_under("Melanoma", "Acral Melanoma") is False
    assert oncotree.is_under("Unknown Disease", "Melanoma") is None
    assert oncotree.is_liquid("Diffuse Large B-Cell Lymphoma, NOS") is True
    assert oncotree.is_liquid("Melanoma") is False


# --- NCIt, HGNC, ClinicalTrials.gov ------------------------------------------------------


def test_concepts_that_are_neither_drug_nor_class():
    gene = {
        "code": "C1",
        "name": "Gene X",
        "properties": [{"type": "Semantic_Type", "value": "Gene or Genome"}],
    }
    assert concept_summary(gene)["kind"] == "other"
    functional = {
        "code": "C2",
        "name": "X Inhibitor",
        "properties": [{"type": "Semantic_Type", "value": "Chemical Viewed Functionally"}],
    }
    assert concept_summary(functional)["kind"] == "class"


def _concept(code: str, name: str, semantic_types: list[str], identifiers: dict[str, str]) -> dict:
    properties = [{"type": "Semantic_Type", "value": value} for value in semantic_types]
    properties += [{"type": key, "value": value} for key, value in identifiers.items()]
    return {"code": code, "name": name, "properties": properties}


def test_drug_dictionary_classes_are_classes():
    """NCIt classes keep "Chemical Viewed Functionally" even when the NCI Drug Dictionary lists
    them; single agents without any code are still drugs (examples as returned by EVS)."""
    cases = [
        (["Chemical Viewed Functionally"], {"NCI_Drug_Dictionary_ID": "801791"}, "class"),
        (["Chemical Viewed Functionally"], {"NCI_Drug_Dictionary_ID": "696645"}, "class"),
        (["Chemical Viewed Functionally"], {}, "class"),
        (["Classification", "Organic Chemical"], {}, "class"),
        (["Pharmacologic Substance"], {}, "drug"),
        (["Organic Chemical", "Pharmacologic Substance"], {"FDA_UNII_Code": "X"}, "drug"),
        (["Amino Acid, Peptide, or Protein", "Immunologic Factor"], {}, "other"),
        (["Therapeutic or Preventive Procedure"], {}, "other"),
    ]
    for index, (types, identifiers, expected) in enumerate(cases):
        summary = concept_summary(_concept(f"C{index}", f"Concept {index}", types, identifiers))
        assert summary["kind"] == expected, (types, identifiers)


def test_concept_summary_tells_drugs_from_classes():
    drug = concept_summary(NCIT_CONCEPTS["exampleumab"][0])
    drug_class = concept_summary(NCIT_CONCEPTS["anti-pd-1 antibody"][0])
    assert drug["kind"] == "drug" and drug["parents"][0]["name"] == "Anti-PD1 Monoclonal Antibody"
    assert drug_class["kind"] == "class" and "Anti-PD-1 Monoclonal Antibody" in drug_class["synonyms"]


async def test_ncit_search(http):
    candidates, source = await search_concepts(http, "exampleumab")
    assert source == "live" and candidates[0]["name"] == "Exampleumab"


async def test_hgnc_search(http):
    candidates, _ = await search_genes(http, "braf")
    assert candidates[0]["symbol"] == "BRAF" and candidates[0]["matched_by"] == "approved symbol"
    missing, _ = await search_genes(http, "NOTAGENE")
    assert missing == []


async def test_clinicaltrials_fetch(http):
    record, _ = await fetch_study(http, "NCT09999999")
    assert record["protocol_no"] == "EXP-123" and record["phases"] == ["PHASE2"]
    absent, _ = await fetch_study(http, "NCT00000001")
    assert absent is None
    assert find_nct_id("no id here", "Prot_NCT01234567.pdf") == "NCT01234567"


# --- HTTP retries and cache -------------------------------------------------------------


async def test_http_retries_then_caches(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.ctml.registries.http.asyncio.sleep", _no_sleep)
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True})

    client = RegistryHttp(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    data, source = await client.get_json("https://example.org/api", {"q": "x"})
    assert data == {"ok": True} and source == "live" and calls["count"] == 2
    again, source = await client.get_json("https://example.org/api", {"q": "x"})
    assert again == {"ok": True} and source == "cache" and calls["count"] == 2
    await client.aclose()


async def test_http_gives_up_on_client_errors(monkeypatch):
    monkeypatch.setattr("app.services.ctml.registries.http.asyncio.sleep", _no_sleep)
    client = RegistryHttp(transport=httpx.MockTransport(lambda request: httpx.Response(400)))
    with pytest.raises(RegistryError):
        await client.get_json("https://example.org/api")
    await client.aclose()


async def _no_sleep(_seconds: float) -> None:
    return None


# --- ledger and search facade -----------------------------------------------------------


def test_ledger_ids_and_resume(tmp_path):
    path = tmp_path / "lookups.jsonl"
    ledger = Ledger(path)
    first = ledger.add("ncit", "x", "INC-1", "ok", [{"name": "A", "parents": [{"name": "B"}]}])
    assert first.lookup_id == "lk-0001" and first.names() == {"A", "B"}
    resumed = Ledger(path, keep_existing=True)
    assert resumed.get("lk-0001").query == "x"
    assert resumed.add("hgnc", "y", "INC-2", "ok").lookup_id == "lk-0002"
    assert Ledger(path).all() == []  # a new run starts an empty ledger


async def test_search_diagnosis_uses_protocol_definition_and_flags_code_collision(http, oncotree):
    glossary = Glossary({"ACRAL": [Definition("ACRAL", "cutaneous melanoma of the palm", "L9", 1, 2)]})
    terminology = Terminology(http, Ledger(), oncotree, glossary)
    result = await terminology.search_diagnosis("ACRAL", owner="INC-3")
    assert result["lookup_id"] == "lk-0001"
    assert any("defines ACRAL" in note for note in result["notes"])
    code_hit = next(c for c in result["candidates"] if c["code"] == "ACRAL")
    assert "warning" in code_hit
    assert terminology.ledger.get("lk-0001").owner == "INC-3"


async def test_search_diagnosis_bridges_through_ncit(oncotree):
    def handler(request: httpx.Request) -> httpx.Response:
        concepts = [{"code": "C3224", "name": "Melanoma", "synonyms": [], "parents": [], "properties": []}]
        return httpx.Response(200, json={"concepts": concepts})

    client = RegistryHttp(transport=httpx.MockTransport(handler))
    terminology = Terminology(client, Ledger(), oncotree)
    result = await terminology.search_diagnosis("malignant neoplasm of melanocytes", owner="INC-1")
    bridged = [c for c in result["candidates"] if c["match"] == "ncit_bridge"]
    assert bridged and bridged[0]["name"] == "Melanoma" and bridged[0]["bridge"]["ncit_code"] == "C3224"
    await client.aclose()


async def test_search_errors_are_recorded_not_raised(monkeypatch):
    monkeypatch.setattr("app.services.ctml.registries.http.asyncio.sleep", _no_sleep)
    client = RegistryHttp(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    ledger = Ledger()
    terminology = Terminology(client, ledger, None)
    result = await terminology.search_therapy("anything", owner="EXC-1")
    assert "error" in result and ledger.get(result["lookup_id"]).status == "error"
    no_tree = await terminology.search_diagnosis("melanoma", owner="EXC-1")
    assert "not available" in no_tree["error"]
    await client.aclose()


async def test_a_403_is_retried_with_the_standard_library_client(tmp_path):
    calls = []

    def fallback(url, params, timeout):
        calls.append((url, params))
        return 200, b'{"ok": true}'

    client = RegistryHttp(
        cache_dir=tmp_path, transport=httpx.MockTransport(lambda r: httpx.Response(403)), fallback=fallback
    )
    data, source = await client.get_json("https://registry.example/api", params={"q": "x"})
    assert (
        data == {"ok": True} and source == "live" and calls == [("https://registry.example/api", {"q": "x"})]
    )
    refused = RegistryHttp(
        transport=httpx.MockTransport(lambda r: httpx.Response(403)), fallback=lambda *a: (403, b"")
    )
    with pytest.raises(RegistryError, match="403"):
        await refused.get_json("https://registry.example/api")

    def broken(*_):
        raise IncompleteRead(b"")

    cut = RegistryHttp(transport=httpx.MockTransport(lambda r: httpx.Response(403)), fallback=broken)
    with pytest.raises(RegistryError, match="also refused"):
        await cut.get_json("https://registry.example/api")
    await client.aclose()
    await refused.aclose()
    await cut.aclose()


def test_registry_arms_become_citeable_lines():
    record = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT09999999"},
            "armsInterventionsModule": {
                "armGroups": [
                    {
                        "label": "Drugex + Drugy",
                        "type": "EXPERIMENTAL",
                        "description": "Drugex 400 mg every three weeks (Q3W) and Drugy.",
                        "interventionNames": ["Drug: Drugex"],
                    }
                ],
                "interventions": [{"type": "DRUG", "name": "Drugex", "description": "IV infusion"}],
            },
        }
    }
    lines = registry_lines(registry_fields(record))
    assert lines == {
        "R1": "Arm 'Drugex + Drugy' (EXPERIMENTAL; Drug: Drugex): "
        "Drugex 400 mg every three weeks (Q3W) and Drugy.",
        "R2": "Intervention 'Drugex' (DRUG): IV infusion",
    }
    assert registry_lines(None) == {}
