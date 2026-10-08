"""Geospatial environment verification.

Confirms that the geospatial stack (GeoPandas, Shapely, PyProj, and an
OGR-based I/O engine), upload handling, KML support and vector drivers are
available. The service fails fast with an actionable message when a
required component is missing.

Engine note:
    Historically GeoPandas used Fiona as its default I/O engine. Fiona
    publishes no wheels for Python 3.14 and cannot be built without a
    system GDAL installation, so GeoPandas' official default engine since
    1.0 — :mod:`pyogrio` (which bundles GDAL binaries) — is the required
    engine here. Fiona is detected and reported when present, but its
    absence is a warning, not a failure.
"""

from __future__ import annotations

import importlib
import importlib.metadata as metadata
from dataclasses import dataclass, field

from app.exceptions import GeospatialDependencyError

# (distribution name, import name) pairs that MUST be importable.
REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("geopandas", "geopandas"),
    ("shapely", "shapely"),
    ("pyproj", "pyproj"),
    ("pyogrio", "pyogrio"),
    ("python-multipart", "multipart"),
)

# Detected and reported, but not required (see module docstring).
OPTIONAL_PACKAGES: tuple[tuple[str, str], ...] = (
    ("fiona", "fiona"),
)

# Drivers the service cannot operate without.
REQUIRED_DRIVERS: tuple[str, ...] = ("ESRI Shapefile",)
# At least one of these must exist for KML uploads to work.
KML_DRIVERS: tuple[str, ...] = ("KML", "LIBKML")


@dataclass(frozen=True)
class PackageStatus:
    """Import/availability result for a single distribution.

    Attributes:
        distribution: PyPI distribution name.
        import_name: Module name used for ``import``.
        installed: Whether the module imported successfully.
        version: Installed version string, if resolvable.
        error: Import error summary when ``installed`` is ``False``.
        required: Whether the package is part of the required set.
    """

    distribution: str
    import_name: str
    installed: bool
    version: str | None
    error: str | None
    required: bool


@dataclass(frozen=True)
class GeospatialEnvironmentReport:
    """Outcome of an environment verification run.

    Attributes:
        packages: Per-package import results.
        drivers: ODR driver name → capability map (``r``/``rw``).
        kml_supported: Whether a KML-capable driver is available.
        missing_required: Distribution names of failed required packages.
        problems: Human-readable descriptions of every failure.
        warnings: Non-fatal notices (e.g. optional package absent).
    """

    packages: tuple[PackageStatus, ...]
    drivers: dict[str, str]
    kml_supported: bool
    missing_required: tuple[str, ...]
    problems: tuple[str, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """Return ``True`` when every required component is usable."""
        return not self.problems

    def summary(self) -> str:
        """Return a multi-line, human-readable report.

        Returns:
            The formatted report covering packages, drivers and problems.
        """
        lines: list[str] = ["Geospatial environment verification", "=" * 42]
        for pkg in self.packages:
            tag = "required" if pkg.required else "optional"
            if pkg.installed:
                lines.append(
                    f"[OK]        {pkg.distribution} {pkg.version} ({tag})"
                )
            else:
                marker = "MISSING" if pkg.required else "absent"
                lines.append(
                    f"[{marker:^10}] {pkg.distribution} ({tag}): {pkg.error}"
                )
        lines.append("-" * 42)
        lines.append(f"OGR drivers available: {len(self.drivers)}")
        for driver in REQUIRED_DRIVERS:
            state = "yes" if driver in self.drivers else "NO"
            lines.append(f"  driver {driver}: {state}")
        kml = ", ".join(d for d in KML_DRIVERS if d in self.drivers)
        lines.append(f"  KML support: {'yes (' + kml + ')' if kml else 'NO'}")
        if self.warnings:
            lines.append("-" * 42)
            lines.extend(f"[WARN] {w}" for w in self.warnings)
        if self.problems:
            lines.append("-" * 42)
            lines.extend(f"[FAIL] {p}" for p in self.problems)
            lines.append(
                "Fix: pip install -r requirements.txt"
            )
        lines.append("=" * 42)
        lines.append(f"Result: {'OK' if self.ok else 'FAILED'}")
        return "\n".join(lines)


def _probe_package(distribution: str, import_name: str, required: bool) -> PackageStatus:
    """Import a package and resolve its version.

    Args:
        distribution: PyPI distribution name.
        import_name: Module to import.
        required: Whether the package belongs to the required set.

    Returns:
        The resulting :class:`PackageStatus`.
    """
    try:
        importlib.import_module(import_name)
    except Exception as exc:  # noqa: BLE001 - any import failure is a finding
        return PackageStatus(
            distribution=distribution,
            import_name=import_name,
            installed=False,
            version=None,
            error=f"{type(exc).__name__}: {exc}",
            required=required,
        )
    try:
        version = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        version = None
    return PackageStatus(
        distribution=distribution,
        import_name=import_name,
        installed=True,
        version=version,
        error=None,
        required=required,
    )


def check_geospatial_environment() -> GeospatialEnvironmentReport:
    """Probe imports, drivers and KML capability without raising.

    Returns:
        A full :class:`GeospatialEnvironmentReport`; inspect ``ok`` or
        ``problems`` to decide whether the environment is usable.
    """
    statuses = [
        _probe_package(dist, mod, required=True)
        for dist, mod in REQUIRED_PACKAGES
    ] + [
        _probe_package(dist, mod, required=False)
        for dist, mod in OPTIONAL_PACKAGES
    ]

    problems: list[str] = []
    warnings: list[str] = []
    drivers: dict[str, str] = {}

    # Required I/O engine provides the driver inventory.
    pyogrio_status = next(s for s in statuses if s.distribution == "pyogrio")
    if pyogrio_status.installed:
        try:
            from pyogrio import list_drivers

            drivers = dict(list_drivers())
        except Exception as exc:  # noqa: BLE001 - surfaced as a finding
            problems.append(f"pyogrio driver introspection failed: {exc}")
    else:
        problems.append(
            "pyogrio (GDAL I/O engine) unavailable — cannot inspect drivers"
        )

    # Optional Fiona: report its driver table when present.
    fiona_status = next(
        (s for s in statuses if s.distribution == "fiona"), None
    )
    if fiona_status and fiona_status.installed:
        try:
            import fiona

            drivers.setdefault("fiona_count", str(len(fiona.supported_drivers)))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"fiona present but unusable: {exc}")
    elif fiona_status:
        warnings.append(
            "fiona not installed (no Python 3.14 wheels; GDAL source build "
            "unavailable) — using pyogrio as the GeoPandas I/O engine"
        )

    for status in statuses:
        if status.required and not status.installed:
            problems.append(
                f"required package '{status.distribution}' failed to import: "
                f"{status.error}"
            )

    for driver in REQUIRED_DRIVERS:
        if driver not in drivers:
            problems.append(f"required OGR driver '{driver}' not available")

    if not any(d in drivers for d in KML_DRIVERS):
        problems.append(
            "no KML-capable OGR driver (KML/LIBKML) — KML uploads unsupported"
        )

    kml_supported = any(d in drivers for d in KML_DRIVERS)
    missing = tuple(
        s.distribution for s in statuses if s.required and not s.installed
    )

    return GeospatialEnvironmentReport(
        packages=tuple(statuses),
        drivers=drivers,
        kml_supported=kml_supported,
        missing_required=missing,
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


def verify_geospatial_environment() -> GeospatialEnvironmentReport:
    """Verify the environment and fail clearly when it is not usable.

    Returns:
        The report when every required component is present.

    Raises:
        GeospatialDependencyError: If any required package, driver or the
            KML capability is missing. The message contains the full
            report so operators can act on it directly.
    """
    report = check_geospatial_environment()
    if not report.ok:
        raise GeospatialDependencyError(report.summary())
    return report
