"""Reusable FastAPI dependencies (routing concerns only)."""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import get_db
from app.db.repository import FeatureMeasurementRepository
from app.services.file_service import FileService
from app.services.processing_service import ProcessingService


def get_app_settings(request: Request) -> Settings:
    """Provide the settings the application was built with.

    Reads from ``app.state`` so tests can build an app with injected
    settings instead of relying on the process-wide cache.

    Args:
        request: The current request.

    Returns:
        The settings instance stored by ``create_app``.
    """
    return request.app.state.settings


def get_file_service(
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> FileService:
    """Build a per-request :class:`FileService`.

    Args:
        request: The current request.
        session: Request-scoped database session.
        settings: Application settings.

    Returns:
        The service bound to this request.
    """
    return FileService(session=session, settings=settings)


def get_processing_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> ProcessingService:
    """Build a per-request :class:`ProcessingService`.

    Shares the request-scoped session with the file service (FastAPI
    caches the ``get_db`` dependency once per request).

    Args:
        session: Request-scoped database session.
        settings: Application settings.

    Returns:
        The processing pipeline service.
    """
    return ProcessingService(session=session, settings=settings)


def get_feature_measurement_repository(
    session: Session = Depends(get_db),
) -> FeatureMeasurementRepository:
    """Build a per-request measurement-row repository.

    Args:
        session: Request-scoped database session.

    Returns:
        The repository for reading persisted measurements.
    """
    return FeatureMeasurementRepository(session)
