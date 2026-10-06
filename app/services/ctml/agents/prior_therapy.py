"""The prior therapy agent (v5): drug, drug class, category or transplant of a criterion's
prior_treatment slots."""

from __future__ import annotations

from app.services.ctml.agents.instructions import PRIOR_THERAPY
from app.services.ctml.agents.specialist import Specialist
from app.services.ctml.contracts import PriorTreatmentSubmission

PRIOR_THERAPY_AGENT = Specialist(
    domain="prior_treatment",
    role="prior_therapy",
    instructions=PRIOR_THERAPY,
    search_tool="search_therapy",
    submit_tool="submit_prior_therapy",
    submission=PriorTreatmentSubmission,
)
