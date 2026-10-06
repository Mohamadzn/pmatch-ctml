"""The clinical agent (v5): diagnosis, age and receptor status of a criterion's clinical slots."""

from __future__ import annotations

from app.services.ctml.agents.instructions import CLINICAL
from app.services.ctml.agents.specialist import Specialist
from app.services.ctml.contracts import ClinicalSubmission

CLINICAL_AGENT = Specialist(
    domain="clinical",
    role="clinical",
    instructions=CLINICAL,
    search_tool="search_diagnosis",
    submit_tool="submit_clinical",
    submission=ClinicalSubmission,
)
