"""Password-only ASGI gate covering both HTTP and NiceGUI websocket traffic."""
from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
import hmac
from http.cookies import SimpleCookie
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse


def hash_password(password, *, salt=None):
    """Return a salted scrypt hash; plaintext is never persisted or logged."""
    salt = secrets.token_bytes(16) if salt is None else salt
    if not isinstance(password, str) or not isinstance(salt, bytes) or len(salt) < 16:
        raise ValueError("invalid_password_hash_input")
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1,
                             dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def verify_password(password, encoded):
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or (n, r, p) != ("16384", "8", "1"):
            return False
        salt_bytes, expected_bytes = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(salt_bytes) < 16 or len(salt_bytes) > 64 or len(expected_bytes) != 32:
            return False
        if not isinstance(password, str) or len(password.encode("utf-8")) > 1024:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt_bytes, n=16384, r=8, p=1,
                                dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, expected_bytes)
    except (TypeError, ValueError, UnicodeError):
        return False


@dataclass(frozen=True)
class AuthSettings:
    password_hash: str = field(repr=False)
    session_secret: str = field(repr=False)
    public_host: str = "posttrain.dirigent.uk"
    allowed_hosts: tuple = ("posttrain.dirigent.uk", "127.0.0.1", "localhost", "::1")
    session_ttl_seconds: int = 43200
    cookie_name: str = "posttrain_session"
    login_limit: int = 5
    login_window_seconds: int = 300

    def __post_init__(self):
        if (len(self.session_secret) < 32 or re.fullmatch(
                r"scrypt\$16384\$8\$1\$[a-f0-9]{32,128}\$[a-f0-9]{64}", self.password_hash) is None):
            raise ValueError("invalid_auth_configuration")
        if not 60 <= self.session_ttl_seconds <= 604800 or self.login_limit < 1 or self.login_window_seconds < 1:
            raise ValueError("invalid_auth_configuration")

    @classmethod
    def from_env(cls):
        try:
            return cls(password_hash=os.environ["POSTTRAIN_PASSWORD_HASH"],
                       session_secret=os.environ["POSTTRAIN_SESSION_SECRET"],
                       session_ttl_seconds=int(os.environ.get("POSTTRAIN_SESSION_TTL_SECONDS", "43200")))
        except (KeyError, ValueError):
            raise RuntimeError("Set valid POSTTRAIN_PASSWORD_HASH and POSTTRAIN_SESSION_SECRET") from None


class Sessions:
    def __init__(self, settings, *, clock=time.time):
        self.settings, self.clock = settings, clock
        self.values = {}
        self.lock = threading.RLock()

    def _signature(self, token):
        return hmac.new(self.settings.session_secret.encode(), token.encode(), hashlib.sha256).hexdigest()

    def issue(self):
        with self.lock:
            now = self.clock()
            self.values = {key: expiry for key, expiry in self.values.items() if expiry > now}
            if len(self.values) >= 1024:
                self.values.pop(next(iter(self.values)))
            token = secrets.token_urlsafe(32)
            self.values[token] = now + self.settings.session_ttl_seconds
            return f"{token}.{self._signature(token)}"

    def valid(self, cookie):
        if not isinstance(cookie, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}\.[a-f0-9]{64}", cookie) is None:
            return False
        try:
            token, signature = cookie.split(".")
        except ValueError:
            return False
        if not hmac.compare_digest(self._signature(token), signature):
            return False
        with self.lock:
            expires = self.values.get(token, 0)
            if expires <= self.clock():
                self.values.pop(token, None)
                return False
            return True

    def revoke(self, cookie):
        if isinstance(cookie, str):
            with self.lock:
                self.values.pop(cookie.split(".", 1)[0], None)


class LoginLimiter:
    def __init__(self, settings, *, clock=time.time):
        self.settings, self.clock = settings, clock
        self.attempts = defaultdict(deque)
        self.lock = threading.RLock()

    def allow(self, key):
        with self.lock:
            now = self.clock()
            cutoff = now - self.settings.login_window_seconds
            for stale in list(self.attempts):
                while self.attempts[stale] and self.attempts[stale][0] <= cutoff:
                    self.attempts[stale].popleft()
                if not self.attempts[stale]:
                    self.attempts.pop(stale, None)
            if len(self.attempts) >= 4096 and key not in self.attempts:
                return False
            bucket = self.attempts[key]
            if len(bucket) >= self.settings.login_limit:
                return False
            bucket.append(now)
            return True


class AuthGate:
    """Pure ASGI middleware; no HTTP-only bypass for /_nicegui or sockets."""
    def __init__(self, app, *, settings, sessions, limiter):
        self.app, self.settings, self.sessions, self.limiter = app, settings, sessions, limiter

    def _connection(self, scope):
        headers = {}
        for key, value in scope.get("headers", []):
            key = key.decode("latin-1").lower()
            if key in headers and key in {"host", "origin", "cookie"}:
                return None
            headers[key] = value.decode("latin-1")
        raw_host = headers.get("host", "")
        try:
            parsed = urlsplit("//" + raw_host)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if (not host or host not in self.settings.allowed_hosts or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
            return None
        public = host == self.settings.public_host
        origin = headers.get("origin")
        if origin:
            try:
                origin_url = urlsplit(origin)
                origin_port = origin_url.port or (443 if origin_url.scheme == "https" else 80)
            except ValueError:
                return None
            expected_scheme = "https" if public else ("https" if scope.get("scheme") in {"https", "wss"} else "http")
            expected_port = port or (443 if expected_scheme == "https" else 80)
            if (origin_url.scheme != expected_scheme or origin_url.hostname != host
                    or origin_port != expected_port or origin_url.path or origin_url.query
                    or origin_url.fragment or origin_url.username is not None):
                return None
        elif scope["type"] == "websocket" or scope.get("method") not in {"GET", "HEAD", "OPTIONS"}:
            return None
        cookie = None
        try:
            parsed_cookie = SimpleCookie()
            parsed_cookie.load(headers.get("cookie", ""))
            morsel = parsed_cookie.get(self.settings.cookie_name)
            cookie = morsel.value if morsel else None
        except Exception:
            pass
        return host, public, cookie

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        connection = self._connection(scope)
        websocket = scope["type"] == "websocket"
        if connection is None:
            if websocket:
                return await send({"type": "websocket.close", "code": 4403})
            return await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
        host, public, cookie = connection
        path, method = scope.get("path", "/"), scope.get("method", "GET")
        unrestricted = not websocket and ((path == "/login" and method in {"GET", "POST"})
                                          or (path == "/healthz" and method == "GET"))
        authenticated = self.sessions.valid(cookie)
        if not unrestricted and not authenticated:
            if websocket:
                return await send({"type": "websocket.close", "code": 4401})
            return await RedirectResponse("/login", status_code=303)(scope, receive, send)
        scope["posttrain.auth"] = {"authenticated": authenticated, "public": public, "cookie": cookie}
        closed = False

        async def secured_send(message):
            nonlocal closed
            if websocket:
                if closed:
                    return
                if message["type"] in {"websocket.accept", "websocket.send"} and not self.sessions.valid(cookie):
                    closed = True
                    return await send({"type": "websocket.close", "code": 4401})
            elif message["type"] == "http.response.start":
                additions = [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                             (b"x-frame-options", b"DENY"), (b"referrer-policy", b"same-origin"),
                             (b"content-security-policy", b"frame-ancestors 'none'; base-uri 'self'; form-action 'self'")]
                if public:
                    additions.append((b"strict-transport-security", b"max-age=31536000"))
                names = {key for key, _ in additions}
                message = {**message, "headers": [(key, value) for key, value in message.get("headers", [])
                                                   if key.lower() not in names] + additions}
            await send(message)

        await self.app(scope, receive, secured_send)


def install_auth(app, login_html_path, settings):
    """Install before serving NiceGUI; return session manager for testing only."""
    sessions, limiter = Sessions(settings), LoginLimiter(settings)
    template = Path(login_html_path)

    async def login_get(request: Request):
        if request.scope["posttrain.auth"]["authenticated"]:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(template.read_text(encoding="utf-8"))

    async def login_post(request: Request):
        peer = request.scope.get("client")
        if not limiter.allow(peer[0] if peer else "local"):
            return PlainTextResponse("Too many attempts. Try again later.", status_code=429,
                                     headers={"Retry-After": str(settings.login_window_seconds)})
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/x-www-form-urlencoded":
            return RedirectResponse("/login?error=1", status_code=303)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return RedirectResponse("/login?error=1", status_code=303)
        try:
            values = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=4)
            password = values.get("password", [])
            valid = len(password) == 1 and verify_password(password[0], settings.password_hash)
        except (UnicodeError, ValueError):
            valid = False
        if not valid:
            return RedirectResponse("/login?error=1", status_code=303)
        previous = request.scope["posttrain.auth"]["cookie"]
        sessions.revoke(previous)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(settings.cookie_name, sessions.issue(), max_age=settings.session_ttl_seconds,
                            httponly=True, secure=request.scope["posttrain.auth"]["public"]
                            or request.url.scheme == "https", samesite="strict", path="/")
        return response

    async def logout(request: Request):
        sessions.revoke(request.scope["posttrain.auth"]["cookie"])
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(settings.cookie_name, path="/", httponly=True, samesite="strict",
                               secure=request.scope["posttrain.auth"]["public"] or request.url.scheme == "https")
        return response

    async def health(request: Request):
        return JSONResponse({"ok": True})

    app.add_route("/login", login_get, methods=["GET"])
    app.add_route("/login", login_post, methods=["POST"])
    app.add_route("/logout", logout, methods=["POST"])
    app.add_route("/healthz", health, methods=["GET"])
    app.add_middleware(AuthGate, settings=settings, sessions=sessions, limiter=limiter)
    return sessions
