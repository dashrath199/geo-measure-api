"""Docker packaging configuration (Phase 20).

Static consistency checks over the container files (``Dockerfile``,
``docker-compose.yml``, ``.dockerignore``). They guard the packaging
contract without needing a Docker daemon:

- pinned Python 3.14 base image;
- non-root runtime user;
- a container health check that probes the *real* ``/health`` route the
  application serves;
- no baked-in secrets in the image or compose file;
- development/test tooling kept out of the runtime stage;
- environment-driven compose configuration;
- build context excludes local databases, uploads, caches and secrets —
  but never the files the application and test stage need.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _dockerfile() -> str:
    """Return the Dockerfile source."""
    return DOCKERFILE.read_text(encoding="utf-8")


def _compose() -> str:
    """Return the compose file source."""
    return COMPOSE.read_text(encoding="utf-8")


def _dockerignore_patterns() -> list[str]:
    """Return active (non-comment) ``.dockerignore`` patterns.

    Returns:
        One stripped pattern per effective line, negations included.
    """
    patterns: list[str] = []
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


# --------------------------------------------------------------- base image


def test_base_image_is_pinned_python_314() -> None:
    """The image pins a specific Python 3.14 slim base tag (no floats)."""
    match = re.search(r"(?m)^ARG PYTHON_BASE=(\S+)$", _dockerfile())
    assert match is not None, "Dockerfile must pin PYTHON_BASE"
    assert re.fullmatch(r"python:3\.14\.\d+-slim-[a-z]+", match.group(1)), (
        f"unexpected base image tag: {match.group(1)}"
    )


def test_installs_existing_requirements_with_lock_constraints() -> None:
    """Dependency install uses the repo's requirements files + the lock.

    Guards deterministic installation: the runtime stage installs
    ``requirements.txt`` constrained by ``requirements.lock.txt``, and
    the test tooling (``requirements-dev.txt``) appears only before the
    runtime stage.
    """
    text = _dockerfile()
    deps_stage = text.split("AS test", 1)[0]
    runtime_stage = text.split("AS runtime", 1)[1]

    assert re.search(
        r"pip install -r requirements\.txt -c requirements\.lock\.txt",
        deps_stage,
    )
    assert re.search(
        r"pip install -r requirements-dev\.txt -c requirements\.lock\.txt",
        text.split("AS runtime", 1)[0],
    ), "test stage must install the dev requirements"
    # The production stage must not know about dev tooling or the suite.
    assert "requirements-dev" not in runtime_stage
    assert "pytest" not in runtime_stage
    assert re.search(r"(?m)^COPY tests ", runtime_stage) is None


# ------------------------------------------------------------------ runtime


def test_image_runs_as_non_root_user() -> None:
    """Every USER directive targets a non-root account; compose agrees."""
    users = re.findall(r"(?m)^USER\s+(\S+)$", _dockerfile())
    assert users, "Dockerfile must switch to a non-root user"
    for user in users:
        assert user not in {"root", "0"}, f"runtime user must not be {user}"
    # No compose-level override back to root.
    assert re.search(r"(?m)^\s+user:\s*['\"]?root\b", _compose()) is None


def test_healthcheck_probes_the_live_health_route(client) -> None:
    """The container health check calls the route the app actually serves.

    Extracts the URL path from the Dockerfile ``HEALTHCHECK`` and hits it
    through the application, so a renamed/removed route fails here rather
    than leaving containers permanently ``unhealthy``.
    """
    text = _dockerfile()
    assert re.search(r"(?m)^HEALTHCHECK\s+(?!NONE)", text), (
        "Dockerfile must define a HEALTHCHECK"
    )
    url = re.search(r"http://[0-9.]+:\d+(/[^\s'\"]*)", text)
    assert url is not None, "HEALTHCHECK must target a local URL"
    path = url.group(1)

    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_no_secrets_baked_into_image_or_compose() -> None:
    """No credential-like assignments or credentialed URLs in packaging."""
    for name, text in (
        ("Dockerfile", _dockerfile()),
        ("docker-compose.yml", _compose()),
    ):
        assert re.search(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]", text
        ) is None, f"possible secret in {name}"
        assert re.search(r"://[^/\s:@]+:[^/\s@]+@", text) is None, (
            f"credentialed URL in {name}"
        )


# ------------------------------------------------------------------- compose


def test_compose_has_single_application_service_on_api_port() -> None:
    """One application service only, mapped to the existing API port."""
    lines = _compose().splitlines()
    scope: list[str] = []
    in_services = False
    for line in lines:
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services:
            if re.match(r"^\S", line):  # next top-level key
                break
            scope.append(line)
    services = re.findall(r"(?m)^  ([A-Za-z0-9_-]+):", "\n".join(scope))
    assert services == ["app"], f"expected a single service, got {services}"

    compose = _compose()
    assert re.search(r"(?m)^\s+- ['\"]?8000:8000['\"]?$", compose), (
        "compose must publish the API port 8000"
    )
    assert re.search(r"(?m)^\s+target:\s*runtime\s*$", compose), (
        "compose must build the runtime stage, not the test stage"
    )


def test_compose_healthcheck_is_defined_or_inherited() -> None:
    """A health check exists for GET /health, defined or Dockerfile-inherited."""
    compose = _compose()
    if re.search(r"(?m)^\s+healthcheck:", compose):
        assert "/health" in compose
    else:
        # Inherited from the image: the Dockerfile must define it.
        assert re.search(r"(?m)^HEALTHCHECK\s+", _dockerfile())


def test_compose_configuration_is_environment_interpolated() -> None:
    """Every compose setting is overridable via ``${VAR:-default}``.

    Keeps configuration (and any future secret) out of the committed
    file: values come from the environment or a git-ignored ``.env``.
    """
    match = re.search(r"(?ms)^    environment:\n(.*?)(?=^    \S|^volumes)", _compose())
    assert match is not None, "compose service must declare environment"
    pairs = re.findall(r"(?m)^      ([A-Z_]+):\s*(.+)$", match.group(1))
    assert pairs, "environment block must not be empty"
    for key, value in pairs:
        assert re.fullmatch(r"\$\{[A-Z_]+:-[^}]+\}", value.strip()), (
            f"{key} must use environment interpolation, got {value!r}"
        )


def test_read_only_root_filesystem_with_scoped_writes() -> None:
    """Compose keeps the root filesystem read-only; writes are scoped.

    Only ``/data`` (named volume: database + uploads) and ``/tmp``
    (processing scratch) are writable.
    """
    compose = _compose()
    assert re.search(r"(?m)^\s+read_only:\s*true\s*$", compose)
    assert re.search(r"(?m)^\s+- app-data:/data\s*$", compose)
    tmpfs = re.search(r"(?ms)^\s+tmpfs:\n(.*?)(?=^\S|\Z)", compose)
    assert tmpfs is not None, "compose must mount a scratch tmpfs"
    assert "/tmp" in tmpfs.group(1)


# -------------------------------------------------------------- .dockerignore


def test_dockerignore_excludes_local_artifacts_and_secrets() -> None:
    """The build context drops VCS, caches, venvs, DBs, uploads, secrets."""
    patterns = set(_dockerignore_patterns())
    required = {
        ".git/",
        "**/__pycache__/",
        ".pytest_cache/",
        ".venv/",
        "venv/",
        "*.db",
        "uploads/",
        ".env",
        "benchmark_output/",
        "!.env.example",
    }
    missing = required - patterns
    assert not missing, f".dockerignore missing patterns: {sorted(missing)}"


def test_dockerignore_keeps_required_project_files() -> None:
    """No exclusion pattern removes files the application/tests require."""
    protected = {
        "app",
        "tests",
        "scripts",
        "pytest.ini",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements.lock.txt",
        "Dockerfile",
        "docker-compose.yml",
        ".dockerignore",
    }
    excluded = {
        pattern.rstrip("/")
        for pattern in _dockerignore_patterns()
        if not pattern.startswith(("!", "**"))
    }
    assert excluded.isdisjoint(protected), (
        f"required files excluded from context: {sorted(excluded & protected)}"
    )
