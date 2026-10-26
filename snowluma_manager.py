"""SnowLuma process lifecycle, WebUI session and status aggregation.

Design notes
------------
* The plugin owns the SnowLuma runtime it downloads (``data/.../snowluma``), but
  every *configuration* change goes through SnowLuma's official WebUI API. We
  never hand-edit ``config/onebot_<uin>.json``: ``POST /api/config/<uin>`` saves
  and hot-reloads the adapter set, which is both safer and version-proof.
* Only ``config/runtime.json`` is written before launch, and only to pin the
  WebUI port / host and the hook auto-load switch — fields SnowLuma documents as
  launcher-level runtime settings.
* QQ processes started by this plugin are tracked so that ``stop``/cleanup never
  kills a QQ client the operator started themselves.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from .download import SnowLumaDownloadMixin
from .qq_manager import QQManager, is_qq_process, running_qq_pids
from .snowluma_api import (
    SnowLumaAPI,
    SnowLumaAuthError,
    SnowLumaConsentRequired,
    SnowLumaError,
    SnowLumaTOTPRequired,
)
from .utils import (
    CREATE_NO_WINDOW,
    DEFAULT_SNOWLUMA_PORT,
    IS_LINUX,
    IS_WINDOWS,
    PLUGIN_NAME,
    SNOWLUMA_NODE_RANGE,
    decrypt_secret,
    encrypt_secret,
    generate_strong_password,
    is_supported_node,
    mask_secret,
    node_version_text,
    normalize_base_url,
    platform_asset_key,
    port_is_open,
    resolve_persistent_dir,
    shorten,
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LISTEN_RE = re.compile(r"listening\s+(https?)://([^\s:]+):(\d+)", re.IGNORECASE)
PORT_BUMP_RE = re.compile(r"port\s+(\d+)\s+is\s+in\s+use,\s*using\s+(\d+)\s+instead", re.IGNORECASE)
INITIAL_CRED_RE = re.compile(r"initial\s+credentials:\s*user=(\S+)\s+password=(\S+)", re.IGNORECASE)
VERSION_RE = re.compile(r"SnowLuma[^\d]{0,8}v?(\d+\.\d+\.\d+)", re.IGNORECASE)

LOG_NOISE = (
    "GLX is not present",
    "EGL_NOT_INITIALIZED",
    "eglInitialize",
    "ANGLE Display::initialize",
    "Exiting GPU process",
    "Initialization of all EGL display types failed",
    "Failed to connect to the bus",
    "libva error",
)


class SnowLumaManager(SnowLumaDownloadMixin):
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.context = plugin.context
        self.persistent_dir = resolve_persistent_dir(self.context)
        self.install_dir = self.persistent_dir / "snowluma"
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.persistent_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self.process: Optional[asyncio.subprocess.Process] = None
        self.log_lines: deque = deque(maxlen=1500)
        self.download_state: Dict[str, Any] = {
            "downloading": False,
            "percent": 0,
            "downloaded_mb": 0.0,
            "total_mb": 0.0,
            "speed_kbps": 0.0,
            "current_mirror": "",
            "current_index": 0,
            "total_mirrors": 0,
            "phase": "idle",
            "message": "",
        }
        self._downloading = False
        self._log_task: Optional[asyncio.Task] = None
        self._auto_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()

        self.deploy_mode = "native"
        self.webui_port = int(plugin.config.get("snowluma_port", DEFAULT_SNOWLUMA_PORT) or DEFAULT_SNOWLUMA_PORT)
        self.webui_host = "127.0.0.1"
        self.webui_scheme = "http"
        self.password = ""
        self.api: Optional[SnowLumaAPI] = None
        self.last_error = ""
        self.last_error_at = ""
        self.consent_required = False
        self.must_change_password = False
        self.auth_ok = False
        self.accounts: List[Dict[str, Any]] = []
        self.connections: List[Dict[str, Any]] = []
        self.processes: List[Dict[str, Any]] = []
        self._auto_synced_uin = ""
        self._auto_sync_failures = 0
        self._auto_hook_done = False
        self._qq_autostarted = False
        self._snapshot_ts = 0.0
        self._snapshot: Dict[str, Any] = {}
        self._installed_version = ""
        self._node_cache: Optional[tuple] = None

        self.qq = QQManager(self)

        self._load_password()
        self._refresh_paths()
        self.deploy_mode = self._resolve_deploy_mode()
        self._sync_api_endpoint()

    # ------------------------------------------------------------------ #
    # Small helpers
    # ------------------------------------------------------------------ #

    def _notice(self, message: str, level: str = "info") -> None:
        self._log(message, level)

    def _log(self, message: str, level: str = "info") -> None:
        text = str(message)
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}"
        self.log_lines.append(line)
        try:
            if level == "error":
                logger.error(f"[{PLUGIN_NAME}] {text}")
            elif level == "warning":
                logger.warning(f"[{PLUGIN_NAME}] {text}")
            else:
                logger.info(f"[{PLUGIN_NAME}] {text}")
        except Exception:
            pass

    def _record_error(self, message: str) -> None:
        self.last_error = message
        self.last_error_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._log(f"[err] {message}", "error")

    def logs_since(self, since: int) -> Dict[str, Any]:
        lines = list(self.log_lines)
        since = max(0, min(int(since or 0), len(lines)))
        return {"lines": lines[since:], "total": len(lines)}

    # ------------------------------------------------------------------ #
    # Password / deploy mode / paths
    # ------------------------------------------------------------------ #

    def _load_password(self) -> None:
        raw = str(self.plugin.config.get("webui_password", "") or "")
        self.password = decrypt_secret(raw) if raw else ""

    def _set_password(self, plain: str, *, persist: bool = True) -> None:
        self.password = plain or ""
        if persist:
            self.plugin.config["webui_password"] = encrypt_secret(self.password)
            try:
                self.plugin._save_config(self.plugin.config)
            except Exception:
                pass

    def _resolve_deploy_mode(self) -> str:
        requested = str(self.plugin.config.get("deploy_mode", "auto") or "auto").strip().lower()
        if requested not in ("auto", "native", "external"):
            requested = "auto"
        if requested == "external":
            return "external"
        if self.is_installed():
            return "native"
        if requested == "auto":
            external_port = int(self.plugin.config.get("external_port", DEFAULT_SNOWLUMA_PORT) or DEFAULT_SNOWLUMA_PORT)
            external_host = str(self.plugin.config.get("external_host", "127.0.0.1") or "127.0.0.1")
            if port_is_open(external_host, external_port):
                return "external"
        return "native"

    def set_deploy_mode(self, mode: str) -> str:
        self.deploy_mode = mode if mode in ("native", "external") else self._resolve_deploy_mode()
        self._sync_api_endpoint()
        return self.deploy_mode

    def _refresh_paths(self) -> None:
        """Follow a single nested folder if the archive was extracted manually."""
        if not (self.install_dir / "index.mjs").is_file():
            try:
                for child in self.install_dir.iterdir():
                    if child.is_dir() and (child / "index.mjs").is_file():
                        self.install_dir = child
                        break
            except OSError:
                pass
        if not self.webui_port:
            self.webui_port = int(self.plugin.config.get("snowluma_port", DEFAULT_SNOWLUMA_PORT) or DEFAULT_SNOWLUMA_PORT)

    @property
    def config_dir(self) -> Path:
        return self.install_dir / "config"

    @property
    def webui_url(self) -> str:
        if self.deploy_mode == "external":
            host = str(self.plugin.config.get("external_host", "127.0.0.1") or "127.0.0.1")
            port = int(self.plugin.config.get("external_port", DEFAULT_SNOWLUMA_PORT) or DEFAULT_SNOWLUMA_PORT)
            return normalize_base_url(host, port)
        return normalize_base_url(self.webui_host, self.webui_port)

    @property
    def running(self) -> bool:
        if self.deploy_mode == "external":
            return self.auth_ok or port_is_open(
                str(self.plugin.config.get("external_host", "127.0.0.1")),
                int(self.plugin.config.get("external_port", DEFAULT_SNOWLUMA_PORT) or DEFAULT_SNOWLUMA_PORT),
            )
        return self.process is not None and self.process.returncode is None

    def is_installed(self) -> bool:
        try:
            return (self.install_dir / "index.mjs").is_file()
        except OSError:
            return False

    def installed_version(self) -> str:
        if self._installed_version:
            return self._installed_version
        meta = self.read_installed_release()
        self._installed_version = str(meta.get("version") or "")
        return self._installed_version

    def installed_target(self) -> str:
        meta = self.read_installed_release()
        return str(meta.get("target") or platform_asset_key() or "")

    # ------------------------------------------------------------------ #
    # Runtime preparation
    # ------------------------------------------------------------------ #

    def _ensure_password(self) -> str:
        if self.password:
            return self.password
        self.password = generate_strong_password()
        self._set_password(self.password)
        self._log("[auth] 已为本机 SnowLuma 生成随机 WebUI 密码（加密保存）")
        return self.password

    def node_runtime(self, *, force: bool = False) -> Dict[str, Any]:
        """Node runtime probe (cached: it spawns ``node --version``)."""
        now = time.time()
        if not force and self._node_cache is not None and now - self._node_cache[0] < 60:
            return dict(self._node_cache[1])
        runtime = self.resolve_node_runtime()
        runtime["required_range"] = SNOWLUMA_NODE_RANGE
        self._node_cache = (now, runtime)
        return dict(runtime)

    def _node_problem(self, runtime: Dict[str, Any]) -> str:
        if not runtime.get("chosen"):
            return (
                "未找到 Node.js。SnowLuma full 包自带 Node 运行时；若使用 lite 包，"
                f"请安装 Node.js {SNOWLUMA_NODE_RANGE} 并确保 node 在 PATH 中。"
            )
        if not is_supported_node(runtime.get("version")):
            return (
                f"当前 Node.js {node_version_text(runtime.get('version')) or '(unknown)'} 不满足 SnowLuma 要求 "
                f"{SNOWLUMA_NODE_RANGE}。建议改用 full 发行包。"
            )
        return ""

    # ------------------------------------------------------------------ #
    # API session
    # ------------------------------------------------------------------ #

    def _sync_api_endpoint(self) -> None:
        base = self.webui_url
        if self.api is None:
            self.api = SnowLumaAPI(base_url=base, password=self.password)
        else:
            self.api.update(base_url=base, password=self.password)
        if self.password:
            self.api.password = self.password

    async def _rotate_password_if_needed(self, api: SnowLumaAPI) -> bool:
        """Rotate a bootstrap password when SnowLuma demands it."""
        self.must_change_password = True
        old = self.password
        new_password = generate_strong_password()
        try:
            await api.change_password(old, new_password)
        except SnowLumaConsentRequired:
            raise
        except SnowLumaError as e:
            self._record_error(f"自动修改 WebUI 密码失败: {e.message}")
            return False
        self._set_password(new_password)
        self._log("[auth] [ok] 已按 SnowLuma 要求重置初始密码（新密码已加密保存）")
        try:
            await api.login(new_password)
        except SnowLumaError as e:
            self._record_error(f"重置密码后重新登录失败: {e.message}")
            return False
        self.must_change_password = False
        return True

    async def login(self, *, totp: str = "") -> bool:
        async with self._login_lock:
            self._sync_api_endpoint()
            assert self.api is not None
            api = self.api

            if not self.password:
                self._record_error("缺少 SnowLuma WebUI 密码：请在插件配置或页面中填写")
                return False

            try:
                payload = await api.login(self.password, totp=totp)
            except SnowLumaTOTPRequired:
                self._record_error("SnowLuma 开启了二次验证（TOTP），请在插件页面输入 6 位验证码后重试")
                return False
            except SnowLumaAuthError as e:
                self.auth_ok = False
                self._record_error(f"SnowLuma 登录失败: {e.message}")
                return False
            except SnowLumaError as e:
                self.auth_ok = False
                self._record_error(f"SnowLuma 登录异常: {e.message}")
                return False

            if payload.get("mustChangePassword"):
                try:
                    if not await self._rotate_password_if_needed(api):
                        return False
                except SnowLumaConsentRequired:
                    # SnowLuma gates change-password behind the agreement gate;
                    # finish the rotation right after the operator consents.
                    self.must_change_password = True
                    self.consent_required = True
                    self.auth_ok = True
                    self._log(
                        "[auth] 需要重置初始密码，但必须先同意 SnowLuma 协议；同意后会自动完成",
                        "warning",
                    )
                    return True

            self.auth_ok = True
            self.last_error = ""
            self.must_change_password = False
            await self._detect_consent()
            return True

    async def _detect_consent(self) -> None:
        api = self.api
        if api is None:
            return
        try:
            data = await api.agreements()
            self.consent_required = bool(data.get("consentRequired"))
            if self.consent_required:
                self._log(
                    "[auth] SnowLuma 要求先同意用户协议 / 隐私政策：可在插件页面点击「同意协议」，"
                    "或打开 WebUI 手动确认",
                    "warning",
                )
        except SnowLumaConsentRequired:
            self.consent_required = True
        except Exception:
            pass

    async def accept_agreements(self) -> Dict[str, Any]:
        api = await self.ensure_api(quiet=True)
        if api is None:
            return {"ok": False, "message": self.last_error or "SnowLuma 未就绪"}
        try:
            data = await api.agreements()
            version = str(data.get("version") or "")
            if not version:
                return {"ok": False, "message": "未读取到协议版本"}
            await api.record_consent(version)
            self.consent_required = False
            self._log(f"[auth] [ok] 已记录协议同意（version={version}）")
            message = "已同意 SnowLuma 用户协议与隐私政策"
            if self.must_change_password:
                try:
                    if await self._rotate_password_if_needed(api):
                        self.must_change_password = False
                        message += "，并已重置初始 WebUI 密码"
                except Exception as exc:
                    logger.warning(f"[{PLUGIN_NAME}] password rotation after consent failed: {exc}")
            return {"ok": True, "message": message}
        except SnowLumaError as e:
            return {"ok": False, "message": e.message}

    async def reset_password(self) -> Dict[str, Any]:
        """Rotate the managed WebUI password and return the new plaintext once."""
        api = await self.ensure_api(quiet=True)
        if api is None:
            return {"ok": False, "message": self.last_error or "SnowLuma 未就绪，无法重置密码"}
        new_password = generate_strong_password()
        try:
            await api.change_password(self.password, new_password)
        except SnowLumaConsentRequired:
            return {"ok": False, "message": "请先在控制台同意 SnowLuma 用户协议，再重置密码"}
        except SnowLumaError as e:
            return {"ok": False, "message": f"重置失败: {e.message}"}

        self._set_password(new_password)
        try:
            await api.login(new_password)
            self.auth_ok = True
            self.last_error = ""
        except SnowLumaError as e:
            self.auth_ok = False
            return {
                "ok": False,
                "message": f"密码已重置但自动重新登录失败：{e.message}",
                "password": new_password,
            }
        self._log("[auth] [ok] 已重置 SnowLuma WebUI 密码（新密码已加密保存）")
        return {"ok": True, "message": "WebUI 密码已重置", "password": new_password}

    async def ensure_api(self, *, quiet: bool = False, retries: int = 1) -> Optional[SnowLumaAPI]:
        """Return a logged-in API client or ``None`` (never raises)."""
        if self.deploy_mode != "external" and not self.is_running_check():
            self.last_error = "SnowLuma 未运行（未启动或进程已退出）"
            return None
        if self.deploy_mode == "external" and not self.is_installed() and not self.password:
            self.last_error = "external 模式需要填写 SnowLuma WebUI 密码"
        self._sync_api_endpoint()
        api = self.api
        if api is None:
            return None

        for attempt in range(max(1, retries)):
            if self.auth_ok and api.token:
                try:
                    await api.get("/api/status")
                    return api
                except SnowLumaConsentRequired:
                    self.consent_required = True
                    return api
                except Exception:
                    self.auth_ok = False
                    api.token = ""
            if await self.login():
                return self.api
            if attempt + 1 < retries:
                await asyncio.sleep(1.5)
        if not quiet and self.last_error:
            pass
        return None if not self.auth_ok else self.api

    # ------------------------------------------------------------------ #
    # Start / stop
    # ------------------------------------------------------------------ #

    def _spawn_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        port = int(self.plugin.config.get("snowluma_port", DEFAULT_SNOWLUMA_PORT) or DEFAULT_SNOWLUMA_PORT)
        env["SNOWLUMA_WEBUI_PORT"] = str(port)
        env["SNOWLUMA_WEBUI_HOST"] = "127.0.0.1"
        env["SNOWLUMA_HOOK_AUTOLOAD"] = "1" if bool(self.plugin.config.get("auto_hook", True)) else "0"
        webui_json = self.config_dir / "webui.json"
        if not webui_json.exists() and self.password:
            env["SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD"] = self.password
        if bool(self.plugin.config.get("auto_accept_agreements", False)):
            env["SNOWLUMA_ACCEPT_EULA"] = "1"
            env["SNOWLUMA_ACCEPT_PRIVACY"] = "1"
        env.setdefault("NODE_ENV", "production")
        return env

    async def _wait_ready(self, timeout: float = 75.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process is not None and self.process.returncode is not None:
                self._record_error(f"SnowLuma 进程已退出（code={self.process.returncode}）")
                return False
            base = self.webui_url
            host = self.webui_host
            port = self.webui_port
            try:
                if port_is_open(host, port):
                    if self.api is None:
                        self._sync_api_endpoint()
                    api = self.api
                    if api is not None:
                        api.update(base_url=base)
                        if await api.ping():
                            return True
            except Exception:
                pass
            await asyncio.sleep(0.8)
        return False

    async def start(self) -> bool:
        if self.deploy_mode == "external":
            return await self.connect_external()

        if self.running:
            return True
        if not self.is_installed():
            self._record_error("尚未安装 SnowLuma：请先在插件页面选择版本并安装")
            return False

        runtime = self.node_runtime()
        problem = self._node_problem(runtime)
        if problem:
            self._record_error(problem)
            return False

        self._ensure_password()

        node = str(runtime["chosen"])
        cmd = [node, "index.mjs"]
        env = self._spawn_env()
        creationflags = CREATE_NO_WINDOW if IS_WINDOWS else 0
        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.install_dir),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=creationflags,
                start_new_session=not IS_WINDOWS,
            )
        except Exception as e:
            self._record_error(f"启动 SnowLuma 失败: {e}")
            return False

        self._log(
            f"[start] 已启动 SnowLuma（pid={self.process.pid}, node={node_version_text(runtime.get('version')) or node}）"
        )
        self._log_task = asyncio.create_task(self._read_logs())

        ready = await self._wait_ready()
        if not ready:
            if not self.last_error:
                self._record_error("SnowLuma WebUI 未在预期时间内就绪，请查看日志")
            await self.stop()
            return False

        self._log(f"[start] [ok] WebUI 就绪: {self.webui_url}")
        if not await self.login():
            await self.stop()
            return False
        await self.refresh_accounts()
        self._snapshot_ts = 0.0
        self._start_auto_flow()
        return True

    async def connect_external(self) -> bool:
        self._sync_api_endpoint()
        host = str(self.plugin.config.get("external_host", "127.0.0.1") or "127.0.0.1")
        port = int(self.plugin.config.get("external_port", DEFAULT_SNOWLUMA_PORT) or DEFAULT_SNOWLUMA_PORT)
        if not port_is_open(host, port):
            self._record_error(f"external 模式：无法连接 {host}:{port}")
            return False
        if not self.password:
            self._record_error("external 模式：请在插件配置中填写 SnowLuma WebUI 密码")
            return False
        if not await self.login():
            return False
        self._log(f"[start] [ok] 已连接外部 SnowLuma: {self.webui_url}")
        await self.refresh_accounts()
        self._start_auto_flow()
        return True

    async def stop(self) -> None:
        if self._auto_task and not self._auto_task.done():
            self._auto_task.cancel()
            try:
                await asyncio.wait_for(self._auto_task, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            self._auto_task = None

        # Terminate before joining the log reader: the reader is blocked on
        # ``stdout.readline()`` until the pipe closes, so joining it first would
        # deadlock (found by the end-to-end test).
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=6)
            except asyncio.TimeoutError:
                try:
                    if IS_WINDOWS and process.pid:
                        import subprocess

                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=CREATE_NO_WINDOW,
                            timeout=15,
                        )
                    else:
                        process.kill()
                except Exception:
                    pass
            except Exception:
                pass

        if self._log_task and not self._log_task.done():
            self._log_task.cancel()
            try:
                await asyncio.wait_for(self._log_task, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            self._log_task = None

        if self.api is not None:
            self.api.token = ""
            await self.api.close()
        self.auth_ok = False
        self.accounts = []
        self.connections = []
        self.processes = []
        self._auto_synced_uin = ""
        self._auto_sync_failures = 0
        self._auto_hook_done = False
        self._qq_autostarted = False
        self._snapshot_ts = 0.0
        self.webui_port = int(self.plugin.config.get("snowluma_port", DEFAULT_SNOWLUMA_PORT) or DEFAULT_SNOWLUMA_PORT)
        self._log("[stop] SnowLuma 已停止")

    async def restart(self) -> bool:
        await self.stop()
        await asyncio.sleep(1.0)
        return await self.start()

    # ------------------------------------------------------------------ #
    # Log streaming
    # ------------------------------------------------------------------ #

    async def _read_logs(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        stream = process.stdout
        while True:
            try:
                raw = await stream.readline()
            except asyncio.CancelledError:
                # Let cancellation leave this task immediately; ``stop()``
                # terminates the process before joining this reader.
                raise
            except RuntimeError:
                break
            if not raw:
                break
            text = ANSI_RE.sub("", raw.decode("utf-8", errors="ignore")).rstrip()
            if not text:
                continue
            if any(noise in text for noise in LOG_NOISE):
                continue

            self.log_lines.append(text)

            match = LISTEN_RE.search(text)
            if match:
                scheme, port_text = match.group(1), match.group(3)
                port = int(port_text)
                if port != self.webui_port or scheme != self.webui_scheme:
                    self.webui_port = port
                    self.webui_scheme = scheme
                    self._sync_api_endpoint()
                    self._log(f"[start] 检测到 WebUI 端口: {self.webui_url}")

            bump = PORT_BUMP_RE.search(text)
            if bump:
                self.webui_port = int(bump.group(2))
                self._sync_api_endpoint()

            cred = INITIAL_CRED_RE.search(text)
            if cred:
                # SnowLuma rotates an unrotated bootstrap password on every start,
                # so this line is authoritative for the current run. Adopt it when
                # it differs from what we stored (the old value is now invalid).
                fresh = cred.group(2).strip()
                if fresh and fresh != self.password:
                    self._set_password(fresh)
                    self._log("[auth] 已从启动日志捕获 SnowLuma 初始密码，并加密保存")

            version = VERSION_RE.search(text)
            if version:
                self._installed_version = version.group(1)

        code = None
        try:
            if process.returncode is not None:
                code = process.returncode
            else:
                code = await process.wait()
        except Exception:
            pass
        if self.process is process:
            self._log(f"[exit] SnowLuma 进程结束（code={code}）", "warning")
            self.process = None
            self.auth_ok = False

    # ------------------------------------------------------------------ #
    # Account / process snapshots
    # ------------------------------------------------------------------ #

    async def refresh_accounts(self) -> None:
        api = await self.ensure_api(quiet=True)
        if api is None:
            return
        try:
            self.accounts = await api.qq_list()
        except Exception:
            self.accounts = []
        try:
            self.connections = await api.connections()
        except Exception:
            self.connections = []
        try:
            self.processes = await api.processes()
        except Exception:
            self.processes = []

    def primary_uin(self) -> str:
        configured = str(self.plugin.config.get("qq_number", "") or "").strip()
        if configured:
            return configured
        for account in self.accounts:
            uin = str(account.get("uin") or "").strip()
            if uin:
                return uin
        for info in self.processes:
            account = info.get("account")
            if isinstance(account, dict) and account.get("uin"):
                return str(account["uin"])
        return ""

    # ------------------------------------------------------------------ #
    # Auto flow
    # ------------------------------------------------------------------ #

    def _start_auto_flow(self) -> None:
        if self._auto_task and not self._auto_task.done():
            return
        self._auto_task = asyncio.create_task(self._auto_flow())

    async def _auto_flow(self) -> None:
        try:
            await asyncio.sleep(1.5)
            if await self.ensure_api(quiet=True) is None:
                return

            if self.consent_required:
                self._log("[auto] 等待协议同意后再继续自动流程", "warning")
                return

            for round_index in range(12):
                if self.deploy_mode != "external" and not self.is_running_check():
                    self._log("[auto] SnowLuma 进程已退出，自动流程停止")
                    return
                await self.refresh_accounts()
                uin = self.primary_uin()

                need_hook = bool(self.plugin.config.get("auto_hook", True))
                if need_hook and not self._auto_hook_done:
                    hookable = await self.qq.hookable_processes()
                    if hookable:
                        result = await self.qq.hook_all()
                        self._auto_hook_done = True
                        if result.get("loaded"):
                            self._log(f"[auto] {result.get('message')}")

                if not uin and bool(self.plugin.config.get("auto_start_qq", False)) and not self._qq_autostarted:
                    if not running_qq_pids():
                        self._qq_autostarted = True
                        result = await self.qq.launch()
                        self._log(f"[auto] {result.get('message')}")

                if uin:
                    if bool(self.plugin.config.get("auto_sync", True)) and uin != self._auto_synced_uin:
                        ok, message = await self.plugin.syncer.sync(trigger="auto")
                        self._log(f"[auto] 同步 OneBot 配置: {message}", "info" if ok else "warning")
                        if ok:
                            self._auto_synced_uin = uin
                            self._auto_sync_failures = 0
                            self.connections = await self._safe_connections()
                            return
                        self._auto_sync_failures += 1
                        if self._auto_sync_failures >= 3:
                            self._log(
                                "[auto] 连续 3 次自动同步失败，已停止重试；请在面板手动同步并查看错误",
                                "warning",
                            )
                            return
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] auto flow failed: {e}")

    async def _safe_connections(self) -> List[Dict[str, Any]]:
        if self.api is None:
            return []
        try:
            return await self.api.connections()
        except Exception:
            return []

    # ------------------------------------------------------------------ #
    # Install / uninstall / cleanup
    # ------------------------------------------------------------------ #

    async def install(self, version: Optional[str] = None, flavor: Optional[str] = None) -> bool:
        if self.running:
            self._record_error("SnowLuma 正在运行，请先停止后再安装")
            return False
        ok = await self.install_release(version=version, flavor=flavor)
        if ok:
            self._refresh_paths()
            self._installed_version = ""
            self._node_cache = None
            self.deploy_mode = "native"
            if self.is_running_check():
                await self.login()
        return ok

    async def uninstall(self, *, purge: bool = False) -> bool:
        await self.stop()
        try:
            if self.install_dir.exists():
                shutil.rmtree(self.install_dir, ignore_errors=True)
            self._log(f"[uninstall] 已删除安装目录: {self.install_dir}")
        except Exception as e:
            self._record_error(f"删除安装目录失败: {e}")
            return False
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self._installed_version = ""
        if purge:
            try:
                shutil.rmtree(self.persistent_dir / "downloads", ignore_errors=True)
                self._log("[uninstall] 已清理下载缓存")
            except Exception:
                pass
        return True

    async def cleanup(self) -> Dict[str, Any]:
        """Stop the managed process and reap obvious orphans."""
        await self.stop()
        killed = self._kill_orphans()
        for stray in (self.persistent_dir / "downloads").glob(".staging-*"):
            shutil.rmtree(stray, ignore_errors=True)
        self._auto_synced_uin = ""
        return {"ok": True, "killed": killed, "message": "已清理运行状态"}

    def _kill_orphans(self) -> List[int]:
        """Kill node processes that belong to *our* install directory.

        Matching uses the executable path (we always spawn
        ``<install>/node.exe|node``) and, on POSIX, the process working
        directory — the command line only carries ``index.mjs``.
        """
        killed: List[int] = []
        target = str(self.install_dir).lower()
        try:
            if IS_WINDOWS:
                import subprocess

                safe = str(self.install_dir).replace("'", "''")
                script = (
                    f"$t='{safe}'.ToLower(); "
                    "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
                    "Where-Object { ($_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith($t)) -or "
                    "($_.CommandLine -and $_.CommandLine.ToLower().Contains($t)) } | "
                    "Select-Object -ExpandProperty ProcessId"
                )
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True,
                    text=True,
                    timeout=25,
                    creationflags=CREATE_NO_WINDOW,
                )
                for line in (proc.stdout or "").splitlines():
                    line = line.strip()
                    if not line.isdigit():
                        continue
                    pid = int(line)
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW,
                        timeout=15,
                    )
                    killed.append(pid)
            else:
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    pid = int(entry.name)
                    if pid == os.getpid():
                        continue
                    matched = False
                    try:
                        cwd = os.readlink(f"/proc/{pid}/cwd").lower()
                        matched = cwd == target or cwd.startswith(target.rstrip("/") + "/")
                    except Exception:
                        pass
                    if not matched:
                        try:
                            raw = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                                "utf-8", errors="ignore"
                            )
                            matched = target in raw.lower()
                        except Exception:
                            pass
                    if not matched:
                        continue
                    try:
                        os.kill(pid, 15)
                        killed.append(pid)
                    except Exception:
                        continue
            if killed:
                self._log(f"[cleanup] 已清理残留 node 进程: {killed}")
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] orphan cleanup failed: {e}")
        return killed

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    async def status(self) -> Dict[str, Any]:
        now = time.time()
        if self._snapshot and now - self._snapshot_ts < 1.2:
            return self._snapshot

        if self.is_running_check() and self.auth_ok and now - getattr(self, "_last_refresh_ts", 0.0) > 2.0:
            await self.refresh_accounts()
            self._last_refresh_ts = time.time()

        runtime = self.node_runtime()
        qq_info = self.qq.detect()
        release_meta = self.read_installed_release()
        accounts = self.accounts
        connections = self.connections
        processes = [info for info in self.processes if is_qq_process(info)]

        data = {
            "platform": "Windows" if IS_WINDOWS else ("Linux" if IS_LINUX else os.name),
            "platform_asset": platform_asset_key(),
            "is_windows": IS_WINDOWS,
            "is_linux": IS_LINUX,
            "persistent_dir": str(self.persistent_dir),
            "install_dir": str(self.install_dir),
            "config_dir": str(self.config_dir),
            "deploy_mode": self.deploy_mode,
            "external": self.deploy_mode == "external",
            "installed": self.is_installed(),
            "installed_version": str(release_meta.get("version") or self.installed_version()),
            "installed_target": str(release_meta.get("target") or ""),
            "release_flavor": (
                ("full" if runtime.get("bundled") else "lite")
                if self.is_installed()
                else str(self.plugin.config.get("release_flavor", "full") or "full")
            ),
            "running": self.is_running_check(),
            "pid": self.process.pid if self.process is not None and self.process.returncode is None else None,
            "webui_url": self.webui_url,
            "webui_port": int(self.plugin.config.get("external_port", self.webui_port))
            if self.deploy_mode == "external"
            else self.webui_port,
            "auth_ok": self.auth_ok,
            "consent_required": self.consent_required,
            "must_change_password": self.must_change_password,
            "webui_password_masked": mask_secret(self.password),
            "password_managed": bool(self.password),
            "node": {
                "chosen": runtime.get("chosen"),
                "bundled": bool(runtime.get("bundled")),
                "system": runtime.get("system"),
                "version": node_version_text(runtime.get("version")),
                "supported": is_supported_node(runtime.get("version")),
                "required_range": SNOWLUMA_NODE_RANGE,
            },
            "accounts": accounts,
            "connections": connections,
            "qq_processes": processes,
            "qq": qq_info,
            "download": dict(self.download_state),
            "log_count": len(self.log_lines),
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "config": {**self.plugin.config, "webui_password": mask_secret(self.password)},
        }
        self._snapshot = data
        self._snapshot_ts = now
        return data

    def is_running_check(self) -> bool:
        if self.deploy_mode == "external":
            try:
                host = str(self.plugin.config.get("external_host", "127.0.0.1") or "127.0.0.1")
                port = int(self.plugin.config.get("external_port", DEFAULT_SNOWLUMA_PORT) or DEFAULT_SNOWLUMA_PORT)
                return port_is_open(host, port, timeout=0.3)
            except Exception:
                return False
        return self.process is not None and self.process.returncode is None

    # ------------------------------------------------------------------ #
    # Misc actions used by the panel
    # ------------------------------------------------------------------ #

    def reveal_password(self) -> str:
        if not self.auth_ok:
            # Allow reading back a managed password for WebUI login even when the
            # session is down; this endpoint is authenticated by AstrBot itself.
            pass
        return self.password

    async def load_releases(self, force: bool = False) -> List[Dict[str, Any]]:
        return await self.list_releases(force=force)

    async def fetch_server_logs(self, limit: int = 200) -> List[Dict[str, Any]]:
        api = await self.ensure_api(quiet=True)
        if api is None:
            return []
        try:
            return await api.logs(limit=limit)
        except Exception:
            return []

    async def process_action(self, pid: int, action: str) -> Dict[str, Any]:
        api = await self.ensure_api(quiet=True)
        if api is None:
            return {"ok": False, "message": self.last_error or "SnowLuma 未就绪"}
        try:
            result = await api.process_action(pid, action)
            await self.refresh_accounts()
            return {
                "ok": bool(result.get("success")),
                "message": str(result.get("message") or ""),
                "process": result.get("process"),
            }
        except Exception as e:
            return {"ok": False, "message": shorten(str(e), 200)}
