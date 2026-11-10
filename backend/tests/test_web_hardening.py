"""HTTP hardening (headers, hosts, body cap, docs), same-origin frontend
serving, and worker liveness."""

import threading

import pytest
from fastapi.testclient import TestClient

from app.jobs import worker
from app.jobs.registry import HandlerRegistry
from app.main import create_app
from app.web import API_CSP, APP_CSP
from tests.admin_helpers import MFA_KEY, admin_app
from tests.conftest import make_settings
from tests.crm_helpers import SECRET

PROD = {"app_env": "production", "app_base_url": "https://hub.example.test", "crm_webhook_secret": "s" * 40}


@pytest.fixture
def dist(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>app</title><div id=root></div>")
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("not public")
    return tmp_path


@pytest.fixture
def client(db_conn, migrated_database, pool_cleanup):
    with admin_app(migrated_database, seed=False) as c:
        yield c


def test_api_responses_carry_security_headers(client) -> None:
    for response in (client.get("/api/health"), client.get("/api/portal/me"), client.get("/api/admin/me")):
        h = response.headers
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer" and h["content-security-policy"] == API_CSP
        assert h["cross-origin-opener-policy"] == "same-origin"
        assert "strict-transport-security" not in h  # not in development/test
        assert "access-control-allow-origin" not in h


def test_production_adds_hsts_restricts_hosts_and_hides_docs(db_conn, migrated_database, pool_cleanup) -> None:
    with admin_app(migrated_database, seed=False, **PROD) as c:
        wrong = c.get("/api/portal/me")  # TestClient sends Host: testserver
        assert (wrong.status_code, wrong.json()["code"]) == (400, "invalid_host")
        assert wrong.headers["strict-transport-security"].startswith("max-age=31536000")
        assert c.get("/api/health").status_code in (200, 503)  # probes are exempt from the host check
        right = c.get("/api/portal/me", headers={"Host": "hub.example.test"})
        assert right.status_code == 401
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path, headers={"Host": "hub.example.test"}).status_code == 404


def test_docs_are_available_outside_production(client) -> None:
    assert client.get("/openapi.json").status_code == 200


def test_explicit_allowed_hosts(db_conn, migrated_database, pool_cleanup) -> None:
    with admin_app(migrated_database, seed=False, **PROD, allowed_hosts="hub.example.test, api.example.test") as c:
        assert c.get("/api/admin/me", headers={"Host": "api.example.test:443"}).status_code == 401
        assert c.get("/api/admin/me", headers={"Host": "evil.example"}).status_code == 400


def test_wildcard_hosts_are_refused_in_production(migrated_database) -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="ALLOWED_HOSTS"):
        make_settings(migrated_database, **PROD, admin_mfa_encryption_key=MFA_KEY, allowed_hosts="*")


def test_oversized_bodies_are_refused_before_routing(client) -> None:
    response = client.post("/api/webhooks/crm/deal-won", content=b"x" * (1024 * 1024 + 1),
                           headers={"Content-Type": "application/json"})
    assert (response.status_code, response.json()["code"]) == (413, "payload_too_large")


# ------------------------------------------------------- frontend serving
def test_built_frontend_is_served_on_the_api_origin(db_conn, migrated_database, pool_cleanup, dist) -> None:
    with admin_app(migrated_database, seed=False, frontend_dist_dir=dist) as c:
        for path in ("/", "/admin", "/admin/clients/123", "/portal", "/portal/anything"):
            r = c.get(path)
            assert r.status_code == 200 and "<div id=root>" in r.text, path
            assert r.headers["content-security-policy"] == APP_CSP
            assert r.headers["referrer-policy"] == "no-referrer"
            assert r.headers["cache-control"] == "no-cache"
        asset = c.get("/assets/index-abc123.js")
        assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
        for path in ("/assets/missing.js", "/assets/../secret.txt", "/assets/%2e%2e/secret.txt", "/secret.txt",
                     "/index.html/../secret.txt"):
            assert c.get(path).status_code == 404, path
        assert c.get("/api/does-not-exist").status_code == 404


def test_without_a_build_only_the_api_is_served(client) -> None:
    assert client.get("/admin").status_code == 404
    assert client.get("/").status_code == 404


def test_production_requires_a_real_build_when_configured(migrated_database, tmp_path) -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="FRONTEND_DIST_DIR"):
        make_settings(migrated_database, **PROD, admin_mfa_encryption_key=MFA_KEY, frontend_dist_dir=tmp_path)


# ------------------------------------------------------------ worker health
def test_worker_health_reflects_live_heartbeats(client, db_conn) -> None:
    assert client.get("/api/health/worker").json() == {"status": "no_worker"}
    db_conn.execute("INSERT INTO worker_heartbeats (worker_id) VALUES ('w1')")
    assert client.get("/api/health/worker").status_code == 200
    db_conn.execute("UPDATE worker_heartbeats SET last_seen_at = now() - interval '5 minutes'")
    assert client.get("/api/health/worker").status_code == 503
    db_conn.execute("UPDATE worker_heartbeats SET last_seen_at = now(), stopped_at = now()")
    assert client.get("/api/health/worker").status_code == 503


def test_worker_records_heartbeats_and_its_stop(db_conn, migrated_database, pool_cleanup) -> None:
    db_conn.execute("INSERT INTO worker_heartbeats (worker_id, last_seen_at) VALUES ('old', now() - interval '8 days')")
    stop = threading.Event()
    settings = make_settings(migrated_database, worker_poll_seconds=0.05, crm_webhook_secret=SECRET)
    thread = threading.Thread(target=worker.run, args=(settings, HandlerRegistry(), stop), kwargs={"worker_id": "hb-test"})
    thread.start()
    try:
        for _ in range(100):
            row = db_conn.execute("SELECT * FROM worker_heartbeats WHERE worker_id = 'hb-test'").fetchone()
            if row:
                break
            threading.Event().wait(0.05)
        assert row and row["stopped_at"] is None
    finally:
        stop.set()
        thread.join(timeout=10)
    rows = {r["worker_id"]: r for r in db_conn.execute("SELECT * FROM worker_heartbeats").fetchall()}
    assert set(rows) == {"hb-test"}  # pruned the 8-day-old row
    assert rows["hb-test"]["stopped_at"] is not None


def test_app_factory_does_not_need_settings_at_import() -> None:
    app = create_app(make_settings("postgresql:///ltd_test"))
    assert isinstance(TestClient(app), TestClient)
