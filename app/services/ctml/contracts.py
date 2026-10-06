"""What the agents submit: metadata, study design and one criterion's encoding, and in the v5
layout the eligibility logic structure, the clinical, genomic and prior-therapy slot values,
the coverage listing and the resolver findings.

These are internal contracts, not the CTML output schema. Enumerated values come from
the CTML schema (schema/loader.py). Registry-valued fields (diagnosis, drug, drug class,
gene) are plain strings here; the checks accept them only when a lookup made in the same
run returned them.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from app.services.ctml.schema.loader import enum_values, literal_of

VariantCategory = literal_of("CTMLGenomicLeaf", "variant_category")
CnvCall = literal_of("CTMLGenomicLeaf", "cnv_call")
TreatmentCategory = literal_of("CTMLPriorTreatmentLeaf", "treatment_category")
TransplantType = literal_of("CTMLPriorTreatmentLeaf", "transplant_type")
Her2Status = literal_of("CTMLClinicalLeaf", "her2_status")
ErStatus = literal_of("CTMLClinicalLeaf", "er_status")
PrStatus = literal_of("CTMLClinicalLeaf", "pr_status")

# Patient descriptions use the positive values only ("!Mutation" describes a rule, not a patient).
PositiveVariantCategory = Literal[
    tuple(v for v in enum_values("CTMLGenomicLeaf", "variant_category") if not v.startswith("!"))
]  # type: ignore[misc]
PositiveCnvCall = Literal[
    tuple(v for v in enum_values("CTMLGenomicLeaf", "cnv_call") if not v.startswith("!"))
]  # type: ignore[misc]
PositiveTransplantType = Literal[
    tuple(v for v in enum_values("CTMLPriorTreatmentLeaf", "transplant_type") if not v.startswith("!"))
]  # type: ignore[misc]

SENTINELS = ("_SOLID_", "_LIQUID_")

# Where residual text goes and whether it is omitted is decided per category (compiler.py).
CONTEXT_CATEGORIES = (
    "disease context",
    "prior-therapy context",
    "performance status",
    "life expectancy",
    "tissue/biomarker submission",
    "consent",
    "oral administration",
    "curative local treatment",
    "other malignancy",
    "CNS disease",
    "major comorbidity",
    "laboratory value",
    "contraception/pregnancy",
    "washout/timing",
    "concomitant medication",
    "investigator judgment",
    "body measurement",
    "optional sub-study",
    "other",
)
ContextCategory = Literal[CONTEXT_CATEGORIES]  # type: ignore[valid-type]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Criterion encoding -------------------------------------------------------------------


class Clinical(Strict):
    age_expression: str | None = Field(None, description='Age rule such as ">=18" or "<75".')
    oncotree_primary_diagnosis: str | None = Field(
        None,
        description="An OncoTree name returned by search_diagnosis, or _SOLID_ / _LIQUID_. "
        "Prefix ! to exclude.",
    )
    her2_status: Her2Status | None = None
    er_status: ErStatus | None = None
    pr_status: PrStatus | None = None


class Genomic(Strict):
    hugo_symbol: str = Field(description="An HGNC symbol returned by search_gene. Prefix ! to exclude.")
    variant_category: VariantCategory
    protein_change: str | None = Field(
        None, description='HGVS protein change such as "p.V600E" (Mutation only).'
    )
    wildcard_protein_change: str | None = Field(None, description='Codon such as "p.G12" (Mutation only).')
    cnv_call: CnvCall | None = Field(None, description="CNV only.")
    fusion_partner_hugo_symbol: str | None = Field(
        None, description="Structural Variation only; an HGNC symbol returned by search_gene."
    )


class PriorTreatment(Strict):
    # Field order is the CTML output order (treatment_category first).
    treatment_category: TreatmentCategory
    agent_class: str | None = Field(
        None, description="A drug class name returned by search_therapy (kind class). Prefix ! to exclude."
    )
    agent: str | None = Field(
        None, description="A drug name returned by search_therapy (kind drug). Prefix ! to exclude."
    )
    transplant_type: TransplantType | None = Field(None, description="Stem Cell Transplant only.")


class LeafNode(Strict):
    type: Literal["leaf"]
    clinical: Clinical | None = None
    genomic: Genomic | None = None
    prior_treatment: PriorTreatment | None = None
    source_concept: str = Field(description="The exact words of the criterion that this leaf encodes.")
    line_ids: list[str] = Field(min_length=1, description="IDs of the lines that state this condition.")
    lookup_ids: list[str] = Field(
        default_factory=list, description="lookup_id of every search that returned a value used here."
    )

    def kind(self) -> str | None:
        present = [name for name in ("clinical", "genomic", "prior_treatment") if getattr(self, name)]
        return present[0] if len(present) == 1 else None


class GroupNode(Strict):
    type: Literal["and", "or"]
    items: list[Annotated[Union[GroupNode, LeafNode], Field(discriminator="type")]] = Field(min_length=1)  # noqa: UP007


Tree = Annotated[Union[GroupNode, LeafNode], Field(discriminator="type")]  # noqa: UP007


class UnmappedItem(Strict):
    quote: str = Field(description="Exact words of a requirement that is not encoded.")
    reason: str
    line_ids: list[str] = Field(default_factory=list)


class Confirmation(Strict):
    code: str = Field(description="The code of the check that asked for confirmation.")
    path: str = Field(description="The path the check reported.")
    quote: str = Field(description="Exact source words that support the flagged pattern.")


class CriterionSubmission(Strict):
    representable: Literal["full", "partial", "none"] = Field(
        description="full: the tree states the whole criterion; partial: some requirements stay as text; "
        "none: no tree."
    )
    tree: Tree | None = None
    unmapped_items: list[UnmappedItem] = Field(
        default_factory=list, description="Requirements left unencoded (required for partial)."
    )
    context_category: ContextCategory
    review_notes: list[str] = Field(
        default_factory=list,
        description="Anything a reviewer must see, for example a rule for another population in this text.",
    )


# --- Example patients for test_tree ------------------------------------------------------


class PatientAlteration(Strict):
    hugo_symbol: str
    variant_category: PositiveVariantCategory
    protein_change: str | None = None
    cnv_call: PositiveCnvCall | None = None
    fusion_partner_hugo_symbol: str | None = None


class PatientTherapy(Strict):
    treatment_category: TreatmentCategory
    agent: str | None = None
    agent_classes: list[str] = Field(
        default_factory=list, description="Classes of the drug, from your lookups."
    )
    transplant_type: PositiveTransplantType | None = None


class ExamplePatient(Strict):
    name: str
    expect: Literal["eligible", "not_eligible"]
    age: float | None = None
    diagnosis: str | None = Field(None, description="An OncoTree name.")
    her2_status: Her2Status | None = None
    er_status: ErStatus | None = None
    pr_status: PrStatus | None = None
    alterations: list[PatientAlteration] | None = Field(
        None, description="None means unknown; [] means none."
    )
    prior_treatments: list[PatientTherapy] | None = Field(
        None, description="None means unknown; [] means none."
    )


# --- Study design -------------------------------------------------------------------------


class DoseLevel(Strict):
    level_code: str = Field(description="A drug name from drugs, or Placebo.")
    level_description: str = Field(
        description="Dose, unit, route and schedule, for example '5 mg/kg IV Q2W'. For dose escalation give "
        "the starting dose followed by '(starting dose)'."
    )
    line_ids: list[str] = Field(min_length=1)


class Arm(Strict):
    arm_code: str = Field(description="Arm title: the population label and the protocol's arm title.")
    arm_description: str = Field(description="What the arm is for, in the protocol's words.")
    scope_labels: list[str] = Field(
        default_factory=list,
        description="Eligibility scope labels (from the list given) whose criteria apply to this arm.",
    )
    dose_levels: list[DoseLevel] = Field(min_length=1)
    line_ids: list[str] = Field(min_length=1)


class Drug(Strict):
    name: str
    role: Literal["investigational", "comparator", "placebo", "background"]
    line_ids: list[str] = Field(min_length=1)


class DesignSubmission(Strict):
    design_type: Literal["single_arm", "randomized", "multi_part", "multi_cohort", "multi_arm"]
    design_quote: str = Field(description="Exact words of the protocol that state the design.")
    drugs: list[Drug] = Field(min_length=1)
    arms: list[Arm] = Field(min_length=1)
    review_notes: list[str] = Field(default_factory=list)


# --- Metadata -----------------------------------------------------------------------------


class SourcedText(Strict):
    value: str = Field(description="Exact words from the cited lines, without the field label.")
    line_ids: list[str] = Field(min_length=1)


class MetadataSubmission(Strict):
    long_title: SourcedText
    short_title: SourcedText | None = None
    protocol_no: SourcedText
    protocol_version_no: SourcedText | None = None
    protocol_version_date: SourcedText | None = None
    phase: SourcedText
    sponsor_name: SourcedText
    nct_purpose: SourcedText | None = None
    nct_id: SourcedText | None = None
    review_notes: list[str] = Field(default_factory=list)


# --- v5: eligibility logic structure ------------------------------------------------------
# The eligibility logic agent states what a criterion requires and how its parts combine.
# Each condition CTML can hold is a slot; the clinical, genomics and prior therapy agents fill
# the slots of their domain with registry values. Code joins them by slot ID.

SlotDomain = Literal["clinical", "genomic", "prior_treatment"]


class Slot(Strict):
    type: Literal["slot"]
    slot_id: str = Field(description='Unique within the criterion: "S1", "S2", ...')
    domain: SlotDomain = Field(
        description="clinical: diagnosis, age, HER2/ER/PR status. genomic: gene and variant. "
        "prior_treatment: a drug, a drug class, a treatment category or a transplant."
    )
    concept: str = Field(description="The exact words of the criterion that state this condition.")
    line_ids: list[str] = Field(min_length=1, description="IDs of the lines that state this condition.")
    negated: bool = Field(
        False, description="True when eligible patients must NOT have this condition (it is excluded)."
    )


class LogicGroup(Strict):
    type: Literal["and", "or"]
    items: list[Annotated[Union[LogicGroup, Slot], Field(discriminator="type")]] = Field(min_length=1)  # noqa: UP007


LogicTree = Annotated[Union[LogicGroup, Slot], Field(discriminator="type")]  # noqa: UP007


class LogicExample(Strict):
    name: str
    expect: Literal["eligible", "not_eligible"]
    has: list[str] = Field(
        default_factory=list,
        description="Slot IDs whose condition (as worded in the slot) this patient has. The patient "
        "has none of the other slots' conditions.",
    )


class EligibilitySubmission(Strict):
    representable: Literal["full", "partial", "none"] = Field(
        description="full: the slots state the whole criterion; partial: some requirements stay as text; "
        "none: nothing CTML can hold (no logic)."
    )
    logic: LogicTree | None = None
    unmapped_items: list[UnmappedItem] = Field(
        default_factory=list,
        description="Requirements CTML cannot hold, as exact words (required for partial).",
    )
    context_category: ContextCategory
    examples: list[LogicExample] = Field(
        default_factory=list,
        description="At least one eligible and one not_eligible patient, described by the slots they have.",
    )
    review_notes: list[str] = Field(default_factory=list)


# --- v5: slot values ------------------------------------------------------------------------
# The positive value of each slot. Negation comes from the structure, never from these agents.


class ClinicalValue(Strict):
    age_expression: str | None = Field(
        None, description='Age as worded in the slot, such as ">=18" or "<75".'
    )
    oncotree_primary_diagnosis: str | None = Field(
        None, description="An OncoTree name returned by search_diagnosis, or _SOLID_ / _LIQUID_."
    )
    her2_status: Her2Status | None = None
    er_status: ErStatus | None = None
    pr_status: PrStatus | None = None


class GenomicValue(Strict):
    hugo_symbol: str = Field(description="An HGNC symbol returned by search_gene.")
    variant_category: PositiveVariantCategory
    protein_change: str | None = Field(
        None, description='HGVS protein change such as "p.V600E" (Mutation only).'
    )
    wildcard_protein_change: str | None = Field(None, description='Codon such as "p.G12" (Mutation only).')
    cnv_call: PositiveCnvCall | None = Field(None, description="CNV only.")
    fusion_partner_hugo_symbol: str | None = Field(
        None, description="Structural Variation only; an HGNC symbol returned by search_gene."
    )


class PriorTreatmentValue(Strict):
    treatment_category: TreatmentCategory
    agent_class: str | None = Field(
        None, description="A drug class name returned by search_therapy (kind class, or a parent class)."
    )
    agent: str | None = Field(None, description="A single drug name returned by search_therapy (kind drug).")
    transplant_type: PositiveTransplantType | None = Field(None, description="Stem Cell Transplant only.")


class SlotFillBase(Strict):
    slot_id: str
    status: Literal["mapped", "cannot_map"]
    lookup_ids: list[str] = Field(
        default_factory=list, description="lookup_id of every search that returned a value used here."
    )
    reason: str = Field("", description="Why the slot cannot be mapped (cannot_map only).")


# A slot normally has one value. Several values are for words that no single registry entry
# covers (for example a cancer of several sites, or inhibitors of two targets): any of them
# satisfies the slot, and a negated slot excludes each of them.
VALUES_DESCRIPTION = (
    "The registry value that states the slot's words. Give several only when no single registry entry "
    "covers the words; any of them then satisfies the slot. Empty for cannot_map."
)


class ClinicalFill(SlotFillBase):
    clinical: list[ClinicalValue] = Field(default_factory=list, description=VALUES_DESCRIPTION)


class GenomicFill(SlotFillBase):
    genomic: list[GenomicValue] = Field(default_factory=list, description=VALUES_DESCRIPTION)


class PriorTreatmentFill(SlotFillBase):
    prior_treatment: list[PriorTreatmentValue] = Field(default_factory=list, description=VALUES_DESCRIPTION)


class ClinicalSubmission(Strict):
    fills: list[ClinicalFill] = Field(min_length=1)
    review_notes: list[str] = Field(default_factory=list)


class GenomicSubmission(Strict):
    fills: list[GenomicFill] = Field(min_length=1)
    review_notes: list[str] = Field(default_factory=list)


class PriorTreatmentSubmission(Strict):
    fills: list[PriorTreatmentFill] = Field(min_length=1)
    review_notes: list[str] = Field(default_factory=list)


# --- v5: coverage and resolver --------------------------------------------------------------


class CoverageItem(Strict):
    quote: str = Field(description="Exact words of the criterion.")
    kind: Literal["requirement", "exception", "example", "note"] = Field(
        description="requirement: something a patient must have or must not have; exception: an "
        "exception to a requirement or an exclusion; example: an example of a listed item; note: an "
        "explanation, a definition or a procedure that does not decide eligibility."
    )


class CoverageSubmission(Strict):
    items: list[CoverageItem] = Field(min_length=1)


class ResolverIssue(Strict):
    target: str = Field(description='A criterion ID such as "EXC-3", or "design" or "metadata".')
    action: Literal["repair", "review"] = Field(
        description="repair: the protocol words show the candidate is wrong and the agent that made it "
        "must fix it; review: a person must look at it."
    )
    problem: str = Field(description="One or two sentences: what is wrong and what the protocol says.")
    quotes: list[str] = Field(min_length=1, description="Exact protocol words that show the problem.")
    line_ids: list[str] = Field(min_length=1, description="IDs of the lines the quotes come from.")


class ResolverSubmission(Strict):
    issues: list[ResolverIssue] = Field(default_factory=list)
    summary: str = Field("", description="One sentence on the overall state of the candidate.")


GroupNode.model_rebuild()
CriterionSubmission.model_rebuild()
LogicGroup.model_rebuild()
EligibilitySubmission.model_rebuild()
