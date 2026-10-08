"""Contract for the API-wide error envelope.

Every error response — domain, validation, routing or unexpected —
uses this shape::

    {"error": {"code": "UNSUPPORTED_FILE_TYPE", "message": "..."}}

The model exists so the envelope is documented in the OpenAPI schema;
responses themselves are built in ``app.main``'s exception handlers.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """The inner error object.

    Attributes:
        code: Stable, machine-readable error code (e.g.
            ``UNSUPPORTED_FILE_TYPE``) — branch on this, not on text.
        message: Human-readable, client-safe explanation: single-line,
            server paths redacted, never a stack trace.
    """

    code: str = Field(
        description="Machine-readable error code, e.g. UNSUPPORTED_FILE_TYPE."
    )
    message: str = Field(
        description="Client-safe human-readable explanation."
    )


class ErrorResponse(BaseModel):
    """Standard error response body.

    Attributes:
        error: The error detail object.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "code": "UNSUPPORTED_FILE_TYPE",
                    "message": (
                        "Unsupported file format (.txt); "
                        "supported: .kml, .zip"
                    ),
                }
            }
        }
    )

    error: ErrorDetail
