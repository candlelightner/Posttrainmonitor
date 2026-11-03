"""Only fictional operator/contact/authentication values are used here."""
from html import escape
from html.parser import HTMLParser
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from legal import OperatorDetails, render_legal
from security import AuthSettings, hash_password, install_auth


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(value for name, value in attrs if name == "href")


@pytest.fixture
def public_app(tmp_path, monkeypatch):
    for key in ("POSTTRAIN_OPERATOR_NAME", "POSTTRAIN_OPERATOR_LOCATION", "POSTTRAIN_OPERATOR_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    app = Starlette()
    settings = AuthSettings(hash_password("invented-legal-test-password"), "invented-session-secret-" * 3)
    template = tmp_path / "login.html"
    template.write_text('<form method="post" action="/login"><input name="password"></form>')
    sessions = install_auth(app, template, settings)

    async def private_stats(request):
        return JSONResponse({"synthetic_private_statistic": 12345})

    for path in ("/", "/api/metrics", "/static/dashboard.css", "/_nicegui/client"):
        app.add_route(path, private_stats)
    return app, sessions


def test_legal_is_public_for_get_and_head_without_starting_a_session(public_app):
    app, sessions = public_app
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        response = client.get("/legal", follow_redirects=False)
        assert response.status_code == 200 and "Legal &amp; Privacy" in response.text
        assert "set-cookie" not in response.headers and sessions.values == {}
        assert response.headers["cache-control"] == "no-store"
        head = client.head("/legal", follow_redirects=False)
        assert head.status_code == 200 and head.content == b""


def test_public_notice_does_not_expose_auth_config_or_statistics(public_app, monkeypatch):
    app, _ = public_app
    monkeypatch.setenv("POSTTRAIN_SESSION_SECRET", "invented-unpublished-secret")
    monkeypatch.setenv("POSTTRAIN_PASSWORD_HASH", "invented-unpublished-hash")
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        text = client.get("/legal").text
        assert "not been configured" in text
        assert "invented-unpublished" not in text and "synthetic_private_statistic" not in text
        for path in ("/", "/api/metrics", "/static/dashboard.css", "/_nicegui/client"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303 and response.headers["location"] == "/login"
            assert "synthetic_private_statistic" not in response.text


def test_operator_configuration_is_explicit_and_html_escaped(monkeypatch):
    operator = OperatorDetails('<script>Example Operator</script>', 'Example <Town> & Region',
                               'operator" onmouseover="test@example.invalid')
    text = render_legal(operator)
    assert escape(operator.name) in text and escape(operator.location) in text
    assert escape(operator.email) in text
    assert "<script>Example Operator</script>" not in text
    assert ' onmouseover="test' not in text and "mailto:operator%22" in text
    monkeypatch.setenv("POSTTRAIN_OPERATOR_NAME", "Fictional Environment Operator")
    assert "Fictional Environment Operator" not in render_legal()
    assert OperatorDetails.from_env().name == "Fictional Environment Operator"


def test_selected_operator_fields_appear_on_route_without_other_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTTRAIN_OPERATOR_NAME", "Example Operator")
    monkeypatch.setenv("POSTTRAIN_OPERATOR_LOCATION", "Example Town")
    monkeypatch.setenv("POSTTRAIN_OPERATOR_EMAIL", "operator@example.invalid")
    monkeypatch.setenv("UNRELATED_PRIVATE_VALUE", "invented-unrelated-secret")
    app = Starlette()
    login = tmp_path / "login.html"
    login.write_text("Sign in")
    settings = AuthSettings(hash_password("invented-test-password"), "invented-session-secret-" * 3)
    install_auth(app, login, settings)
    with TestClient(app, base_url="https://posttrain.dirigent.uk") as client:
        text = client.get("/legal").text
        assert "Example Operator" in text and "Example Town" in text
        assert 'href="mailto:operator@example.invalid"' in text
        assert "invented-unrelated-secret" not in text


def test_notice_describes_current_cookie_and_storage_without_old_admin_policy():
    text = render_legal()
    for expected in ("posttrain_session", "12 hours", "HttpOnly", "SameSite=Strict", "Secure",
                     "10,000 observations", "server memory", "later login", "5 minutes",
                     "HTTP access logging is disabled", "Cloudflare privacy policy"):
        assert expected in text
    assert "seven days" not in text and "SQLite" not in text and "tm_viewer" not in text
    assert "no administrator account" in text


def test_legal_and_login_links_are_standalone_and_safe():
    document = render_legal()
    links = Links()
    links.feed(document)
    assert {"/", "/login", "https://www.cloudflare.com/privacypolicy/", "https://www.dsb.gv.at/"} <= set(links.hrefs)
    assert "<style>" in document and 'href="/static/' not in document
    login_links = Links()
    login_links.feed((Path(__file__).resolve().parents[1] / "static/login.html").read_text())
    assert "/legal" in login_links.hrefs


def test_public_legal_does_not_open_post_or_websocket_auth_bypasses(public_app):
    app, _ = public_app
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        response = client.post("/legal", headers={"Origin": "http://127.0.0.1:8766"}, follow_redirects=False)
        assert response.status_code == 303
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("ws://127.0.0.1:8766/legal", headers={"Origin": "http://127.0.0.1:8766"}):
                pytest.fail("public legal websocket bypassed authentication")
        assert denied.value.code == 4401
