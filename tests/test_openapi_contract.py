"""Tests for the published OpenAPI contract.

The generated schema *is* the API's published surface: every required
operation must appear with the right method, the upload must document
its multipart ``file`` part, error responses must reference the shared
``ErrorResponse`` envelope, and the success responses must carry their
response models. Guarding the schema here catches accidental contract
drift — a removed route, a dropped error model, an undocumented
parameter — without asserting incidental wording.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

# Operations the assignment requires to be documented.
REQUIRED_OPERATIONS: dict[str, set[str]] = {
    "/health": {"get"},
    "/api/files/": {"post"},
    "/api/files/{file_id}/": {"get"},
    "/api/files/{file_id}/measurements/": {"get"},
}

# Documented error responses per operation.
ERROR_RESPONSES: dict[tuple[str, str], set[str]] = {
    ("/api/files/", "post"): {"400", "413", "415", "422", "500"},
    ("/api/files/{file_id}/", "get"): {"404", "422", "500"},
    ("/api/files/{file_id}/measurements/", "get"): {"404", "422", "500"},
}


def _schema(client: TestClient) -> dict[str, Any]:
    """Fetch the OpenAPI document served by the app.

    Args:
        client: Test client bound to the application.

    Returns:
        The parsed OpenAPI schema.
    """
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def _operation(
    schema: dict[str, Any], path: str, method: str
) -> dict[str, Any]:
    """Return one documented operation.

    Args:
        schema: The OpenAPI document.
        path: Path key to look up.
        method: HTTP method of the operation.

    Returns:
        The operation object.
    """
    return schema["paths"][path][method]


def _response_schema_ref(
    operation: dict[str, Any], status: str
) -> str:
    """Return the ``$ref`` name of a response's JSON schema.

    Args:
        operation: OpenAPI operation object.
        status: HTTP status code of the response.

    Returns:
        The trailing component name of the schema reference.

    Raises:
        AssertionError: If the response carries no JSON schema ref.
    """
    content = operation["responses"][status]["content"]
    ref = content["application/json"]["schema"]["$ref"]
    assert ref.startswith("#/components/schemas/"), ref
    return ref.rsplit("/", 1)[-1]


def test_schema_is_served_and_versioned(client: TestClient) -> None:
    """``/openapi.json`` returns an OpenAPI 3.x document."""
    schema = _schema(client)

    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"]
    assert schema["info"]["version"]


def test_required_operations_are_documented(client: TestClient) -> None:
    """Every required endpoint appears with the required methods."""
    schema = _schema(client)

    for path, methods in REQUIRED_OPERATIONS.items():
        assert path in schema["paths"], path
        for method in methods:
            assert method in schema["paths"][path], f"{method} {path}"


def test_upload_documents_multipart_file_part(
    client: TestClient,
) -> None:
    """The upload operation requires a multipart ``file`` part."""
    schema = _schema(client)
    operation = _operation(schema, "/api/files/", "post")

    body = operation["requestBody"]
    assert body["required"] is True
    form_schema = body["content"]["multipart/form-data"]["schema"]
    resolved = schema["components"]["schemas"][
        form_schema["$ref"].rsplit("/", 1)[-1]
    ]
    assert "file" in resolved["properties"]
    assert "file" in resolved.get("required", [])
    assert resolved["properties"]["file"]["type"] == "string"


def test_upload_error_responses_use_error_envelope(
    client: TestClient,
) -> None:
    """All documented upload errors reference ``ErrorResponse``."""
    schema = _schema(client)
    operation = _operation(schema, "/api/files/", "post")

    assert set(operation["responses"]) == ERROR_RESPONSES[
        ("/api/files/", "post")
    ] | {"201"}
    for status in ERROR_RESPONSES[("/api/files/", "post")]:
        assert _response_schema_ref(operation, status) == "ErrorResponse"


def test_retrieval_error_responses_use_error_envelope(
    client: TestClient,
) -> None:
    """Both retrieval endpoints document the shared error envelope."""
    schema = _schema(client)

    for path in ("/api/files/{file_id}/", "/api/files/{file_id}/measurements/"):
        operation = _operation(schema, path, "get")
        assert set(operation["responses"]) == ERROR_RESPONSES[
            (path, "get")
        ] | {"200"}
        for status in ERROR_RESPONSES[(path, "get")]:
            assert _response_schema_ref(operation, status) == "ErrorResponse"


def test_success_responses_reference_their_models(
    client: TestClient,
) -> None:
    """Success responses carry the declared response models."""
    schema = _schema(client)

    assert _response_schema_ref(
        _operation(schema, "/api/files/", "post"), "201"
    ) == "FileUploadResponse"
    assert _response_schema_ref(
        _operation(schema, "/api/files/{file_id}/", "get"), "200"
    ) == "FileInfoResponse"
    assert _response_schema_ref(
        _operation(schema, "/api/files/{file_id}/measurements/", "get"), "200"
    ) == "FileMeasurementsResponse"
    assert _response_schema_ref(
        _operation(schema, "/health", "get"), "200"
    ) == "HealthResponse"


def test_error_envelope_shape_is_documented(
    client: TestClient,
) -> None:
    """``ErrorResponse`` requires an ``error`` with ``code`` + ``message``."""
    schema = _schema(client)
    components = schema["components"]["schemas"]

    error = components["ErrorResponse"]
    assert error["required"] == ["error"]
    detail = components[
        error["properties"]["error"]["$ref"].rsplit("/", 1)[-1]
    ]
    assert set(detail["required"]) == {"code", "message"}
    assert detail["properties"]["code"]["type"] == "string"
    assert detail["properties"]["message"]["type"] == "string"


def test_file_id_path_parameter_is_declared_uuid(
    client: TestClient,
) -> None:
    """Both retrieval paths document ``file_id`` as a required UUID."""
    schema = _schema(client)

    for path in ("/api/files/{file_id}/", "/api/files/{file_id}/measurements/"):
        parameters = _operation(schema, path, "get")["parameters"]
        (file_id,) = [p for p in parameters if p["name"] == "file_id"]
        assert file_id["in"] == "path"
        assert file_id["required"] is True
        assert file_id["schema"]["type"] == "string"
        assert file_id["schema"]["format"] == "uuid"
