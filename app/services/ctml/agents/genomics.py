"""The genomics agent (v5): gene and alteration of a criterion's genomic slots."""

from __future__ import annotations

from app.services.ctml.agents.instructions import GENOMICS
from app.services.ctml.agents.specialist import Specialist
from app.services.ctml.contracts import GenomicSubmission

GENOMICS_AGENT = Specialist(
    domain="genomic",
    role="genomics",
    instructions=GENOMICS,
    search_tool="search_gene",
    submit_tool="submit_genomics",
    submission=GenomicSubmission,
)
