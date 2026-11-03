"""Authentication fixtures use invented passwords and session secrets only."""
import json

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from security import AuthSettings, hash_password, install_auth, verify_password


@pytest.fixture
def secured(tmp_path, monkeypatch):
    for key in ("POSTTRAIN_OPERATOR_NAME", "POSTTRAIN_OPERATOR_LOCATION", "POSTTRAIN_OPERATOR_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    template = tmp_path / "login.html"
    template.write_text('<form action="/login" method="post"><input name="password" type="password"></form>')
    app = Starlette()
    settings = AuthSettings(hash_password("synthetic-test-password", salt=b"s" * 16), "secret" * 10)
    sessions = install_auth(app, template, settings)

    async def stats(request):
        return JSONResponse({"statistic": 123})

    async def socket(websocket):
        await websocket.accept()
        try:
            while True:
                await websocket.receive_text()
                await websocket.send_json({"statistic": 123})
        except WebSocketDisconnect:
            pass

    app.add_route("/", stats)
    app.add_route("/api/metrics", stats)
    app.add_route("/_nicegui/client", stats)
    app.add_route("/static/app.js", stats)
    app.routes.append(WebSocketRoute("/_nicegui_ws/socket.io", socket))
    return app, settings, sessions


def login(client, password="synthetic-test-password", origin="http://127.0.0.1:8766"):
    return client.post("/login", data={"password": password}, headers={"Origin": origin}, follow_redirects=False)


@pytest.mark.parametrize("path", ["/", "/api/metrics", "/_nicegui/client", "/static/app.js", "/docs"])
def test_every_stats_and_nicegui_http_path_requires_login(secured, path):
    app, _, _ = secured
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
        assert "statistic" not in response.text
        assert client.get("/healthz").json() == {"ok": True}
        assert 'name="password"' in client.get("/login").text


def test_wrong_and_right_password_cookie_and_logout(secured, caplog):
    app, settings, sessions = secured
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        wrong = login(client, "incorrect-test-password")
        assert wrong.status_code == 303 and wrong.headers["location"] == "/login?error=1"
        assert settings.cookie_name not in client.cookies
        right = login(client)
        assert right.status_code == 303 and right.headers["location"] == "/"
        cookie = right.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=43200" in cookie
        token = client.cookies.get(settings.cookie_name)
        assert sessions.valid(token)
        assert client.get("/api/metrics").json() == {"statistic": 123}
        assert client.get("/").headers["cache-control"] == "no-store"
        assert client.post("/logout", headers={"Origin": "http://127.0.0.1:8766"}, follow_redirects=False).status_code == 303
        assert not sessions.valid(token)
        assert client.get("/", follow_redirects=False).status_code == 303
        assert "incorrect-test-password" not in caplog.text


def test_public_hostname_uses_secure_cookie(secured):
    app, _, _ = secured
    with TestClient(app, base_url="https://posttrain.dirigent.uk") as client:
        response = login(client, origin="https://posttrain.dirigent.uk")
        assert response.status_code == 303 and "Secure" in response.headers["set-cookie"]
        assert client.get("/").status_code == 200
        assert "strict-transport-security" in client.get("/").headers


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "https://evil.example"}, {"Origin": "null"},
    {"Origin": "http://127.0.0.1:9999"},
    {"Origin": "http://127.0.0.1:8766", "Host": "evil.example", "X-Forwarded-Host": "posttrain.dirigent.uk"},
])
def test_login_rejects_missing_or_cross_origin_and_bad_host(secured, headers):
    app, _, _ = secured
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        response = client.post("/login", data={"password": "synthetic-test-password"}, headers=headers)
        assert response.status_code == 403 and "synthetic-test-password" not in response.text


def test_rate_limit_blocks_further_attempts(secured):
    app, settings, _ = secured
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        for _ in range(settings.login_limit):
            assert login(client, "wrong").status_code == 303
        response = login(client)
        assert response.status_code == 429
        assert response.headers["retry-after"] == str(settings.login_window_seconds)


def test_tampered_and_expired_cookies_fail_closed(secured):
    app, settings, sessions = secured
    clock = [1000]
    sessions.clock = lambda: clock[0]
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        login(client)
        original = client.cookies.get(settings.cookie_name)
        assert not sessions.valid("malformed.non-ascii-💥")
        client.cookies.set(settings.cookie_name, "malformed.signature")
        assert client.get("/", follow_redirects=False).status_code == 303
        client.cookies.set(settings.cookie_name, original)
        clock[0] += settings.session_ttl_seconds + 1
        assert client.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("origin,code", [("http://127.0.0.1:8766", 4401), ("https://evil.example", 4403), (None, 4403)])
def test_unauthenticated_or_cross_origin_websocket_never_connects(secured, origin, code):
    app, _, _ = secured
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        with pytest.raises(WebSocketDisconnect) as failed:
            with client.websocket_connect("ws://127.0.0.1:8766/_nicegui_ws/socket.io", headers={"Origin": origin} if origin else {}):
                pytest.fail("unauthorized websocket connected")
        assert failed.value.code == code


def test_authenticated_websocket_is_revoked_by_logout(secured):
    app, _, _ = secured
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        login(client)
        with client.websocket_connect("ws://127.0.0.1:8766/_nicegui_ws/socket.io", headers={"Origin": "http://127.0.0.1:8766"}) as websocket:
            websocket.send_text("refresh")
            assert websocket.receive_json() == {"statistic": 123}
            client.post("/logout", headers={"Origin": "http://127.0.0.1:8766"}, follow_redirects=False)
            websocket.send_text("refresh")
            with pytest.raises(WebSocketDisconnect) as failed:
                websocket.receive_json()
            assert failed.value.code == 4401


def test_open_websocket_loses_access_when_session_expires(secured):
    app, settings, sessions = secured
    clock = [1000]
    sessions.clock = lambda: clock[0]
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        login(client)
        with client.websocket_connect("ws://127.0.0.1:8766/_nicegui_ws/socket.io", headers={"Origin": "http://127.0.0.1:8766"}) as websocket:
            clock[0] += settings.session_ttl_seconds + 1
            websocket.send_text("refresh")
            with pytest.raises(WebSocketDisconnect) as failed:
                websocket.receive_json()
            assert failed.value.code == 4401


def test_password_hash_is_salted_and_environment_fails_closed(monkeypatch):
    first, second = hash_password("synthetic-test-password"), hash_password("synthetic-test-password")
    assert first != second and verify_password("synthetic-test-password", first)
    assert not verify_password("other", first)
    assert not verify_password("synthetic-test-password", "scrypt$malformed")
    monkeypatch.delenv("POSTTRAIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("POSTTRAIN_SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="Set valid POSTTRAIN"):
        AuthSettings.from_env()
