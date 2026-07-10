"""Tests del webapp FastAPI: rutas + auth bearer."""

import importlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_no_auth(monkeypatch):
    """Webapp sin auth (sin WEBAPP_AUTH_TOKEN)."""
    monkeypatch.delenv("WEBAPP_AUTH_TOKEN", raising=False)
    from core import webapp
    importlib.reload(webapp)
    return TestClient(webapp.app)


@pytest.fixture
def client_with_auth(monkeypatch):
    """Webapp con token de auth."""
    monkeypatch.setenv("WEBAPP_AUTH_TOKEN", "test-token-12345")
    from core import webapp
    importlib.reload(webapp)
    return TestClient(webapp.app)


# ─── Health check (público siempre) ─────────────────────────────────────────
def test_healthz_public_no_auth(client_no_auth):
    r = client_no_auth.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["auth_required"] is False


def test_healthz_public_with_auth(client_with_auth):
    r = client_with_auth.get("/healthz")
    assert r.status_code == 200
    assert r.json()["auth_required"] is True


# ─── Index público ──────────────────────────────────────────────────────────
def test_index_html_served(client_no_auth):
    r = client_no_auth.get("/")
    assert r.status_code == 200
    assert "Data Cleaner" in r.text


# ─── Auth: rutas protegidas ─────────────────────────────────────────────────
def test_upload_without_auth_blocked_when_required(client_with_auth):
    r = client_with_auth.post("/upload", files={"file": ("test.csv", b"a,b\n1,2\n")})
    assert r.status_code == 401
    assert "Missing" in r.json()["detail"]


def test_upload_with_wrong_token_blocked(client_with_auth):
    r = client_with_auth.post(
        "/upload",
        files={"file": ("test.csv", b"a,b\n1,2\n")},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 403
    assert "Invalid" in r.json()["detail"]


def test_download_without_auth_blocked_when_required(client_with_auth):
    r = client_with_auth.get("/download/foo.csv")
    assert r.status_code == 401


def test_bigdata_without_auth_blocked(client_with_auth):
    r = client_with_auth.post("/bigdata/prepare-dataset", json={})
    assert r.status_code == 401


# ─── Auth deshabilitada: pasa todo ─────────────────────────────────────────
def test_endpoints_pass_through_when_auth_disabled(client_no_auth):
    """Sin token configurado, no requiere Authorization header."""
    # download de archivo inexistente devuelve 404, no 401 — eso prueba que la
    # auth no bloqueó la llamada
    r = client_no_auth.get("/download/no_existe.csv")
    assert r.status_code == 404


# ─── Healthz never requires auth ─────────────────────────────────────────────
def test_healthz_always_public_even_with_auth_enabled(client_with_auth):
    r = client_with_auth.get("/healthz")
    assert r.status_code == 200
