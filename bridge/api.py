"""Portal API (aiohttp) + static files. Contract: topic-spec.md "Portal-API"."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from . import registers as rg
from .cache import Cache, iso
from .commands import COMMANDS, CommandExecutor
from .config import LIMITS, Config, validate_editable
from .history import History, HistoryUnavailable, RequestError
from .logbuffer import RingBufferHandler, set_level
from .state import Runtime

log = logging.getLogger("bridge.api")

COOKIE = "h2m_session"
SESSION_TTL = 12 * 3600
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 60.0

PLACEHOLDER = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>huawei2mqtt</title>"
               "</head><body><h1>huawei2mqtt</h1><p>Portal files not found. "
               "API: <a href=\"/api/status\">/api/status</a></p></body></html>")


class Sessions:
    """Signed, stateless session tokens. Key = secret + hash of the portal password."""

    def __init__(self, secret: bytes, password: Callable[[], str],
                 clock: Callable[[], float] = time.time) -> None:
        self._secret, self._password, self._clock = secret, password, clock

    def _key(self) -> bytes:
        return hashlib.sha256(self._secret + self._password().encode()).digest()

    def issue(self) -> str:
        body = base64.urlsafe_b64encode(
            json.dumps({"exp": int(self._clock()) + SESSION_TTL}).encode()).decode()
        sig = hmac.new(self._key(), body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def valid(self, token: str | None) -> bool:
        if not token or "." not in token or not self._password():
            return False
        body, _, sig = token.partition(".")
        good = hmac.new(self._key(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return False
        try:
            return json.loads(base64.urlsafe_b64decode(body.encode()))["exp"] > self._clock()
        except (ValueError, KeyError, TypeError):
            return False


class LoginLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, ip: str) -> deque[float]:
        q = self._fails[ip]
        now = self._clock()
        while q and now - q[0] > LOGIN_WINDOW:
            q.popleft()
        if not q:
            self._fails.pop(ip, None)
        return q

    def blocked(self, ip: str) -> bool:
        return len(self._prune(ip)) >= LOGIN_MAX_FAILS

    def fail(self, ip: str) -> None:
        self._prune(ip)
        self._fails[ip].append(self._clock())


def client_ip(request: web.Request) -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote or "?"


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


@web.middleware
async def middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        if request.path.startswith("/api/"):
            resp = json_response({"error": exc.reason or "error"}, exc.status)
        else:
            raise
    except Exception:  # noqa: BLE001
        log.exception("unhandled error in %s %s", request.method, request.path)
        resp = json_response({"error": "internal_error"}, 500)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


class Api:
    def __init__(self, cfg: Config, rt: Runtime, cache: Cache, executor: CommandExecutor,
                 ring: RingBufferHandler, sessions: Sessions,
                 is_mqtt_connected: Callable[[], bool] | None = None,
                 history: History | None = None) -> None:
        self.history = history
        self.cfg, self.rt, self.cache, self.exe, self.ring = cfg, rt, cache, executor, ring
        self.sessions = sessions
        self.limiter = LoginLimiter()
        self.on_settings_changed: Callable[[], None] | None = None

    # -- auth ----------------------------------------------------------------------
    def logged_in(self, request: web.Request) -> bool:
        return self.sessions.valid(request.cookies.get(COOKIE))

    def require_write(self, request: web.Request) -> None:
        if not self.cfg.portal_password:
            raise web.HTTPForbidden(reason="read_only")
        if not self.logged_in(request):
            raise web.HTTPUnauthorized(reason="login_required")

    async def body(self, request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(reason="invalid_json") from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(reason="invalid_json")
        return data

    # -- handlers --------------------------------------------------------------------
    async def healthz(self, request: web.Request) -> web.Response:
        return json_response({"status": "ok"})

    async def status(self, request: web.Request) -> web.Response:
        c, rt = self.cfg, self.rt
        return json_response({
            "bridge": {"version": rt.version, "uptime_s": rt.uptime_s, "started": iso(rt.started)},
            "inverter": {"state": rt.inverter, "last_success": rt.last_success_iso,
                         "last_error": rt.last_error, "dropouts_24h": rt.dropouts_24h,
                         "last_dropout": iso(rt.last_dropout) if rt.last_dropout else None},
            "poll": {"live_interval_s": c.poll_interval_live,
                     "last_live_at": iso(rt.last_live_at) if rt.last_live_at else None,
                     "next_live_at": iso(rt.next_live_at) if rt.next_live_at else None},
            "history": self.history.status() if self.history else
            {"days": c.history_days, "samples": 0, "samples_5m": 0, "oldest": None,
             "detail_oldest": None, "db_size_bytes": 0},
            "dongle": {"state": rt.dongle, "host": c.huawei_host, "port": c.huawei_port,
                       "unit_id": c.huawei_unit_id},
            "mqtt": {"state": "connected" if rt.mqtt_connected else "disconnected",
                     "host": c.mqtt_host, "port": c.mqtt_port, "last_error": rt.mqtt_error,
                     "base_topic": rt.base},
            "write_access": rt.write_access,
            "shutdown_allowed": c.allow_shutdown,
            "auth": {"password_set": bool(c.portal_password),
                     "logged_in": self.logged_in(request)},
        })

    async def values(self, request: web.Request) -> web.Response:
        out: dict[str, Any] = {g: self.cache.group_payload(g) for g in rg.GROUP_FIELDS}
        out["registers"] = self.cache.registers()
        return json_response(out)

    async def logs(self, request: web.Request) -> web.Response:
        try:
            since = int(request.query.get("since", "0"))
        except ValueError:
            raise web.HTTPBadRequest(reason="invalid_since") from None
        return json_response({"entries": self.ring.entries(since)})

    async def login(self, request: web.Request) -> web.Response:
        if not self.cfg.portal_password:
            raise web.HTTPForbidden(reason="read_only")
        ip = client_ip(request)
        if self.limiter.blocked(ip):
            return json_response({"error": "too_many_attempts"}, 429)
        data = await self.body(request)
        pw = data.get("password")
        if not isinstance(pw, str) or not hmac.compare_digest(
                hashlib.sha256(pw.encode()).digest(),
                hashlib.sha256(self.cfg.portal_password.encode()).digest()):
            self.limiter.fail(ip)
            log.warning("portal login failed from %s", ip)
            return json_response({"error": "invalid_credentials"}, 401)
        resp = json_response({"ok": True})
        secure = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
        resp.set_cookie(COOKIE, self.sessions.issue(), max_age=SESSION_TTL, httponly=True,
                        samesite="Strict", secure=secure, path="/")
        return resp

    async def logout(self, request: web.Request) -> web.Response:
        self.require_write(request)
        resp = web.Response(status=204)
        resp.del_cookie(COOKIE, path="/")
        return resp

    def _settings_view(self) -> dict[str, Any]:
        c = self.cfg
        return {
            "editable": c.editable(),
            "readonly": {
                "huawei_host": c.huawei_host, "huawei_port": c.huawei_port,
                "huawei_unit_id": c.huawei_unit_id, "mqtt_host": c.mqtt_host,
                "mqtt_port": c.mqtt_port, "mqtt_username_set": bool(c.mqtt_username),
                "mqtt_password_set": bool(c.mqtt_password), "mqtt_tls": c.mqtt_tls,
                "portal_password_set": bool(c.portal_password),
                "allow_shutdown": c.allow_shutdown, "base_topic": self.rt.base,
                "software_version": self.cache.value("software_version"),
                "firmware_version": self.cache.value("firmware_version"),
            },
            "limits": LIMITS,
        }

    async def get_settings(self, request: web.Request) -> web.Response:
        return json_response(self._settings_view())

    async def put_settings(self, request: web.Request) -> web.Response:
        self.require_write(request)
        data = await self.body(request)
        clean, errors = validate_editable(data)
        if errors:
            return json_response({"error": "validation", "fields": errors}, 400)
        self.cfg.apply_editable(clean)
        if "log_level" in clean:
            set_level(clean["log_level"])
        log.info("settings changed: %s", ", ".join(sorted(clean)))
        if self.on_settings_changed:
            self.on_settings_changed()
        return json_response(self._settings_view())

    async def command(self, request: web.Request) -> web.Response:
        self.require_write(request)
        data = await self.body(request)
        command = data.get("command")
        if command not in COMMANDS or "value" not in data:
            return json_response({"error": "validation",
                                  "fields": {"command": "unknown_command"}}, 400)
        return json_response(await self.exe.execute(command, data["value"]))

    async def write_test(self, request: web.Request) -> web.Response:
        self.require_write(request)
        return json_response(await self.exe.write_test())

    # -- history / awards (rc2) -------------------------------------------------------
    def _hist(self) -> History:
        if self.history is None:
            raise web.HTTPServiceUnavailable(reason="history_unavailable")
        return self.history

    async def _hist_call(self, coro: Any) -> web.Response:
        try:
            return json_response(await coro)
        except RequestError as exc:
            return json_response({"error": exc.reason}, 400)
        except HistoryUnavailable:
            return json_response({"error": "history_unavailable"}, 503)

    async def get_history(self, request: web.Request) -> web.Response:
        q = request.query
        return await self._hist_call(self._hist().samples(
            q.get("from"), q.get("to"), q.get("fields"), q.get("points")))

    async def get_daily(self, request: web.Request) -> web.Response:
        q = request.query
        return await self._hist_call(self._hist().daily(q.get("from"), q.get("to")))

    async def get_awards(self, request: web.Request) -> web.Response:
        return await self._hist_call(self._hist().awards(self.cache.value("total_yield_kwh")))

    async def history_clear(self, request: web.Request) -> web.Response:
        self.require_write(request)
        data = await self.body(request)
        return await self._hist_call(self._hist().clear(data.get("scope")))

    # -- static files ------------------------------------------------------------------
    def _file(self, rel: str) -> Path | None:
        root = self.cfg.web_dir.resolve()
        try:
            p = (root / rel).resolve()
            p.relative_to(root)
        except (ValueError, OSError):
            return None
        return p if p.is_file() else None

    def _serve(self, p: Path) -> web.Response:
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if p.suffix == ".js":
            ctype = "application/javascript"
        text = ctype.startswith("text/") or ctype in ("application/javascript", "application/json")
        return web.Response(body=p.read_bytes(), content_type=ctype,
                            charset="utf-8" if text else None)

    async def index(self, request: web.Request) -> web.Response:
        p = self._file("index.html")
        if p is None:
            return web.Response(text=PLACEHOLDER, content_type="text/html", charset="utf-8")
        return self._serve(p)

    async def static(self, request: web.Request) -> web.Response:
        rel = request.match_info["tail"].lstrip("/")
        if request.path.startswith("/static/"):
            p = self._file(rel) or self._file(f"static/{rel}")
        else:
            p = self._file(rel) if rel else None
        if p is None:
            raise web.HTTPNotFound()
        return self._serve(p)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[middleware])
        app.add_routes([
            web.get("/healthz", self.healthz),
            web.get("/api/status", self.status),
            web.get("/api/values", self.values),
            web.get("/api/logs", self.logs),
            web.post("/api/login", self.login),
            web.post("/api/logout", self.logout),
            web.get("/api/settings", self.get_settings),
            web.put("/api/settings", self.put_settings),
            web.post("/api/command", self.command),
            web.post("/api/write-test", self.write_test),
            web.get("/api/history", self.get_history),
            web.get("/api/daily", self.get_daily),
            web.get("/api/awards", self.get_awards),
            web.post("/api/history/clear", self.history_clear),
            web.get("/", self.index),
            web.get("/static/{tail:.*}", self.static),
            web.get("/{tail:(?!api/).+}", self.static),
        ])
        return app
