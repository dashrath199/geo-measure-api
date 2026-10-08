"""Tests for the common geospatial domain models.

Verifies the five required models, the exact feature contract
(``feature_id``, ``geometry_type``, ``geometry``, ``source_crs``,
``properties``), CRS classification, and that no internal GeoPandas or
Shapely objects can leak into the API-facing representation.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import shapely
from shapely.geometry import Point

from app.geo.crs import crs_information
from app.geo.processors import KMLProcessor, ShapefileProcessor, dataset_to_geospatial_file
from app.geo.readers import features_from_gdf, read_shapefile
from app.schemas.geospatial import (
    CRSInformation,
    Feature,
    GeospatialFile,
    GeometryInformation,
    Properties,
)


# ------------------------------------------------------------- contracts


def test_feature_fields_match_specification() -> None:
    """``Feature`` exposes exactly the common representation fields."""
    assert set(Feature.model_fields) == {
        "feature_id",
        "geometry_type",
        "geometry",
        "source_crs",
        "properties",
    }


def test_required_models_exist_and_compose() -> None:
    """All five required models exist and nest as documented."""
    crs = CRSInformation(authority_code="EPSG:4326", is_geographic=True)
    geometry = GeometryInformation(
        geometry_type="Point", wkt="POINT (1 2)", bbox=(1.0, 2.0, 1.0, 2.0)
    )
    feature = Feature(
        feature_id=0,
        geometry_type="Point",
        geometry=geometry,
        source_crs=crs,
        properties=Properties({"name": "x"}),
    )
    file_model = GeospatialFile(
        filename="a.kml", file_type="kml", source_crs=crs, features=[feature]
    )

    assert file_model.feature_count == 1
    assert file_model.features[0].geometry.geometry_type == "Point"
    assert file_model.features[0].properties["name"] == "x"


def test_feature_count_stays_in_sync() -> None:
    """``feature_count`` derives from the feature list automatically."""
    crs = CRSInformation()
    file_model = GeospatialFile(
        filename="x.kml", file_type="kml", source_crs=crs, features=[]
    )
    assert file_model.feature_count == 0


def test_properties_behaves_like_a_mapping() -> None:
    """``Properties`` supports mapping access and dumps as a plain dict."""
    props = Properties({"name": "alpha", "value": 1.5})

    assert props["name"] == "alpha"
    assert "value" in props
    assert "absent" not in props
    assert props.get("absent") is None
    assert props.get("value") == 1.5
    # RootModel serializes as the raw object, not a wrapper.
    assert json.loads(props.model_dump_json()) == {"name": "alpha", "value": 1.5}


def test_quality_summary_properties() -> None:
    """Invalid/empty/missing geometry ids are derived per file."""
    crs = CRSInformation()
    valid = Feature(
        feature_id=0,
        geometry_type="Point",
        geometry=GeometryInformation(
            geometry_type="Point", wkt="POINT (0 0)"
        ),
        source_crs=crs,
    )
    broken = Feature(
        feature_id=1,
        geometry_type="Polygon",
        geometry=GeometryInformation(
            geometry_type="Polygon",
            wkt="POLYGON ((0 0, 1 1, 1 0, 0 2, 0 0))",
            is_valid=False,
        ),
        source_crs=crs,
    )
    empty = Feature(
        feature_id=2,
        geometry_type="Polygon",
        geometry=GeometryInformation(
            geometry_type="Polygon",
            wkt="POLYGON EMPTY",
            is_empty=True,
            bbox=None,
        ),
        source_crs=crs,
    )
    absent = Feature(feature_id=3, geometry=None, source_crs=crs)
    file_model = GeospatialFile(
        filename="q.kml",
        file_type="kml",
        source_crs=crs,
        features=[valid, broken, empty, absent],
    )

    assert file_model.invalid_geometry_ids == [1]
    assert file_model.empty_geometry_ids == [2]
    assert file_model.missing_geometry_ids == [3]


# ----------------------------------------------------------- CRS service


def test_crs_information_classifies_geographic() -> None:
    """EPSG:4326 is reported geographic with its authority code."""
    info = crs_information("EPSG:4326")

    assert info.authority_code == "EPSG:4326"
    assert info.is_geographic is True
    assert info.is_projected is False
    assert info.is_defined


def test_crs_information_classifies_projected() -> None:
    """A UTM zone is reported projected, not geographic."""
    info = crs_information("EPSG:32633")

    assert info.authority_code == "EPSG:32633"
    assert info.is_projected is True
    assert info.is_geographic is False


def test_crs_information_unknown_and_verbatim_fallback() -> None:
    """Missing CRS stays fully unknown; odd strings pass through."""
    unknown = crs_information(None)
    assert not unknown.is_defined
    assert unknown.authority_code is None
    assert unknown.is_geographic is None
    assert unknown.is_projected is None

    odd = crs_information("+proj=longlat +datum=WGS84 +no_defs")
    assert odd.authority_code  # resolved to some string form
    assert odd.is_geographic is True


# ------------------------------------------------- no internal objects


def test_domain_file_contains_no_geopandas_or_shapely_objects(
    tmp_path: Path,
) -> None:
    """Converted output holds only Pydantic models and plain values."""
    gdf = gpd.GeoDataFrame(
        {"name": ["s"]}, geometry=[Point(1, 2)], crs="EPSG:4326"
    )
    shp_path = tmp_path / "leak.shp"
    gdf.to_file(shp_path)

    converted = dataset_to_geospatial_file(
        read_shapefile(shp_path),
        filename="leak.shp",
        file_type="shapefile_zip",
    )
    for result in (ShapefileProcessor().process(shp_path), converted):
        for feature in result.features:
            assert isinstance(feature, Feature)
            assert isinstance(feature.source_crs, CRSInformation)
            assert isinstance(feature.properties, Properties)
            if feature.geometry is not None:
                assert isinstance(feature.geometry, GeometryInformation)
                assert isinstance(feature.geometry.wkt, str)
                # The deadly sin: a live Shapely object in the model.
                assert not isinstance(
                    feature.geometry, shapely.geometry.base.BaseGeometry
                )


def test_full_file_is_json_serializable(tmp_path: Path) -> None:
    """``model_dump_json`` round-trips a processed shapefile file."""
    gdf = gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[Point(0, 0), Point(1, 1)],
        crs="EPSG:3857",
    )
    shp_path = tmp_path / "json.shp"
    gdf.to_file(shp_path)

    result = ShapefileProcessor().process(shp_path)
    payload = json.loads(result.model_dump_json())

    assert payload["feature_count"] == 2
    assert payload["source_crs"]["authority_code"] == "EPSG:3857"
    assert payload["features"][0]["geometry"]["wkt"].startswith("POINT")
    assert payload["features"][0]["properties"] == {"name": "a"}


# ------------------------------------------- degraded geometry cases


def test_missing_geometry_converts_to_none_fields() -> None:
    """Source features without geometry map to null domain fields."""
    gdf = gpd.GeoDataFrame(
        {"name": ["ghost"]}, geometry=[None], crs="EPSG:4326"
    )

    result = dataset_to_geospatial_file(
        features_from_gdf(gdf),
        filename="ghost.kml",
        file_type="kml",
    )

    feature = result.features[0]
    assert feature.geometry is None
    assert feature.geometry_type is None
    assert result.missing_geometry_ids == [0]
    result.model_dump_json()


def test_empty_geometry_converts_to_flag_without_bbox() -> None:
    """Empty geometries keep their type, flag as empty and omit bbox."""
    from shapely.geometry import Polygon

    gdf = gpd.GeoDataFrame(
        {"name": ["none"]}, geometry=[Polygon()], crs="EPSG:4326"
    )

    result = dataset_to_geospatial_file(
        features_from_gdf(gdf),
        filename="empty.kml",
        file_type="kml",
    )

    geometry = result.features[0].geometry
    assert geometry is not None
    assert geometry.is_empty
    assert geometry.bbox is None
    assert result.empty_geometry_ids == [0]


def test_invalid_geometry_flag_preserved() -> None:
    """Invalid geometries carry ``is_valid=False`` into the domain."""
    bowtie = shapely.geometry.Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
    gdf = gpd.GeoDataFrame(
        {"name": ["bad"]}, geometry=[bowtie], crs="EPSG:4326"
    )

    result = dataset_to_geospatial_file(
        features_from_gdf(gdf),
        filename="bad.shp",
        file_type="shapefile_zip",
    )

    geometry = result.features[0].geometry
    assert geometry is not None
    assert geometry.is_valid is False
    assert result.invalid_geometry_ids == [0]


def test_kml_processor_emits_domain_model(tmp_path: Path) -> None:
    """KML processing output is the shared Pydantic representation."""
    path = tmp_path / "dom.kml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<Placemark><name>k</name>"
        "<Point><coordinates>1,2,0</coordinates></Point>"
        "</Placemark></Document></kml>",
        encoding="utf-8",
    )

    result = KMLProcessor().process(path)

    assert isinstance(result, GeospatialFile)
    assert result.file_type == "kml"
    assert result.source_crs.authority_code == "EPSG:4326"
    assert set(result.model_dump()) >= {
        "filename",
        "file_type",
        "source_crs",
        "features",
        "feature_count",
    }
