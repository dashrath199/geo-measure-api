"""Geospatial layer: validation, extraction, reading, CRS, measurements.

Modules are framework-agnostic service components (no FastAPI imports):

- ``environment``   — dependency/driver verification
- ``validation``    — upload validation (extension/content/archive)
- ``zip_extraction`` — secure shapefile ZIP extraction
- ``readers``       — shapefile → normalized feature records
- ``processors``    — ShapefileProcessor / KMLProcessor over the shared
  feature representation
"""
