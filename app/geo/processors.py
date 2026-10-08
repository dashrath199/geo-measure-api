"""Format processors producing one shared domain representation.

``ShapefileProcessor`` and ``KMLProcessor`` both normalize their format
into :class:`~app.schemas.geospatial.GeospatialFile` — the single
Pydantic domain model the rest of the pipeline consumes::

    Uploaded File → File Type Detection (``get_processor``)
        → Format Processor (this module)
        → Normalized Features (:class:`GeospatialFile` / :class:`Feature`)
        → CRS Service → Measurement Service → API Response

Because every processor emits the same serializable structure, CRS
handling, measurements and response shaping are each written **once**
and never duplicated per format. No GeoPandas/Shapely object leaves the
processors: geometry is rendered as WKT + bounding box.

Framework-agnostic: no FastAPI imports.

CRS policy:
- Shapefile: the *actual* source CRS is preserved; ``None`` → unknown
  (never assumed).
- KML: the OGC KML specification defines coordinates as WGS 84
  longitude/latitude, so EPSG:4326 is applied **explicitly** whenever
  the reader does not report a CRS of its own.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import geopandas as gpd
import pyogrio

from app.exceptions import GeometryProcessingError
from app.geo.crs import crs_information
from app.geo.readers import FeatureDataset, features_from_gdf, read_shapefile
from app.geo.validation import (
    SNIFF_BYTES,
    reject_xml_entities,
    validate_file_content,
)
from app.schemas.geospatial import (
    Feature,
    GeospatialFile,
    GeometryInformation,
    Properties,
)

logger = logging.getLogger(__name__)

# OGC KML spec: coordinates are always WGS 84 lon/lat (geographic).
KML_CRS = "EPSG:4326"


def dataset_to_geospatial_file(
    dataset: FeatureDataset,
    *,
    filename: str,
    file_type: str,
) -> GeospatialFile:
    """Convert a reader's internal dataset into the domain model.

    Geometries are rendered to WKT/bbox so the result contains no
    Shapely objects and serializes directly for the API layer.

    Args:
        dataset: Normalized features produced by a reader.
        filename: Name of the source file.
        file_type: Format identifier (``shapefile_zip`` / ``kml``).

    Returns:
        The common :class:`GeospatialFile` representation.
    """
    source_crs = crs_information(dataset.crs)
    features: list[Feature] = []

    for internal in dataset.features:
        if internal.geometry is None:
            geometry_info = None
            geometry_type = None
        else:
            # Empty geometries report (nan, nan, nan, nan) bounds — a
            # null bbox keeps the JSON representation valid.
            bounds = internal.geometry.bounds
            bbox = (
                tuple(bounds)
                if bounds and not any(math.isnan(value) for value in bounds)
                else None
            )
            geometry_type = internal.geometry_type
            geometry_info = GeometryInformation(
                geometry_type=geometry_type,
                wkt=internal.geometry.wkt,
                bbox=bbox,
                is_valid=internal.is_valid,
                is_empty=internal.is_empty,
            )
        features.append(
            Feature(
                feature_id=internal.feature_id,
                geometry_type=geometry_type,
                geometry=geometry_info,
                source_crs=source_crs,
                properties=Properties(internal.properties),
            )
        )

    return GeospatialFile(
        filename=filename,
        file_type=file_type,
        source_crs=source_crs,
        features=features,
    )


class ShapefileProcessor:
    """Processes an extracted shapefile into the shared domain model."""

    source_type = "shapefile_zip"

    def process(self, shp_path: str | Path) -> GeospatialFile:
        """Read a shapefile and normalize it into ``GeospatialFile``.

        Args:
            shp_path: Path to the extracted ``.shp`` file.

        Returns:
            The shared :class:`GeospatialFile`.

        Raises:
            GeometryProcessingError: If the shapefile cannot be read.
        """
        path = Path(shp_path)
        dataset = read_shapefile(path)
        return dataset_to_geospatial_file(
            dataset,
            filename=path.name,
            file_type=self.source_type,
        )


class KMLProcessor:
    """Processes a KML file into the shared domain model.

    Handles the empty-document quirk defensively: GDAL exposes a KML
    with no placemarks as *zero layers*, which the default reader cannot
    open — the layer list is inspected first and an empty (but valid)
    dataset is returned instead of failing.
    """

    source_type = "kml"

    def process(self, kml_path: str | Path) -> GeospatialFile:
        """Read a KML file, applying EPSG:4326 explicitly.

        Args:
            kml_path: Path to the ``.kml`` file.

        Returns:
            The shared :class:`GeospatialFile`; zero features for
            valid-but-empty KML documents.

        Raises:
            InvalidFileError: If the bytes are not KML at all.
            GeometryProcessingError: If the file is missing, unreadable,
                or cannot be parsed by the KML driver.
        """
        path = Path(kml_path)
        if path.suffix.lower() != ".kml":
            raise GeometryProcessingError(
                f"Expected a .kml file, got: {path.name!r}"
            )
        if not path.is_file():
            raise GeometryProcessingError(f"KML file not found: {path}")

        # Sniff the head for the root element, but scan the *whole*
        # document for DTD/entity declarations: stored bytes are
        # re-checked on this layer too (defense in depth).
        with path.open("rb") as handle:
            data = handle.read()
        validate_file_content("kml", data[:SNIFF_BYTES])
        reject_xml_entities(data)

        try:
            layers = pyogrio.list_layers(path)
        except Exception as exc:  # noqa: BLE001 - reader boundary wraps errors
            raise GeometryProcessingError(
                f"Could not read KML '{path.name}': {exc}",
                code="MALFORMED_KML",
            ) from exc

        if len(layers) == 0:
            # Valid KML with no placemarks: GDAL reports zero layers.
            logger.info(
                "KML %s contains no placemarks -> 0 features", path.name
            )
            return GeospatialFile(
                filename=path.name,
                file_type=self.source_type,
                source_crs=crs_information(KML_CRS),
                features=[],
            )

        try:
            gdf = gpd.read_file(path)
        except Exception as exc:  # noqa: BLE001 - reader boundary wraps errors
            raise GeometryProcessingError(
                f"Could not read KML '{path.name}': {exc}",
                code="MALFORMED_KML",
            ) from exc

        if gdf.crs is None:
            # Explicit per the KML specification — the only format where
            # a default CRS is justified, because the format defines it.
            gdf = gdf.set_crs(KML_CRS)
            logger.debug(
                "KML %s reported no CRS; applied %s per OGC KML spec",
                path.name,
                KML_CRS,
            )

        dataset = features_from_gdf(gdf)
        logger.info(
            "Read KML %s: %d feature(s), crs=%s",
            path.name,
            dataset.feature_count,
            dataset.crs,
        )
        return dataset_to_geospatial_file(
            dataset,
            filename=path.name,
            file_type=self.source_type,
        )


def get_processor(file_type: str) -> ShapefileProcessor | KMLProcessor:
    """Return the processor for a stored file's type.

    This is the pipeline's *file type detection* step.

    Args:
        file_type: ``"shapefile_zip"`` or ``"kml"`` (values produced by
            upload validation).

    Returns:
        A processor instance.

    Raises:
        GeometryProcessingError: For unknown file types.
    """
    if file_type == ShapefileProcessor.source_type:
        return ShapefileProcessor()
    if file_type == KMLProcessor.source_type:
        return KMLProcessor()
    raise GeometryProcessingError(
        f"No processor available for file type: {file_type!r}"
    )
