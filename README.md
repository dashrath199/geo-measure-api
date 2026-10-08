# Geo Measure API

A production-oriented FastAPI service that accepts geospatial file
uploads (Shapefile-in-ZIP and KML), extracts their features, resolves
CRS safely and computes area/length measurements in projected metres —
never in geographic degrees.

## Features

- `POST /api/files/` — validated, size-limited, traversal-safe upload
- Format processors (Shapefile, KML) feeding **one** Pydantic domain
  model (`GeospatialFile` / `Feature`)
- CRS service: explicit missing-CRS refusal (never assumes EPSG:4326),
  UTM zone strategy with equal-area/aequidistant fallbacks
- Measurement engine: planar area/length with structured outcomes and
  per-feature isolation (one bad feature never fails a file)
- SQLite persistence (SQLAlchemy 2.0), layered architecture, extensive
  test suite

## Quickstart

```bash
pip install -r requirements-dev.txt
python scripts/verify_geo_env.py     # geospatial environment check
uvicorn app.main:app --reload        # http://127.0.0.1:8000
python -m pytest                     # run the test suite
```

Configuration is environment-driven:
`DATABASE_URL`, `MAX_UPLOAD_MB`, `MAX_FEATURES`, `STORAGE_DIR`,
`LOG_LEVEL`, …

### Docker

The repository ships a reproducible container image with the working
geospatial stack preinstalled (GeoPandas, Shapely, PyProj, **Pyogrio**
with bundled GDAL — Python 3.14 slim, no system GDAL, no Fiona), runs
as a non-root user, and never has secrets baked in. Dependency
installation is pinned by `requirements.lock.txt` (constraints applied
on top of `requirements.txt` / `requirements-dev.txt`).

Build:

```bash
docker build -t geo-measure-api .
```

Run:

```bash
docker run --rm -p 8000:8000 geo-measure-api
```

or, with Docker Compose (single `app` service, persistent storage,
read-only root filesystem):

```bash
docker compose up --build
```

| URL | Purpose |
|---|---|
| `http://localhost:8000/api/files/` | API base (`API_PREFIX`, default `/api`) |
| `http://localhost:8000/docs` | Swagger UI |
| `http://localhost:8000/openapi.json` | OpenAPI schema |
| `http://localhost:8000/health` | Health endpoint (also the container health check) |

Environment configuration — the image defaults below are non-secret;
override with `docker run -e VAR=value` or compose/shell variables (a
local `.env` is git-ignored and never committed):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:////data/geo_measure.db` | SQLAlchemy URL (SQLite by default) |
| `STORAGE_DIR` | `/data/uploads` | Upload storage directory (server-generated UUID names) |
| `MAX_UPLOAD_MB` | `10` | Maximum upload size in megabytes |
| `MAX_FEATURES` | `50000` | Maximum features accepted per dataset |
| `API_PREFIX` | `/api` | Prefix for versioned routes (`/health` is always unversioned) |

Also honoured: `APP_NAME`, `ENVIRONMENT`, `LOG_LEVEL` (see
`.env.example`).

Persistent storage behavior: `docker compose` mounts the named volume
`app-data` at `/data`, holding the SQLite database and uploaded files
across `docker compose down`/`up` (remove with `docker compose down
-v`). With plain `docker run`, add `-v geo-data:/data` for the same
persistence; without a volume, storage lives inside the container and
disappears with it. Only `/data` and `/tmp` are writable — the root
filesystem is read-only under compose, and uploads are stored outside
any web root (never served as static files).

Run the test suite inside the image (separate test stage — the
production image contains no test tooling):

```bash
docker build --target test -t geo-measure-api:test .
docker run --rm geo-measure-api:test
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness probe → `{"status": "ok"}` |
| `POST /api/files/` | Upload `.zip` (shapefile) or `.kml` → `{id, filename, status}` |
| `GET /api/files/{id}/` | File metadata (status, feature count, CRS, safe failure text) |
| `GET /api/files/{id}/measurements/` | Per-feature measurements |

## Error handling

Every error response — domain, validation, routing or unexpected —
uses one JSON envelope with a stable machine-readable `code` and a
client-safe `message` (single-line, server paths redacted, never a
stack trace):

```json
{
    "error": {
        "code": "UNSUPPORTED_FILE_TYPE",
        "message": "Unsupported file format (.txt); supported: .kml, .zip"
    }
}
```

Codes are declared on the custom exception hierarchy
(`app/exceptions.py`) and mapped to HTTP statuses in one place
(`app/main.py`). Unexpected exceptions are logged with full tracebacks
for operators; clients only ever see a generic `INTERNAL_ERROR`.

### A. HTTP-level errors

These are the failures that actually return an HTTP error response —
raised while the request is parsed and validated (upload validation
runs before anything is stored):

| Scenario | HTTP | `code` |
|---|---|---|
| Unsupported file type | 415 | `UNSUPPORTED_FILE_TYPE` |
| Malformed KML content (upload validation) | 400 | `MALFORMED_KML` |
| Malformed ZIP | 400 | `MALFORMED_ZIP` |
| Unsafe archive (traversal, zip bomb, links, …) | 400 | `UNSAFE_ARCHIVE` |
| KML with DTD/ENTITY declarations | 400 | `UNSAFE_CONTENT` |
| Archive without any `.shp` dataset | 400 | `INVALID_SHAPEFILE` |
| Missing Shapefile components (`.shx`, `.dbf`) | 400 | `MISSING_SHAPEFILE_COMPONENT` |
| File not found | 404 | `FILE_NOT_FOUND` |
| Oversized upload | 413 | `FILE_TOO_LARGE` |
| Malformed request (validation) | 422 | `VALIDATION_ERROR` |
| Unknown route / wrong method | 404 / 405 | `NOT_FOUND` / `METHOD_NOT_ALLOWED` |
| Storage failure | 500 | `STORAGE_FAILURE` |
| Unexpected exception (logged) | 500 | `INTERNAL_ERROR` |

### B. Processing failures (recorded, not returned as HTTP errors)

`POST /api/files/` answers **201** once the upload has been validated,
stored and registered; the pipeline then runs *within that same
request* and the record ends up `COMPLETED` or `FAILED`:

- The 201 body reports the acceptance stage (`PROCESSING`), or
  `FAILED` when the in-request pipeline already failed — clients
  never receive internal error details there.
- Pipeline-level failures are captured as a `FAILED` record with a
  redacted `error_message` instead of an HTTP error: an unreadable or
  corrupt dataset (`PROCESSING_FAILURE`, `INVALID_SHAPEFILE`,
  `MALFORMED_KML` at re-parse), an unusable geometry column
  (`INVALID_GEOMETRY`), a foreign geometry type at read time
  (`UNSUPPORTED_GEOMETRY`), or a dataset above `MAX_FEATURES`
  (`TOO_MANY_FEATURES`). Read the outcome from
  `GET /api/files/{id}/`.
- Per-feature problems never fail the file: missing CRS
  (`MISSING_CRS`), unparseable CRS (`INVALID_CRS`), empty geometry
  (`EMPTY_GEOMETRY`), invalid or unknown geometry are isolated as
  structured per-feature skip statuses with a `reason` in
  `GET /api/files/{id}/measurements/` (matrix above), and the file
  still reports `COMPLETED`.

The exception hierarchy still maps these domain codes to HTTP 400
should one ever surface as a request-level domain error — but the
pipeline itself captures them as described above.

## Security

Security review of the upload and processing pipeline (Phase 16).
Decisions by threat class:

| Concern | Decision |
|---|---|
| Arbitrary file upload | Extension whitelist (`.zip`/`.kml`) **and** content sniffing (magic bytes / `<kml` root) before anything is stored; stored bytes are never served or executed |
| Malicious ZIP archives | Validated *without* extraction at upload time; at processing only whitelisted shapefile-component members are extracted; encrypted members rejected (`UNSAFE_ARCHIVE`) |
| Zip Slip | Member names checked for `..` segments, absolute paths, drive letters and NUL bytes (`is_unsafe_member_path`, shared by validation and extraction); extraction additionally resolves every target and requires it to stay inside the destination root; duplicate member paths collide on exclusive-create (`xb`) and fail as `UNSAFE_ARCHIVE` instead of overwriting |
| Path traversal | Storage names are server-generated UUIDs + whitelisted suffix — client filenames never reach the filesystem. Client names are sanitized (basename only, control characters stripped, 255-char cap) for display and DB only |
| Oversized uploads | Three layers: an ASGI body gate refuses an oversized declared `Content-Length` **unread** and counts chunked/lying bodies, aborting at `max_upload_mb` + 64 KB multipart slack (413 `FILE_TOO_LARGE`); the service then enforces the exact per-file limit during its streaming read |
| Decompression bombs | `MAX_ZIP_MEMBERS` (5 000) and total declared uncompressed size ≤ `20 × MAX_UPLOAD_MB`, enforced at upload *and* extraction; Python's `zipfile` caps member reads at the declared size, so bytes written to disk are bounded by the totals that were checked (no lying-header bypass) |
| Unsafe filenames | Sanitized for display; never used to build paths; all response text is JSON-encoded |
| Arbitrary file execution | Nothing from an upload is ever executed. Extraction skips non-shapefile members and rejects executables, symlinks, nested archives and encrypted members |
| Sensitive filesystem paths | `redact_paths()` scrubs every error message on write *and* read (`<path>/file.ext`); `stored_path` is never serialized in any response; full paths appear only in server-side logs |
| Malformed geospatial files | Reader/processor boundaries wrap every failure as a domain error; per-feature isolation means one bad feature never fails a file; KML carrying DTD/ENTITY declarations is rejected (`UNSAFE_CONTENT` — XML-bomb/XXE class) at upload and **re-scanned in full** at processing (the 8 KB sniff window cannot be used to hide a DTD) |
| Denial of service | Body gate + `MAX_UPLOAD_MB` + zip member/size limits + `MAX_FEATURES` (default 50 000): over-dense datasets fail with `TOO_MANY_FEATURES` instead of grinding through unbounded processing or producing unbounded measurement responses. Residual (accepted): transient memory while parsing a crafted archive's central directory is bounded by the 10 MB upload cap; rate limiting belongs in front of the service |
| SQL injection | All queries use SQLAlchemy Core/ORM bound parameters. The only raw SQL is a startup migration interpolating hardcoded table/column-name dicts — never user input |
| Unsafe error messages | One `{"error": {"code", "message"}}` envelope (see *Error handling*): curated messages, path-redacted, capped at 500 characters; unexpected failures return a generic `INTERNAL_ERROR` and full tracebacks go to logs only — `debug` never alters this |
| Accidental exposure of uploads | No static-file mounts and no download endpoint: `uploads/` is invisible to HTTP; response models exclude `stored_path` and settings |
| Unsafe temp files | Processing extracts into `tempfile.TemporaryDirectory` (random name, mode `0700`, removed per run); multipart bodies spool via Starlette's temp files |

Configuration knobs: `MAX_UPLOAD_MB` (default 10), `MAX_FEATURES`
(default 50 000), `STORAGE_DIR` (default `uploads/`, outside any web
root).

No authentication is added: the assignment defines a single-tenant
measurement API and authentication was explicitly out of scope.

## Supported geometry matrix

All measurements are computed **after** CRS transformation into a
projected, metric CRS (see *CRS strategy*). Every case returns a
structured category — `SUPPORTED` · `UNSUPPORTED` · `INVALID` ·
`NO_MEASUREMENT` (reported as the response's `status` field) plus a
human-readable `reason` where relevant — no case raises.

| Geometry | Measurement | Unit | Category | Behavior |
|---|---|---|---|---|
| `Polygon` | area | `square_meters` | `SUPPORTED` | Planar area in projected CRS |
| `MultiPolygon` | total area | `square_meters` | `SUPPORTED` | Sum of parts (parts must be disjoint — overlap is invalid) |
| `LineString` | length | `meters` | `SUPPORTED` | Planar length |
| `LinearRing` | length | `meters` | `SUPPORTED` | Closed-ring length |
| `MultiLineString` | total length | `meters` | `SUPPORTED` | Sum of parts |
| `GeometryCollection` with areal parts | total area | `square_meters` | `SUPPORTED` | Parts inspected & flattened (nesting supported); linear parts disclosed in `reason` (they don't affect area) |
| `GeometryCollection` with only linear parts | total length | `meters` | `SUPPORTED` | Lengths of linear parts only — polygon perimeters excluded; point parts ignored |
| `GeometryCollection` with only points | — | — | `NO_MEASUREMENT` | Nothing meaningful to compute |
| `Point` / `MultiPoint` | — | — | `NO_MEASUREMENT` | Returns `{"measurement": null}` (no CRS work needed) |
| Empty geometry | — | — | `NO_MEASUREMENT` | `skipped_empty` |
| Missing geometry | — | — | `NO_MEASUREMENT` | `skipped_missing_geometry` |
| Invalid geometry (fails OGC validity) | — | — | `INVALID` | Skipped — a planar figure would mislead (e.g. bowtie) |
| Corrupt/unparseable input | — | — | `INVALID` | Isolated per feature (`error`), file still processed |
| Unknown/foreign geometry type | — | — | `UNSUPPORTED` | Skipped — application never crashes |
| No CRS metadata | — | — | `NO_MEASUREMENT` | **Never** assumed to be EPSG:4326 |
| Unparseable CRS | — | — | `NO_MEASUREMENT` | Domain error surfaced as a skip |

Example results — the public response shape of
`GET /api/files/{id}/measurements/` (one entry per feature; there is
no `category` or `measurement_crs` field):

```json
{
  "file_id": "3f0c9f1e-8a5d-4f3a-9c2b-1d4e6a8b0c7d",
  "crs": "EPSG:4326",
  "features": [
    {
      "feature_id": 0,
      "geometry_type": "Polygon",
      "measurement": {"type": "area", "value": 12345.67, "unit": "square_meters"},
      "status": "SUPPORTED",
      "reason": null,
      "properties": {"name": "zone a"}
    },
    {
      "feature_id": 1,
      "geometry_type": "LineString",
      "measurement": {"type": "length", "value": 4567.89, "unit": "meters"},
      "status": "SUPPORTED",
      "reason": null,
      "properties": {"name": "centerline"}
    },
    {
      "feature_id": 2,
      "geometry_type": "Point",
      "measurement": null,
      "status": "NO_MEASUREMENT",
      "reason": "Point geometries require no measurement",
      "properties": {}
    }
  ]
}
```

## CRS strategy (why projection matters)

Latitude/longitude degrees are not a measurement system: 1° of
longitude shrinks with `cos(latitude)` toward the poles, and square
degrees are not square kilometres. Shapely's planar algorithms would
silently return physically meaningless numbers.

- Regional extents (≤ 8° × 12°, 80°S–84°N) → **UTM zone of the bbox
  centre**: `EPSG:326xx` (north) / `EPSG:327xx` (south).
- Large / polar / antimeridian-spanning extents → `EPSG:6933`
  (global equal-area) for area, local `+proj=aeqd` for length.
- Already-projected **metric** CRS → used as-is (identity).
- Projected non-metric (e.g. US feet) → converted to a metric CRS.
- **Missing CRS** → explicit `MissingCRSError`; measurement skipped.

Full assumptions and limitations: `app/geo/crs.py` module docstring.

## Project structure

```
app/
├── main.py               # app factory, error→HTTP mapping, routers
├── config.py             # env-driven settings
├── logging_config.py
├── exceptions.py         # domain error hierarchy
├── api/                  # routes + dependencies (HTTP only)
├── schemas/              # Pydantic contracts (files, geo domain, measurements, error envelope)
├── services/             # upload orchestration
├── geo/                  # validation, safe ZIP extraction, readers,
│                         # processors, CRS service (no FastAPI imports)
├── measurements/         # measurement engine (CRS via app.geo.crs)
├── storage/              # UUID-named local storage
├── db/                   # models, sessions, repository
scripts/                  # verify_geo_env.py, init_db.py
tests/                    # unit + integration suites
```

## Testing

```bash
python -m pytest
```

The suite covers: upload validation (incl. malicious archives), secure
extraction, readers per geometry type, KML processing, domain-model
serialization, CRS selection/transformation, measurement math verified
against closed-form values, and multi-geometry/category matrices.
