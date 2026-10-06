"""Document lane: text cleanup, lines, sections, criteria, glossary and bound documents.
All inputs are synthetic."""

from __future__ import annotations

import pytest

from app.services.ctml.document.glossary import align, build_glossary, trusted_rows
from app.services.ctml.document.inventory import build_inventory, normalize_scope
from app.services.ctml.document.model import build_document
from app.services.ctml.document.sections import parse_heading, route
from app.services.ctml.document.segments import find_segments, parse_pages
from app.services.ctml.qa import BLOCKING_PREFIXES
from app.services.ctml.text import clean_line, comparison_key, contains_phrase, latex_to_text
from app.tests.ctml.synthetic import HEADER, PAGES, TableBlock, build_layout


def document_of(pages):
    return build_document(build_layout(pages), "sha")


# --- text -------------------------------------------------------------------------------


def test_clean_line_removes_markup():
    assert clean_line("1\\. Age &ge; 18 <b>years</b> :selected:") == "1. Age ≥ 18 years"
    assert clean_line("ANC $\\geq 1.5 \\times 10^{9}$/L") == "ANC ≥1.5 × 10^9 /L"


def test_comparison_signs_are_not_mistaken_for_tags():
    assert (
        clean_line("Platelets <100 × 10^9/L or hemoglobin >9 g/dL")
        == "Platelets <100 × 10^9/L or hemoglobin >9 g/dL"
    )
    assert clean_line("ANC \\< 1.5 and creatinine \\> 1.5 x ULN") == "ANC < 1.5 and creatinine > 1.5 x ULN"
    assert clean_line('<td rowspan="2">ALT</td><br/>') == "ALT"


def test_latex_joins_split_digits():
    assert latex_to_text("1 8") == "18"
    assert latex_to_text("\\mathrm{m g}") == "mg"


def test_comparison_key_unifies_quotes_and_dashes():
    assert comparison_key("Gilbert’s  syndrome – rare") == comparison_key("Gilbert's syndrome - rare")
    assert contains_phrase("Prior anti‑PD‑1 therapy", "anti-PD-1")
    assert not contains_phrase("text", "")


# --- lines ------------------------------------------------------------------------------


def test_lines_tables_and_running_headers():
    document = document_of(PAGES)
    texts = [line.text for line in document.lines]
    running = [line for line in document.lines if line.kind == "running"]
    assert [line.text for line in running] == ["Example Pharma Inc. - Confidential"]
    assert running[0].page == 1
    assert not any(text.isdigit() for text in texts)  # page numbers are comments, never lines
    assert document.lines[0].line_id == "L1"
    table_lines = [line for line in document.lines if line.kind == "table"]
    assert len(table_lines) == 1 and table_lines[0].text.startswith("[Table T1]")
    assert document.tables["T1"].rows[1] == ["CNS", "central nervous system"]


def test_repeated_footer_without_comment_is_running_text():
    pages = [
        [f"# {n} Section {n}", f"Body text of page {n}.", "Study XYZ-9 Final Version"] for n in range(1, 6)
    ]
    document = document_of(pages)
    footers = [line for line in document.lines if line.text == "Study XYZ-9 Final Version"]
    assert len(footers) == 1 and footers[0].kind == "running"


# --- sections ---------------------------------------------------------------------------


def test_parse_heading():
    assert parse_heading("5.1.2 Subject Inclusion Criteria") == ("5.1.2", "Subject Inclusion Criteria")
    assert parse_heading("Section 4 Study Design") == ("4", "Study Design")
    assert parse_heading("Overall Design") == ("", "Overall Design")


def test_out_of_order_numbered_heading_is_demoted():
    pages = [
        [
            "# 4 STUDY DESIGN",
            "Design text.",
            "# 5 STUDY POPULATION",
            "## 5.1 Inclusion Criteria",
            "1\\. Age 18 years or older.",
            "# 2. Prior therapy",  # a list item that was marked as a heading
            "Prior therapy text.",
            "## 5.2 Exclusion Criteria",
            "1\\. Pregnancy.",
        ]
    ]
    routing = route(document_of(pages))
    assert "2. Prior therapy" in routing.demoted_headings
    kinds = [(region.section.number, region.kind) for region in routing.eligibility]
    assert kinds == [("5.1", "inclusion"), ("5.2", "exclusion")]


def test_history_sections_are_marked_historical():
    pages = [
        ["# SUMMARY OF CHANGES", "Inclusion criterion 3 was changed.", "# 1 SYNOPSIS", "Current text."],
        ["# 5 STUDY POPULATION", "## 5.1 Inclusion Criteria", "1\\. Adults."],
    ]
    routing = route(document_of(pages))
    history = [s for s in routing.sections if s.title == "SUMMARY OF CHANGES"]
    assert history and history[0].historical
    assert all(not region.section.historical for region in routing.eligibility)


def test_synopsis_criteria_and_non_criteria_titles_are_not_regions():
    pages = [
        [
            "# 1 SYNOPSIS",
            "## Key Inclusion Criteria",
            "1\\. Adults.",
            "# 4 STUDY DESIGN",
            "## 4.3 Inclusion of Women and Minorities",
            "Both sexes are included.",
            "# 5 POPULATION",
            "## 5.1 Inclusion Criteria",
            "1\\. Adults 18 years or older.",
        ]
    ]
    routing = route(document_of(pages))
    assert [region.section.label() for region in routing.eligibility] == ["5.1 Inclusion Criteria"]
    assert routing.notes == ["synopsis_criteria_skipped:Key Inclusion Criteria"]


def test_routing_finds_design_and_glossary():
    routing = route(document_of(PAGES))
    assert [s.label() for s in routing.design] == ["1 SYNOPSIS", "2 STUDY DESIGN"]
    assert [s.label() for s in routing.glossary] == ["12.1 List of Abbreviations"]


# --- criteria ---------------------------------------------------------------------------


def test_inventory_numbered_criteria_with_population_labels():
    document = document_of(PAGES)
    inventory = build_inventory(document, route(document))
    ids = [c.criterion_id for c in inventory.criteria]
    assert ids == ["INC-1", "INC-2", "INC-3", "INC-4", "EXC-1", "EXC-2", "EXC-3"]
    scopes = {c.criterion_id: c.scope_label for c in inventory.criteria}
    assert scopes["INC-2"] == "Part 1" and scopes["INC-3"] == "Part 2" and scopes["INC-1"] is None
    first = inventory.by_id("INC-1")
    assert first.text == "Age 18 years or older."
    assert first.stem == "Participants must meet all of the following criteria:"
    assert inventory.issues == []
    assert inventory.scope_labels() == ["Part 1", "Part 2"]


def test_inventory_subheadings_margin_numbers_and_bullets():
    pages = [
        [
            "# 5 ELIGIBILITY",
            "## 5.2 Exclusion Criteria",
            "Medical Conditions",
            "1\\. Active infection requiring",
            "3",  # stray page artifact
            "systemic therapy.",
            "2\\. Any of the following:",
            "• Unstable angina",
            "• Heart failure",
            "Prior Therapy",
            "3",
            "prior treatment with an inhibitor of X.",
        ]
    ]
    document = document_of(pages)
    inventory = build_inventory(document, route(document))
    texts = {c.criterion_id: c.text for c in inventory.criteria}
    assert set(texts) == {"EXC-1", "EXC-2", "EXC-3"}
    assert "systemic therapy." in texts["EXC-1"] and "Unstable angina" not in texts["EXC-1"]
    assert len(inventory.by_id("EXC-2").item_line_ids) == 2
    assert texts["EXC-3"] == "prior treatment with an inhibitor of X."
    assert [t["text"] for t in inventory.group_titles] == ["Medical Conditions", "Prior Therapy"]


def test_inventory_splits_lettered_alternatives_by_population():
    pages = [
        [
            "# 5 POPULATION",
            "## 5.1 Inclusion Criteria",
            "1\\. Histologically confirmed disease as follows:",
            "a. Part 1: any advanced solid tumor.",
            "b. Part 2: melanoma.",
            "2\\. Adequate organ function.",
            "4\\. Signed consent.",
        ]
    ]
    document = document_of(pages)
    inventory = build_inventory(document, route(document))
    by_id = {c.criterion_id: c for c in inventory.criteria}
    assert by_id["INC-1.1"].scope_label == "Part 1" and "melanoma" not in by_id["INC-1.1"].text
    assert by_id["INC-1.2"].scope_label == "Part 2" and "melanoma" in by_id["INC-1.2"].text
    assert "criterion_numbering_gap:inclusion:3" in inventory.issues


def test_group_labels_inside_an_open_list_stay_in_the_criterion():
    pages = [
        [
            "# 5 POPULATION",
            "## 5.1 Inclusion Criteria",
            "1\\. Age 18 years or older.",
            "2\\. A tumor with one of the following:",
            "Non-Small Cell Lung Cancer",
            "- EGFR exon 20 insertion",
            "- HER2 mutation",
            "Colorectal Cancer",
            "- KRAS G12C mutation",
            "3\\. ECOG performance status of 0 or 1.",
        ]
    ]
    document = document_of(pages)
    inventory = build_inventory(document, route(document))
    assert [c.criterion_id for c in inventory.criteria] == ["INC-1", "INC-2", "INC-3"]
    second = inventory.by_id("INC-2").text
    assert "Non-Small Cell Lung Cancer" in second and "KRAS G12C" in second
    assert inventory.issues == ["no_exclusion_criteria_found"]


def test_population_scope_ends_at_a_general_heading():
    pages = [
        [
            "# 5 POPULATION",
            "## 5.1 Inclusion Criteria",
            "Cohort A",
            "1\\. Melanoma.",
            "Cohort B",
            "2\\. Lung cancer.",
            "All Participants",
            "3\\. Adequate organ function.",
        ]
    ]
    document = document_of(pages)
    scopes = {c.criterion_id: c.scope_label for c in build_inventory(document, route(document)).criteria}
    assert scopes == {"INC-1": "Cohort A", "INC-2": "Cohort B", "INC-3": None}


def test_criteria_printed_as_bullets_are_separate_items():
    pages = [
        [
            "# 5 POPULATION",
            "## 5.2 Exclusion Criteria",
            "• Pregnancy or breastfeeding.",
            "• Any of the following heart conditions:",
            "- Unstable angina",
            "- Heart failure",
            "• Active infection.",
        ]
    ]
    document = document_of(pages)
    inventory = build_inventory(document, route(document))
    assert [c.criterion_id for c in inventory.criteria] == ["EXC-1", "EXC-2", "EXC-3"]
    assert len(inventory.by_id("EXC-2").item_line_ids) == 2
    assert any(issue.startswith("bullet_list:") for issue in inventory.issues)


def test_unnumbered_first_item_is_kept_and_reported():
    pages = [["# 5 POPULATION", "## 5.1 Inclusion Criteria", "Signed informed consent", "2\\. Adults."]]
    document = document_of(pages)
    inventory = build_inventory(document, route(document))
    assert [c.criterion_id for c in inventory.criteria] == ["INC-1", "INC-2"]
    assert any(issue.startswith("unnumbered_item:INC-1") for issue in inventory.issues)


CONTINUED = [
    "# 5 STUDY POPULATION",
    "## 5.1 Inclusion Criteria",
    "1\\. Age 18 years or older.",
    "2\\. Participants who can become pregnant use highly effective contraception (see",
]
AFTER = ["3\\. Body weight over 30 kg.", "## 5.2 Exclusion Criteria", "1\\. Known active CNS metastases."]


@pytest.mark.parametrize(
    "pages",
    [
        [[*CONTINUED, "Section 6.3.1)", *AFTER]],
        [[HEADER, *CONTINUED], [HEADER, "Section 6.3.1)", *AFTER]],
        [[*CONTINUED, "## Section 6.3.1)", *AFTER]],
        [[*CONTINUED[:-1], CONTINUED[-1] + "\nSection 6.3.1)", *AFTER]],
    ],
    ids=["plain_line", "next_page", "marked_as_heading", "same_paragraph"],
)
def test_a_line_that_finishes_a_sentence_stays_in_its_criterion(pages):
    """A short capitalised line followed by a numbered item looks like a group title. When it
    closes the bracket the item left open, it is the end of the item's sentence."""
    document = document_of(pages)
    inventory = build_inventory(document, route(document))
    texts = {c.criterion_id: c.text for c in inventory.criteria}
    assert texts["INC-2"].endswith("contraception (see\nSection 6.3.1)")
    assert texts["INC-3"] == "Body weight over 30 kg."
    assert inventory.group_titles == [] and inventory.issues == []


def test_a_line_inside_a_paragraph_is_never_a_group_title():
    """Document Intelligence keeps the printed line breaks of a paragraph. The last line of a
    criterion's paragraph belongs to the criterion, even when it reads like a title."""
    pages = [
        [
            "# 5 STUDY POPULATION",
            "## 5.1 Inclusion Criteria",
            "1\\. Adults.",
            "## 5.2 Exclusion Criteria",
            "Medical Conditions",
            "1\\. Congestive heart failure as defined by the New York\nHeart Association Class III",
            "2\\. Known active CNS metastases.",
        ]
    ]
    document = document_of(pages)
    wrapped = [line for line in document.lines if "York" in line.text or "Heart" in line.text]
    assert [line.starts_paragraph for line in wrapped] == [True, False]
    inventory = build_inventory(document, route(document))
    assert inventory.by_id("EXC-1").text.endswith("New York\nHeart Association Class III")
    assert [t["text"] for t in inventory.group_titles] == ["Medical Conditions"]


def test_a_line_after_a_dangling_word_stays_in_its_criterion():
    pages = [
        [
            "# 5 POPULATION",
            "## 5.1 Inclusion Criteria",
            "1\\. Adequate organ function as defined in",
            "Table 3",
            "2\\. Signed consent.",
            "## 5.2 Exclusion Criteria",
            "1\\. Active infection.",
        ]
    ]
    document = document_of(pages)
    inventory = build_inventory(document, route(document))
    assert inventory.by_id("INC-1").text == "Adequate organ function as defined in\nTable 3"
    assert inventory.group_titles == [] and inventory.issues == []


def test_unfinished_criterion_text_is_reported_and_blocks():
    pages = [
        [
            "# 5 POPULATION",
            "## 5.1 Inclusion Criteria",
            "1\\. Adequate organ function (see",
            "2\\. Signed consent.",
            "## 5.2 Exclusion Criteria",
            "1\\. Active infection.",
        ]
    ]
    document = document_of(pages)
    inventory = build_inventory(document, route(document))
    assert inventory.issues == ["criterion_text_unfinished:INC-1"]
    assert "inventory:criterion_text_unfinished:INC-1".startswith(BLOCKING_PREFIXES)


def test_normalize_scope():
    assert normalize_scope("PART 2") == "Part 2"
    assert normalize_scope("cohort A") == "Cohort A"


# --- glossary ---------------------------------------------------------------------------


def test_glossary_table_and_inline_definitions():
    pages = [
        [
            "# 1 SYNOPSIS",
            "Participants need an absolute neutrophil count (ANC) above the limit.",
            "Tumors with microsatellite instability-high (MSI-H) status.",
            "Abnormal SGOT (AST), SGPT (ALT).",
            "If a participant has confirmed radiographic progression (iCPD) treatment stops.",
        ],
        [
            "# 12 APPENDIX",
            "## 12.1 List of Abbreviations",
            TableBlock([["Abbreviation", "Definition"], ["ECOG", "Eastern Cooperative Oncology Group"]]),
        ],
    ]
    document = document_of(pages)
    glossary = build_glossary(document, route(document))
    assert glossary.lookup("ECOG")[0].definition == "Eastern Cooperative Oncology Group"
    assert glossary.lookup("ECOG")[0].tier == 1
    assert glossary.lookup("ANC")[0].definition == "absolute neutrophil count"
    assert glossary.lookup("MSI-H")[0].definition == "microsatellite instability-high"
    assert glossary.lookup("ALT") == []  # the long form would cross a clause boundary
    assert glossary.lookup("iCPD") == []  # a long form never starts with a function word
    assert [d.abbreviation for d in glossary.for_text("ANC and ECOG")] == ["ANC", "ECOG"]


def test_shifted_glossary_rows_are_not_trusted():
    rows = [
        ["Abbreviation", "Definition"],
        ["ALT", "alanine aminotransferase"],
        ["AE", "adverse event"],  # next to a half-empty row: not trusted
        ["CR", ""],
        ["CRC", "complete response colorectal cancer"],
        ["DLT dMMR", "dose-limiting toxicity deficient mismatch repair"],
        ["ECOG", "Eastern Cooperative Oncology Group"],
    ]
    assert trusted_rows(rows) == [
        ("ALT", "alanine aminotransferase"),
        ("ECOG", "Eastern Cooperative Oncology Group"),
    ]


def test_align():
    assert align("ANC", ["the", "absolute", "neutrophil", "count"]) == "absolute neutrophil count"
    assert align("XYZ", ["no", "match", "here"]) is None


# --- bound documents --------------------------------------------------------------------


def _contents_page(start: int) -> list[object]:
    return ["# TABLE OF CONTENTS"] + [
        f"{n} Section title number {n} ........ {start + n}" for n in range(1, 9)
    ]


def test_two_protocol_versions_in_one_pdf():
    body = [["Body text page."] for _ in range(24)]
    pages = [["Protocol Alpha Title Page", "Version 1"], _contents_page(3), *body]
    pages += [["Protocol Alpha Title Page", "Version 2"], _contents_page(30), *body]
    segments = find_segments(document_of(pages))
    assert [(s.first_page, s.last_page) for s in segments] == [(1, 26), (27, 52)]


def test_parse_pages():
    assert parse_pages("3-10", 20) == (3, 10)
    assert parse_pages("5", 20) == (5, 20)
    for bad in ("0-3", "4-40", "x"):
        try:
            parse_pages(bad, 20)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_parts_of_a_running_header_at_the_page_top_are_running_text():
    header = '<!-- PageHeader="Clinical Study Protocol - 3.0 Drugex and cetuximab - D0001" -->'
    pages = [
        [header, "# 5 POPULATION", "## 5.1 Inclusion Criteria", "1\\. Adults."],
        [header, "## 5.2 Exclusion Criteria", "1\\. History of another malignancy except for:"],
        [
            "Clinical Study Protocol - 3.0",
            "Drugex and cetuximab - D0001",
            "- Adequately treated skin cancer",
            "2\\. Known active CNS metastases.",
        ],
        [header, "# 6 TREATMENT", "Drugex is given daily."],
    ]
    document = document_of(pages)
    inventory = build_inventory(document, route(document))
    text = inventory.by_id("EXC-1").text
    assert "Clinical Study Protocol" not in text and "D0001" not in text
    assert "Adequately treated skin cancer" in text
