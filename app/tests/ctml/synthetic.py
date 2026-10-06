"""A small synthetic protocol and fake registries for offline tests.

The protocol text is invented. It exercises the document lane (running headers, numbered
criteria, population labels, a glossary table) and gives the agents something to encode.
The registry responses follow the public API formats with invented content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx


@dataclass
class TableBlock:
    rows: list[list[str]]
    caption: str = ""


HEADER = '<!-- PageHeader="Example Pharma Inc. - Confidential" -->'

PAGES: list[list[object]] = [
    [
        HEADER,
        "# A Phase 2 Study of Drugex in Adults with Advanced Solid Tumors",
        "Protocol Number: EXP-123",
        "Amendment 3",
        "Date: 12 March 2024",
        "Sponsor: Example Pharma Inc.",
        "ClinicalTrials.gov: NCT09999999",
    ],
    [
        HEADER,
        "# 1 SYNOPSIS",
        "This study will evaluate the safety and efficacy of drugex in adults with advanced solid tumors.",
        "# 2 STUDY DESIGN",
        "This is an open-label, two-part study.",
        "Part 1 is dose escalation of drugex, starting at 100 mg orally once daily.",
        "Part 2 is dose expansion of drugex at 200 mg orally once daily in participants with melanoma.",
    ],
    [
        HEADER,
        "# 5 STUDY POPULATION",
        "## 5.1 Inclusion Criteria",
        "Participants must meet all of the following criteria:",
        "1\\. Age 18 years or older.",
        "2\\. Part 1: Histologically confirmed advanced solid tumor.",
        "3\\. Part 2: Histologically confirmed melanoma.",
        "4\\. ECOG performance status of 0 or 1.",
    ],
    [
        HEADER,
        "## 5.2 Exclusion Criteria",
        "1\\. Prior treatment with an anti-PD-1 antibody.",
        "2\\. Prior allogeneic stem cell transplant.",
        "3\\. Known active CNS metastases.",
        "# 12 APPENDIX",
        "## 12.1 List of Abbreviations",
        TableBlock(
            [
                ["Abbreviation", "Definition"],
                ["CNS", "central nervous system"],
                ["ECOG", "Eastern Cooperative Oncology Group"],
            ]
        ),
    ],
]


def _html_table(rows: list[list[str]]) -> str:
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


def build_layout(pages: list[list[object]] | None = None) -> dict:
    """A Document Intelligence layout result (markdown content) for the given pages.

    As in Document Intelligence markdown, a blank line separates blocks (paragraphs). A block
    with a line break inside it is one paragraph printed on several lines."""
    pages = PAGES if pages is None else pages
    content = ""
    page_entries = []
    tables = []
    for number, blocks in enumerate(pages, start=1):
        start = len(content)
        for block in blocks:
            if isinstance(block, TableBlock):
                text = _html_table(block.rows)
                offset = len(content)
                cells = [
                    {"rowIndex": r, "columnIndex": c, "content": cell}
                    for r, row in enumerate(block.rows)
                    for c, cell in enumerate(row)
                ]
                table = {
                    "rowCount": len(block.rows),
                    "columnCount": max(len(row) for row in block.rows),
                    "cells": cells,
                    "spans": [{"offset": offset, "length": len(text)}],
                }
                if block.caption:
                    table["caption"] = {"content": block.caption}
                tables.append(table)
                content += text + "\n"
            else:
                content += str(block) + "\n\n"
        content += f'<!-- PageNumber="{number}" -->\n<!-- PageBreak -->\n'
        page_entries.append(
            {"pageNumber": number, "spans": [{"offset": start, "length": len(content) - start}]}
        )
    return {
        "apiVersion": "2024-11-30",
        "modelId": "prebuilt-layout",
        "contentFormat": "markdown",
        "content": content,
        "pages": page_entries,
        "tables": tables,
    }


# --- registries ---------------------------------------------------------------------------

ONCOTREE_NODES = [
    {
        "code": "SKIN",
        "name": "Skin",
        "mainType": "Skin Cancer, Non-Melanoma",
        "tissue": "Skin",
        "parent": "TISSUE",
        "level": 1,
        "externalReferences": {"NCI": ["C12470"]},
    },
    {
        "code": "MEL",
        "name": "Melanoma",
        "mainType": "Melanoma",
        "tissue": "Skin",
        "parent": "SKIN",
        "level": 2,
        "externalReferences": {"NCI": ["C3224"]},
    },
    {
        "code": "ACRAL",
        "name": "Acral Melanoma",
        "mainType": "Melanoma",
        "tissue": "Skin",
        "parent": "MEL",
        "level": 3,
        "externalReferences": {"NCI": ["C4022"]},
    },
    {
        "code": "LNM",
        "name": "Lymphoid Neoplasm",
        "mainType": "Lymphoid Cancer",
        "tissue": "Lymphoid",
        "parent": "TISSUE",
        "level": 1,
        "externalReferences": {},
    },
    {
        "code": "DLBCLNOS",
        "name": "Diffuse Large B-Cell Lymphoma, NOS",
        "mainType": "Non-Hodgkin Lymphoma",
        "tissue": "Lymphoid",
        "parent": "LNM",
        "level": 2,
        "externalReferences": {"NCI": ["C8851"]},
    },
    {
        "code": "LUNG",
        "name": "Lung",
        "mainType": "Lung Cancer",
        "tissue": "Lung",
        "parent": "TISSUE",
        "level": 1,
        "externalReferences": {},
    },
    {
        "code": "NSCLC",
        "name": "Non-Small Cell Lung Cancer",
        "mainType": "Non-Small Cell Lung Cancer",
        "tissue": "Lung",
        "parent": "LUNG",
        "level": 2,
        "externalReferences": {"NCI": ["C2926"]},
    },
]

NCIT_CONCEPTS = {
    "anti-pd-1 antibody": [
        {
            "code": "C128037",
            "name": "Anti-PD1 Monoclonal Antibody",
            "synonyms": [{"name": "Anti-PD-1 Monoclonal Antibody"}],
            "parents": [{"code": "C124946", "name": "PD1 Inhibitor"}],
            # NCIt drug classes carry this semantic type, often with an NCI drug dictionary ID.
            "properties": [
                {"type": "Semantic_Type", "value": "Chemical Viewed Functionally"},
                {"type": "NCI_Drug_Dictionary_ID", "value": "999001"},
            ],
        }
    ],
    "exampleumab": [
        {
            "code": "C900001",
            "name": "Exampleumab",
            "synonyms": [{"name": "EX-1"}],
            "parents": [{"code": "C128037", "name": "Anti-PD1 Monoclonal Antibody"}],
            "properties": [
                {"type": "Semantic_Type", "value": "Pharmacologic Substance"},
                {"type": "FDA_UNII_Code", "value": "XXXX"},
            ],
        }
    ],
}

HGNC_DOCS = {
    "BRAF": {
        "symbol": "BRAF",
        "hgnc_id": "HGNC:1097",
        "name": "B-Raf proto-oncogene",
        "alias_symbol": ["BRAF1"],
        "prev_symbol": [],
        "status": "Approved",
    },
}

CTGOV_STUDY = {
    "protocolSection": {
        "identificationModule": {
            "nctId": "NCT09999999",
            "officialTitle": "A Phase 2 Study of Drugex in Adults with Advanced Solid Tumors",
            "briefTitle": "Drugex in Advanced Solid Tumors",
            "orgStudyIdInfo": {"id": "EXP-123"},
        },
        "descriptionModule": {"briefSummary": "This study tests drugex. It has two parts."},
        "designModule": {"phases": ["PHASE2"]},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Example Pharma Inc."}},
    }
}


def registry_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "oncotree" in url:
        return httpx.Response(200, json=ONCOTREE_NODES)
    if "api-evsrest" in url:
        term = request.url.params.get("term", "").casefold()
        concepts = NCIT_CONCEPTS.get(term, [])
        return httpx.Response(200, json={"total": len(concepts), "concepts": concepts})
    if "genenames" in url:
        parts = request.url.path.split("/")
        field, value = parts[-2], parts[-1]
        doc = HGNC_DOCS.get(value.upper()) if field == "symbol" else None
        return httpx.Response(
            200, json={"response": {"numFound": int(bool(doc)), "docs": [doc] if doc else []}}
        )
    if "clinicaltrials.gov" in url:
        if url.split("?")[0].endswith("NCT09999999"):
            return httpx.Response(200, json=CTGOV_STUDY)
        return httpx.Response(404, json={})
    return httpx.Response(404, json={})


def mock_transport() -> httpx.MockTransport:
    return httpx.MockTransport(registry_handler)


def layout_json() -> str:
    return json.dumps(build_layout())
