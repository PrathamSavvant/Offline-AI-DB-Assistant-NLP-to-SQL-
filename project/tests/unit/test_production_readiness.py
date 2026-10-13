"""
Tests for production readiness features:
- Security headers
- CORS headers & preflight OPTIONS handling
- Centralized JSON error responses on /api/*
- Container probes: /healthz, /readyz
- Enhanced health metrics
- Safe settings inspection
- Preflight validation runner
- Graceful shutdown lifecycle
"""
import pytest
from app import app
from config.settings import settings
from preflight import (
    check_python_version,
    check_packages,
    check_settings,
    check_filesystem,
    run_preflight,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


class TestSecurityHeaders:
    def test_security_headers_present_on_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "default-src 'self'" in resp.headers.get("Content-Security-Policy", "")

    def test_security_headers_present_on_api(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"


class TestCORSAndPreflight:
    def test_cors_headers_on_api_endpoints(self, client, monkeypatch):
        monkeypatch.setattr(settings, "CORS_ORIGINS", "http://localhost:3000")
        resp = client.get("/api/health", headers={"Origin": "http://localhost:3000"})
        assert resp.status_code == 200
        assert resp.headers.get("Access-Control-Allow-Origin") in ("*", "http://localhost:3000")
        assert "GET" in resp.headers.get("Access-Control-Allow-Methods", "")

    def test_options_preflight_on_api_endpoints(self, client, monkeypatch):
        monkeypatch.setattr(settings, "CORS_ORIGINS", "http://localhost:3000")
        resp = client.open("/api/agent/chat", method="OPTIONS", headers={"Origin": "http://localhost:3000"})
        assert resp.status_code == 200
        assert resp.headers.get("Access-Control-Allow-Origin") is not None
        assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")
        assert "Content-Type" in resp.headers.get("Access-Control-Allow-Headers", "")

    def test_unlisted_origin_is_not_allowed(self, client, monkeypatch):
        monkeypatch.setattr(settings, "CORS_ORIGINS", "http://localhost:3000")
        resp = client.get("/api/health", headers={"Origin": "http://untrusted.invalid"})
        assert "Access-Control-Allow-Origin" not in resp.headers

    def test_unlisted_origin_cannot_post(self, client, monkeypatch):
        monkeypatch.setattr(settings, "CORS_ORIGINS", "")
        resp = client.post("/api/execute", json={"sql": "SELECT 1"},
                           headers={"Origin": "http://untrusted.invalid"})
        assert resp.status_code == 403


class TestCentralizedErrorHandlers:
    def test_api_404_returns_json(self, client):
        resp = client.get("/api/this-route-does-not-exist")
        assert resp.status_code == 404
        assert resp.is_json
        data = resp.get_json()
        assert "error" in data
        assert data.get("status_code") == 404

    def test_api_405_method_not_allowed_returns_json(self, client):
        resp = client.post("/healthz")
        assert resp.status_code == 405
        assert resp.is_json
        data = resp.get_json()
        assert "error" in data
        assert data.get("status_code") == 405


class TestContainerProbesAndHealth:
    def test_liveness_probe(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "alive"
        assert "uptime_seconds" in data

    def test_readiness_probe(self, client, monkeypatch):
        # Mock database connection check to succeed
        class MockConn:
            def close(self): pass
        monkeypatch.setattr("core.database.Database.connect", lambda self: MockConn())

        resp = client.get("/readyz")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ready"
        assert "database" in data

    def test_api_health_metrics(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data
        assert "environment" in data
        assert "pool_status" in data
        assert "sync_status" in data


class TestProductionConfiguration:
    def test_safe_dict_redaction(self):
        safe = settings.to_safe_dict()
        assert "db_password" not in safe
        assert "secret_key" not in safe
        assert "environment" in safe
        assert "server_port" in safe
        assert "server_threads" in safe


class TestPreflightRunner:
    def test_check_python_version(self):
        ok, msg = check_python_version()
        assert ok is True
        assert "Python" in msg

    def test_check_packages(self):
        passed, missing = check_packages()
        assert len(passed) >= 6
        assert len(missing) == 0

    def test_check_settings(self):
        ok, msg = check_settings()
        assert ok is True
        assert "Settings valid" in msg

    def test_check_filesystem(self):
        passed, errors = check_filesystem()
        assert len(passed) >= 2
        assert len(errors) == 0

    def test_run_preflight_offline_mode(self):
        results = run_preflight(check_db=False, check_llm_server=False)
        assert results["passed"] is True
        assert len(results["errors"]) == 0


class TestGracefulShutdown:
    def test_app_shutdown_hook(self):
        assert hasattr(app, "shutdown")
        # Invoking shutdown should not raise
        app.shutdown()
