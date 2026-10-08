"""Unit tests for the shapefile reader service.

Covers every required case: Point, LineString, Polygon, MultiLineString,
MultiPolygon, missing CRS, invalid geometry, empty geometry and
attributes — plus CRS preservation (never assuming EPSG:4326).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.exceptions import GeometryProcessingError
from app.geo.readers import (
    features_from_gdf,
    read_shapefile,
)


def _write(tmp_path: Path, name: str, gdf: gpd.GeoDataFrame) -> Path:
    """Write a GeoDataFrame as a shapefile and return the ``.shp`` path.

    Args:
        tmp_path: Test temp directory.
        name: Base name for the dataset.
        gdf: Features to write.

    Returns:
        Path of the written ``.shp``.
    """
    path = tmp_path / f"{name}.shp"
    gdf.to_file(path)
    return path


# ---------------------------------------------------------- geometry types


def test_read_point(tmp_path: Path) -> None:
    """Point features are identified with their type and properties."""
    gdf = gpd.GeoDataFrame(
        {"name": ["p1"]}, geometry=[Point(1.0, 2.0)], crs="EPSG:4326"
    )

    dataset = read_shapefile(_write(tmp_path, "points", gdf))

    feature = dataset.features[0]
    assert dataset.feature_count == 1
    assert feature.geometry_type == "Point"
    assert feature.geometry is not None
    assert feature.geometry.x == pytest.approx(1.0)
    assert feature.properties == {"name": "p1"}


def test_read_linestring(tmp_path: Path) -> None:
    """LineString features are identified correctly."""
    gdf = gpd.GeoDataFrame(
        {"name": ["l1"]},
        geometry=[LineString([(0, 0), (1, 1)])],
        crs="EPSG:4326",
    )

    dataset = read_shapefile(_write(tmp_path, "lines", gdf))

    feature = dataset.features[0]
    assert feature.geometry_type == "LineString"
    assert not feature.is_empty
    assert feature.is_valid


def test_read_polygon(tmp_path: Path) -> None:
    """Polygon features are identified correctly."""
    gdf = gpd.GeoDataFrame(
        {"name": ["poly"]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326"
    )

    dataset = read_shapefile(_write(tmp_path, "polys", gdf))

    assert dataset.features[0].geometry_type == "Polygon"


def test_read_multilinestring(tmp_path: Path) -> None:
    """MultiLineString features keep their multi type."""
    gdf = gpd.GeoDataFrame(
        {"name": ["ml"]},
        geometry=[
            MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]])
        ],
        crs="EPSG:4326",
    )

    dataset = read_shapefile(_write(tmp_path, "multilines", gdf))

    assert dataset.features[0].geometry_type == "MultiLineString"


def test_read_multipolygon(tmp_path: Path) -> None:
    """MultiPolygon features keep their multi type."""
    gdf = gpd.GeoDataFrame(
        {"name": ["mp"]},
        geometry=[MultiPolygon([box(0, 0, 1, 1), box(2, 2, 3, 3)])],
        crs="EPSG:4326",
    )

    dataset = read_shapefile(_write(tmp_path, "multipolys", gdf))

    assert dataset.features[0].geometry_type == "MultiPolygon"


def test_feature_ids_are_sequential(tmp_path: Path) -> None:
    """Ids are the zero-based positional index of each feature."""
    gdf = gpd.GeoDataFrame(
        {"name": ["a", "b", "c"]},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
        crs="EPSG:4326",
    )

    dataset = read_shapefile(_write(tmp_path, "many", gdf))

    assert [f.feature_id for f in dataset.features] == [0, 1, 2]
    assert dataset.feature_count == 3


# ------------------------------------------------------------ CRS handling


def test_missing_crs_is_none_not_wgs84(tmp_path: Path) -> None:
    """A shapefile without .prj yields crs=None — never EPSG:4326."""
    gdf = gpd.GeoDataFrame(
        {"name": ["x"]}, geometry=[Point(0, 0)], crs=None
    )

    dataset = read_shapefile(_write(tmp_path, "nocrs", gdf))

    assert dataset.crs is None
    assert dataset.features[0].crs is None
    assert dataset.crs != "EPSG:4326"


def test_actual_crs_preserved(tmp_path: Path) -> None:
    """A projected source CRS is preserved verbatim (not converted)."""
    gdf = gpd.GeoDataFrame(
        {"name": ["utm"]},
        geometry=[Point(500000, 4000000)],
        crs="EPSG:32633",
    )

    dataset = read_shapefile(_write(tmp_path, "utm", gdf))

    assert dataset.crs == "EPSG:32633"
    assert dataset.features[0].crs == "EPSG:32633"


def test_wgs84_crs_reported_as_authority(tmp_path: Path) -> None:
    """Geographic CRS is reported in authority form when defined."""
    gdf = gpd.GeoDataFrame(
        {"name": ["wgs"]}, geometry=[Point(0, 0)], crs="EPSG:4326"
    )

    dataset = read_shapefile(_write(tmp_path, "wgs", gdf))

    assert dataset.crs == "EPSG:4326"


# ------------------------------------------------------- degenerate data


def test_invalid_geometry_detected(tmp_path: Path) -> None:
    """Self-intersecting polygons are flagged, not dropped or raised."""
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
    gdf = gpd.GeoDataFrame(
        {"name": ["bad"]}, geometry=[bowtie], crs="EPSG:4326"
    )

    dataset = read_shapefile(_write(tmp_path, "invalid", gdf))

    feature = dataset.features[0]
    assert feature.geometry is not None
    assert feature.geometry_type == "Polygon"
    assert not feature.is_valid
    assert dataset.invalid_geometry_ids == (0,)


def test_empty_geometry_handled_gracefully() -> None:
    """Empty geometries are read without error and flagged as empty."""
    gdf = gpd.GeoDataFrame(
        {"name": ["empty"]}, geometry=[Polygon()], crs="EPSG:4326"
    )

    dataset = features_from_gdf(gdf)

    feature = dataset.features[0]
    assert feature.geometry_type == "Polygon"
    assert feature.is_empty
    assert dataset.empty_geometry_ids == (0,)
    assert dataset.feature_count == 1


def test_missing_geometry_handled_gracefully() -> None:
    """Null geometries produce a feature with type None and no crash."""
    gdf = gpd.GeoDataFrame(
        {"name": ["ghost"]}, geometry=[None], crs="EPSG:4326"
    )

    dataset = features_from_gdf(gdf)

    feature = dataset.features[0]
    assert feature.geometry is None
    assert feature.geometry_type is None
    assert feature.properties == {"name": "ghost"}
    assert dataset.missing_geometry_ids == (0,)
    assert feature.as_dict()["geometry"] is None


def test_empty_dataset_yields_zero_features() -> None:
    """A dataset with no rows reads as an empty, valid result."""
    gdf = gpd.GeoDataFrame({"name": []}, geometry=[], crs="EPSG:4326")

    dataset = features_from_gdf(gdf)

    assert dataset.feature_count == 0
    assert dataset.features == ()


# ------------------------------------------------------------- attributes


def test_attributes_extracted_and_json_safe(tmp_path: Path) -> None:
    """Attribute columns come back as JSON-safe property values."""
    gdf = gpd.GeoDataFrame(
        {
            "name": ["alpha", "beta"],
            "value": [1.5, None],
            "count": [10, 20],
            "when": [datetime(2026, 1, 15), datetime(2026, 2, 20)],
        },
        geometry=[Point(0, 0), Point(1, 1)],
        crs="EPSG:4326",
    )

    dataset = read_shapefile(_write(tmp_path, "attrs", gdf))

    first, second = dataset.features
    assert first.properties["name"] == "alpha"
    assert first.properties["value"] == pytest.approx(1.5)
    assert first.properties["count"] == 10
    # DBF has no datetime type: GDAL serializes the date column to a
    # string (format is driver-defined). Pin the content, not the format.
    when = first.properties["when"]
    assert isinstance(when, str)
    assert "2026" in when and ("01-15" in when or "01/15" in when)
    # Null attribute becomes None, not NaN.
    assert second.properties["value"] is None
    # The whole property block must be JSON-serializable as-is.
    json.dumps(first.properties)
    json.dumps(second.properties)


def test_feature_as_dict_matches_contract() -> None:
    """The internal representation carries the documented keys."""
    gdf = gpd.GeoDataFrame(
        {"name": ["x"]}, geometry=[Point(5, 6)], crs="EPSG:4326"
    )

    feature = features_from_gdf(gdf).features[0]
    payload = feature.as_dict()

    assert set(payload) >= {
        "feature_id",
        "geometry_type",
        "geometry",
        "crs",
        "properties",
    }
    assert payload["feature_id"] == 0
    assert payload["geometry_type"] == "Point"
    assert "POINT" in payload["geometry"]
    assert payload["crs"] == "EPSG:4326"
    assert payload["properties"] == {"name": "x"}
    json.dumps(payload)  # whole representation is serializable


# ------------------------------------------------------------- error paths


def test_missing_file_raises(tmp_path: Path) -> None:
    """A nonexistent shapefile is a clear domain error."""
    with pytest.raises(GeometryProcessingError, match="not found"):
        read_shapefile(tmp_path / "nope.shp")


def test_wrong_extension_raises(tmp_path: Path) -> None:
    """Only ``.shp`` inputs are accepted."""
    other = tmp_path / "data.txt"
    other.write_text("x")

    with pytest.raises(GeometryProcessingError, match=r"\.shp"):
        read_shapefile(other)


def test_corrupt_shapefile_raises(tmp_path: Path) -> None:
    """Unreadable shapefile bytes surface as a domain error."""
    corrupt = tmp_path / "corrupt.shp"
    corrupt.write_bytes(b"not a shapefile at all")

    with pytest.raises(GeometryProcessingError, match="Could not read"):
        read_shapefile(corrupt)
