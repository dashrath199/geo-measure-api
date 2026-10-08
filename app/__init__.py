"""Geospatial measurement API package.

The application is organised in layers:

- ``app.api``       — FastAPI routes (HTTP concerns only)
- ``app.schemas``   — Pydantic request/response contracts
- ``app.config``    — settings loaded from environment variables
- ``app.logging_config`` — logging configuration
- ``app.main``      — application factory and startup wiring
"""

__version__ = "0.1.0"
