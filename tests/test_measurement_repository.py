"""Tests for persistence of per-feature measurements.

``FeatureMeasurementRepository`` is the write/read path behind
``GET /api/files/{id}/measurements/``: the processing pipeline
persists its per-feature results through ``replace_for_file`` and the
API serves them through ``list_for_file``. Covers the scalar split
(area / length / unmeasured), feature ordering, re-run replacement
(no duplicate rows) and the JSON properties round-trip.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.repository import FeatureMeasurementRepository
from app.schemas.geospatial import Properties
from app.schemas.measurement import (
    AreaMeasurement,
    FeatureMeasurement,
    LengthMeasurement,
    MeasurementStatus,
)

import app.db.models  # noqa: F401 - registers models on Base.metadata

# A stable owning-file id (the table carries no foreign key).
FILE_ID = "00000000-0000-0000-0000-0000000000ff"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provide an isolated SQLite engine per test."""
    test_engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(bind=test_engine)
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Provide a session bound to the isolated engine."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db_session = factory()
    yield db_session
    db_session.close()


@pytest.fixture
def repository(session: Session) -> FeatureMeasurementRepository:
    """Provide a repository bound to the test session."""
    return FeatureMeasurementRepository(session)


def _area(feature_id: int) -> FeatureMeasurement:
    """Build a measured areal result.

    Args:
        feature_id: Zero-based feature index.

    Returns:
        A measured ``FeatureMeasurement`` with an area value.
    """
    return FeatureMeasurement(
        feature_id=feature_id,
        geometry_type="Polygon",
        measurement=AreaMeasurement(area=15_234.5),
        status=MeasurementStatus.MEASURED,
        measurement_crs="EPSG:32632",
        properties=Properties({"name": "zone a"}),
    )


def _length(feature_id: int) -> FeatureMeasurement:
    """Build a measured linear result.

    Args:
        feature_id: Zero-based feature index.

    Returns:
        A measured ``FeatureMeasurement`` with a length value.
    """
    return FeatureMeasurement(
        feature_id=feature_id,
        geometry_type="LineString",
        measurement=LengthMeasurement(length=345.0),
        status=MeasurementStatus.MEASURED,
        measurement_crs="EPSG:32632",
        properties=Properties({"name": "route b"}),
    )


def _unmeasured(feature_id: int) -> FeatureMeasurement:
    """Build an unmeasured point result.

    Args:
        feature_id: Zero-based feature index.

    Returns:
        A ``FeatureMeasurement`` with no measurement value.
    """
    return FeatureMeasurement(
        feature_id=feature_id,
        geometry_type="Point",
        measurement=None,
        status=MeasurementStatus.NOT_REQUIRED,
        reason="Point geometries require no measurement",
        properties=Properties({}),
    )


def test_replace_persists_scalar_columns(
    repository: FeatureMeasurementRepository,
) -> None:
    """Area, length and unmeasured results land in the right columns."""
    repository.replace_for_file(
        FILE_ID, [_area(0), _length(1), _unmeasured(2)]
    )

    rows = repository.list_for_file(FILE_ID)
    assert [r.feature_index for r in rows] == [0, 1, 2]

    area, length, unmeasured = rows
    assert area.measurement_kind == "area"
    assert area.measurement_value == 15_234.5
    assert area.unit == "square_meters"
    assert area.measurement_crs == "EPSG:32632"
    assert area.status == "measured"
    assert area.category == "SUPPORTED"
    assert area.geometry_type == "Polygon"

    assert length.measurement_kind == "length"
    assert length.measurement_value == 345.0
    assert length.unit == "meters"
    assert length.category == "SUPPORTED"
    assert length.geometry_type == "LineString"

    assert unmeasured.measurement_kind is None
    assert unmeasured.measurement_value is None
    assert unmeasured.unit is None
    assert unmeasured.measurement_crs is None
    assert unmeasured.status == "not_required"
    assert unmeasured.category == "NO_MEASUREMENT"
    assert unmeasured.reason == "Point geometries require no measurement"


def test_list_orders_by_feature_index(
    repository: FeatureMeasurementRepository,
) -> None:
    """Rows come back in feature order regardless of insert order."""
    repository.replace_for_file(FILE_ID, [_length(2), _unmeasured(0), _area(1)])

    rows = repository.list_for_file(FILE_ID)

    assert [r.feature_index for r in rows] == [0, 1, 2]


def test_replace_for_file_replaces_previous_run(
    repository: FeatureMeasurementRepository,
) -> None:
    """Re-running for the same file drops stale rows (no duplicates)."""
    repository.replace_for_file(FILE_ID, [_area(0), _length(1)])
    repository.replace_for_file(FILE_ID, [_unmeasured(0)])

    rows = repository.list_for_file(FILE_ID)

    assert len(rows) == 1
    assert rows[0].status == "not_required"
    assert rows[0].feature_index == 0


def test_properties_round_trip_as_json(
    repository: FeatureMeasurementRepository,
) -> None:
    """Source attributes persist as a parseable JSON object."""
    repository.replace_for_file(FILE_ID, [_area(0)])

    (row,) = repository.list_for_file(FILE_ID)

    assert json.loads(row.properties_json) == {"name": "zone a"}


def test_list_for_file_unknown_id_is_empty(
    repository: FeatureMeasurementRepository,
) -> None:
    """An id with no stored rows returns an empty list, not an error."""
    repository.replace_for_file(FILE_ID, [_area(0)])

    assert repository.list_for_file("00000000-0000-0000-0000-000000000000") == []
