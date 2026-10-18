from fastapi import Body
from fastapi.testclient import TestClient

from pulseops.api.app import create_app
from pulseops.config import Settings
from pulseops.messaging.queue import InMemoryQueue
from pulseops.observability.logging import redact, redact_text
from pulseops.runtime import Runtime


def _client(max_body: int = 1024) -> TestClient:
    settings = Settings(
        environment="test",
        database_url="postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
        queue_backend="memory",
        max_body_bytes=max_body,
        log_json=False,
    )
    app = create_app(Runtime.from_settings(settings, queue=InMemoryQueue()))

    @app.post("/echo")
    def echo(payload: dict = Body(...)) -> dict:  # type: ignore[type-arg]
        return payload

    return TestClient(app)


def test_health_is_dependency_free() -> None:
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_reports_database_failure_as_503() -> None:
    resp = _client().get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["database"].startswith("error")
    assert body["checks"]["queue"] == "ok"


def test_correlation_id_echoed_and_generated() -> None:
    client = _client()
    resp = client.get("/health", headers={"X-Correlation-Id": "abc12345-req"})
    assert resp.headers["x-correlation-id"] == "abc12345-req"
    generated = client.get("/health", headers={"X-Correlation-Id": "bad id with spaces"})
    assert generated.headers["x-correlation-id"] != "bad id with spaces"
    assert len(generated.headers["x-correlation-id"]) == 32


def test_body_size_limit_returns_413_with_error_schema() -> None:
    resp = _client(max_body=100).post("/echo", json={"x": "a" * 500})
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


def test_unknown_route_uses_error_schema() -> None:
    resp = _client().get("/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_redaction_of_common_secret_shapes() -> None:
    text = (
        "connect postgresql://etl:hunter2@db:5432/x failed; api_key=abc123 "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x.y key sk-ant-api03-ABCDEFGHIJKL"
    )
    out = redact_text(text)
    assert "hunter2" not in out
    assert "abc123" not in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "sk-ant-api03-ABCDEFGHIJKL" not in out
    assert redact({"password": "p", "nested": {"token": "t", "ok": "v"}}) == {
        "password": "[REDACTED]",
        "nested": {"token": "[REDACTED]", "ok": "v"},
    }
