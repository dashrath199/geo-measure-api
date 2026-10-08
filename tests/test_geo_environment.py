"""Tests for the geospatial environment verification utility."""

from __future__ import annotations

import pytest

from app.exceptions import GeospatialDependencyError
from app.geo.environment import (
    KML_DRIVERS,
    REQUIRED_DRIVERS,
    REQUIRED_PACKAGES,
    check_geospatial_environment,
    verify_geospatial_environment,
)


def test_all_required_packages_importable() -> None:
    """Every required distribution imports and reports a version."""
    report = check_geospatial_environment()

    for status in report.packages:
        if status.required:
            assert status.installed, (
                f"{status.distribution} failed: {status.error}"
            )
            assert status.distribution in dict(REQUIRED_PACKAGES)


def test_report_is_ok() -> None:
    """The environment passes verification (no problems recorded)."""
    report = check_geospatial_environment()

    assert report.ok, report.problems
    assert report.problems == ()


def test_verify_does_not_raise_in_healthy_env() -> None:
    """``verify_geospatial_environment`` returns a report when healthy."""
    report = verify_geospatial_environment()

    assert report.ok


def test_shapefile_driver_available() -> None:
    """The ESRI Shapefile driver is present."""
    report = check_geospatial_environment()

    for driver in REQUIRED_DRIVERS:
        assert driver in report.drivers


def test_kml_driver_available() -> None:
    """At least one KML-capable driver is present."""
    report = check_geospatial_environment()

    assert report.kml_supported
    assert any(d in report.drivers for d in KML_DRIVERS)


def test_missing_dependency_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing required package raises an actionable domain error."""
    import app.geo.environment as env_module

    original = env_module._probe_package

    def fake_probe(distribution: str, import_name: str, required: bool):
        if distribution == "shapely":
            from dataclasses import replace  # local import for test only

            # Simulate shapely being absent.
            status = original(distribution, import_name, required)
            return type(status)(
                distribution=status.distribution,
                import_name=status.import_name,
                installed=False,
                version=None,
                error="ModuleNotFoundError: simulated",
                required=status.required,
            )
        return original(distribution, import_name, required)

    monkeypatch.setattr(env_module, "_probe_package", fake_probe)

    with pytest.raises(GeospatialDependencyError) as excinfo:
        env_module.verify_geospatial_environment()

    message = str(excinfo.value)
    assert "shapely" in message
    assert "pip install" in message
