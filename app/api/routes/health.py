"""Health check route.

Exposed outside the versioned ``/api`` prefix so that load balancers and
orchestrators can probe a stable, unversioned URL.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    response_description="Service status.",
)
async def health_check() -> HealthResponse:
    """Report that the service is alive and able to serve requests.

    Returns:
        A body of ``{"status": "ok"}``.
    """
    return HealthResponse(status="ok")
