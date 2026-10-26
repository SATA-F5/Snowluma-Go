"""Async client for SnowLuma's official WebUI REST API.

Endpoint map (see ``packages/core/src/webui/server.ts`` upstream):

==============================  ==========================================
POST /api/login                 {"password", "totp?"} -> {"token", ...}
POST /api/logout                invalidate the session token
GET  /api/status                liveness + token check
GET  /api/auth/state            {"mustChangePassword": bool}
POST /api/auth/change-password  {"oldPassword", "newPassword"}
GET  /api/agreements            agreement documents + version
POST /api/agreements/record-consent  {"version"}
GET  /api/qq-list               {"list": [{"uin", "nickname"}]}
GET  /api/connections           live OneBot adapter health per account
GET  /api/processes             QQ processes visible to the hook manager
POST /api/processes/{pid}/load|unload|refresh
GET  /api/config/{uin}          {"config": OneBotConfig}
POST /api/config/{uin}          save + hot reload, {"saved","applied",...}
GET  /api/logs?limit=N          in-memory server log tail
GET  /api/system                host / runtime statistics
GET  /api/update/check          upstream update metadata
==============================  ==========================================

Authentication uses the ``X-SnowLuma-Token`` header (the WebUI deliberately
keeps ``Authorization`` free for reverse proxies).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

SNOWLUMA_TOKEN_HEADER = "X-SnowLuma-Token"


class SnowLumaError(Exception):
    """Base error; ``status``/``code`` mirror the HTTP response."""

    def __init__(self, message: str, *, status: Optional[int] = None, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload or {}


class SnowLumaAuthError(SnowLumaError):
    pass


class SnowLumaConsentRequired(SnowLumaError):
    pass


class SnowLumaPasswordChangeRequired(SnowLumaError):
    pass


class SnowLumaTOTPRequired(SnowLumaError):
    pass


class SnowLumaAPI:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5099",
        password: str = "",
        *,
        token: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.token = token or ""
        self.timeout = timeout
        self.must_change_password = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Plumbing
    # ------------------------------------------------------------------ #

    def update(self, *, base_url: Optional[str] = None, password: Optional[str] = None, token: Optional[str] = None) -> None:
        if base_url:
            self.base_url = base_url.rstrip("/")
        if password is not None:
            self.password = password
        if token is not None:
            self.token = token

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": "snowluma-go-plugin"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def raw_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        auth: bool = True,
        timeout: Optional[float] = None,
    ) -> Tuple[int, Any]:
        """Perform a request and return ``(status, parsed_or_text)``."""
        session = await self._get_session()
        headers: Dict[str, str] = {}
        if auth and self.token:
            headers[SNOWLUMA_TOKEN_HEADER] = self.token
        try:
            async with session.request(
                method.upper(),
                f"{self.base_url}{path}",
                params=params,
                json=json_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout or self.timeout),
            ) as resp:
                text = await resp.text()
                data: Any = text
                if text:
                    try:
                        import json

                        data = json.loads(text)
                    except Exception:
                        data = text
                return resp.status, data
        except asyncio.TimeoutError as e:
            raise SnowLumaError(f"请求超时: {method} {path}") from e
        except aiohttp.ClientError as e:
            raise SnowLumaError(f"无法连接 SnowLuma（{method} {path}）: {e}") from e

    def _raise_for(self, status: int, payload: Any, path: str) -> None:
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or "")
        if status == 401:
            raise SnowLumaAuthError(message or "WebUI 会话已失效", status=status, payload=payload)
        if status == 403 and isinstance(payload, dict):
            if payload.get("consentRequired"):
                raise SnowLumaConsentRequired(
                    message or "请先阅读并同意 SnowLuma 的用户协议与隐私政策",
                    status=status,
                    payload=payload,
                )
            if payload.get("mustChangePassword"):
                raise SnowLumaPasswordChangeRequired(
                    message or "请先修改 SnowLuma WebUI 密码", status=status, payload=payload
                )
        raise SnowLumaError(message or f"SnowLuma API 返回 HTTP {status}（{path}）", status=status, payload=payload)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        auth: bool = True,
        allow_status: Tuple[int, ...] = (200,),
    ) -> Any:
        status, payload = await self.raw_request(
            method, path, params=params, json_body=json_body, auth=auth
        )
        if status not in allow_status:
            self._raise_for(status, payload, path)
        return payload

    async def get(self, path: str, *, params: Optional[Dict[str, Any]] = None, auth: bool = True) -> Any:
        return await self.request("GET", path, params=params, auth=auth)

    async def post(
        self,
        path: str,
        *,
        json_body: Optional[Any] = None,
        auth: bool = True,
        allow_status: Tuple[int, ...] = (200,),
    ) -> Any:
        return await self.request("POST", path, json_body=json_body, auth=auth, allow_status=allow_status)

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    async def login(self, password: Optional[str] = None, totp: str = "") -> Dict[str, Any]:
        """Log in and store the session token.

        Returns the raw payload for callers that need ``mustChangePassword`` /
        ``needsTotp`` handling; raises :class:`SnowLumaAuthError` for a bad
        password and :class:`SnowLumaTOTPRequired` when 2FA is enabled.
        """
        password = self.password if password is None else password
        body: Dict[str, Any] = {"password": password}
        if totp:
            body["totp"] = totp
        status, payload = await self.raw_request(
            "POST", "/api/login", json_body=body, auth=False
        )
        if status == 200 and isinstance(payload, dict):
            if payload.get("needsTotp"):
                raise SnowLumaTOTPRequired("该 SnowLuma 开启了二次验证，请提供 TOTP 或恢复码", payload=payload)
            token = payload.get("token")
            if not token:
                raise SnowLumaAuthError(str(payload.get("message") or "登录失败：响应缺少 token"), payload=payload)
            self.token = str(token)
            self.must_change_password = bool(payload.get("mustChangePassword"))
            return payload
        if status == 429:
            raise SnowLumaAuthError(
                str((payload or {}).get("message") if isinstance(payload, dict) else "") or "登录尝试过多，请稍后再试",
                status=status,
                payload=payload if isinstance(payload, dict) else {},
            )
        if status == 401:
            message = ""
            if isinstance(payload, dict):
                message = str(payload.get("message") or "")
            raise SnowLumaAuthError(message or "WebUI 密码错误", status=status, payload=payload if isinstance(payload, dict) else {})
        self._raise_for(status, payload, "/api/login")
        return {}

    async def ensure_login(self) -> None:
        """Make sure ``self.token`` is usable, logging in when needed."""
        async with self._lock:
            if self.token:
                try:
                    await self.get("/api/status")
                    return
                except SnowLumaAuthError:
                    self.token = ""
                except SnowLumaConsentRequired:
                    # Token is valid, the consent gate is what fired.
                    return
            if not self.password:
                raise SnowLumaAuthError("尚未配置 SnowLuma WebUI 密码")
            await self.login(self.password)

    async def status(self) -> bool:
        try:
            await self.get("/api/status", auth=True)
            return True
        except SnowLumaConsentRequired:
            return True
        except Exception:
            return False

    async def ping(self, timeout: float = 3.0) -> bool:
        """Liveness probe that does not require a session.

        ``/api/status`` answers ``200`` with a token and ``401`` without one;
        both prove the HTTP server is up, which is all startup needs before the
        login step runs.
        """
        try:
            status, _payload = await self.raw_request("GET", "/api/status", auth=False, timeout=timeout)
        except Exception:
            return False
        return status in (200, 401, 403, 429)

    async def auth_state(self) -> Dict[str, Any]:
        data = await self.get("/api/auth/state")
        return data if isinstance(data, dict) else {}

    async def change_password(self, old_password: str, new_password: str) -> bool:
        status, payload = await self.raw_request(
            "POST",
            "/api/auth/change-password",
            json_body={"oldPassword": old_password, "newPassword": new_password},
        )
        if status == 200:
            self.password = new_password
            self.must_change_password = False
            return True
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or "")
        raise SnowLumaError(message or "修改 WebUI 密码失败", status=status, payload=payload if isinstance(payload, dict) else {})

    # ------------------------------------------------------------------ #
    # Agreements (EULA / privacy)
    # ------------------------------------------------------------------ #

    async def agreements(self) -> Dict[str, Any]:
        data = await self.get("/api/agreements")
        return data if isinstance(data, dict) else {}

    async def record_consent(self, version: str) -> Dict[str, Any]:
        data = await self.post("/api/agreements/record-consent", json_body={"version": version})
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ #
    # Accounts / connections / processes
    # ------------------------------------------------------------------ #

    async def qq_list(self) -> List[Dict[str, Any]]:
        data = await self.get("/api/qq-list")
        items = data.get("list") if isinstance(data, dict) else None
        return [item for item in (items or []) if isinstance(item, dict)]

    async def connections(self) -> List[Dict[str, Any]]:
        data = await self.get("/api/connections")
        items = data.get("list") if isinstance(data, dict) else None
        return [item for item in (items or []) if isinstance(item, dict)]

    async def processes(self) -> List[Dict[str, Any]]:
        data = await self.get("/api/processes")
        items = data.get("list") if isinstance(data, dict) else None
        return [item for item in (items or []) if isinstance(item, dict)]

    async def process_action(self, pid: int, action: str) -> Dict[str, Any]:
        if action not in ("load", "unload", "refresh"):
            raise ValueError(f"unsupported process action: {action}")
        data = await self.post(f"/api/processes/{int(pid)}/{action}")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ #
    # OneBot configuration
    # ------------------------------------------------------------------ #

    async def get_onebot_config(self, uin: str) -> Dict[str, Any]:
        data = await self.get(f"/api/config/{uin}")
        config = data.get("config") if isinstance(data, dict) else None
        if not isinstance(config, dict):
            raise SnowLumaError(f"SnowLuma 返回的 OneBot 配置格式异常（UIN={uin}）")
        return config

    async def save_onebot_config(self, uin: str, config: Dict[str, Any]) -> Dict[str, Any]:
        status, payload = await self.raw_request(
            "POST", f"/api/config/{uin}", json_body=config
        )
        if status != 200:
            self._raise_for(status, payload, f"/api/config/{uin}")
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------ #
    # Observability / updates
    # ------------------------------------------------------------------ #

    async def logs(self, limit: int = 200) -> List[Dict[str, Any]]:
        data = await self.get("/api/logs", params={"limit": int(limit)})
        items = data.get("list") if isinstance(data, dict) else None
        return [item for item in (items or []) if isinstance(item, dict)]

    async def system(self) -> Dict[str, Any]:
        data = await self.get("/api/system")
        return data if isinstance(data, dict) else {}

    async def update_check(self, force: bool = False) -> Dict[str, Any]:
        data = await self.get("/api/update/check", params={"force": "true" if force else "false"})
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ #
    # Convenience
    # ------------------------------------------------------------------ #

    @property
    def webui_url(self) -> str:
        return self.base_url

    async def __aenter__(self) -> "SnowLumaAPI":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()
