"""Schemas for service health/liveness endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Response body returned by ``GET /health``.

    Attributes:
        status: Service liveness indicator; ``"ok"`` when the app is running.
    """

    status: Literal["ok"] = Field(description="Service status.")
