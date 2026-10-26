"""Snowluma Go — AstrBot plugin entry point.

The plugin is a small operator console around SnowLuma:

* install / update SnowLuma releases from GitHub (mirrors supported),
* launch it as a managed child process (bundled or system Node),
* talk to its official WebUI REST API (login, accounts, hook, OneBot config),
* upsert an AstrBot reverse-WS adapter and let SnowLuma hot-reload it,
* expose everything through a plugin page plus a ``/snowluma`` admin command.
"""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

try:  # AstrBot exposes this from 4.x; keep the plugin importable otherwise.
    from astrbot.api import AstrBotConfig
except ImportError:  # pragma: no cover
    AstrBotConfig = dict  # type: ignore

from .config_syncer import ConfigSyncer
from .snowluma_manager import SnowLumaManager
from .utils import (
    CONFIG_VERSION,
    DEFAULT_CONFIG,
    IS_LINUX,
    IS_WINDOWS,
    PLUGIN_NAME,
    decrypt_secret,
    encrypt_secret,
    platform_asset_key,
    read_json,
    resolve_persistent_dir,
    shorten,
    write_json,
)  # noqa: F401  (read_json/write_json are kept for future migrations)

PLUGIN_CODE_VERSION = "1.0.0"


def _coerce(key: str, value: Any, default: Any) -> Any:
    if key == "webui_password":
        return str(value or "")
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "开启")
        return bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if value is None:
        return default
    return str(value)


class SnowLumaGoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):  # type: ignore[assignment]
        super().__init__(context)
        self.astrbot_config = config
        self.persistent_dir = resolve_persistent_dir(context)
        self.plugin_path = Path(context.plugin_dir) if hasattr(context, "plugin_dir") else Path(__file__).parent
        self.config_path = self.persistent_dir / "config.json"

        logger.info(f"[{PLUGIN_NAME}] code version: {PLUGIN_CODE_VERSION}")

        self.config = self._load_config()
        self.syncer = ConfigSyncer(self)
        self.manager = SnowLumaManager(self)
        self.manager.deploy_mode = self.manager._resolve_deploy_mode()
        self._auto_start_task: Optional[asyncio.Task] = None

        self._register_routes(context)

        logger.info(
            f"[{PLUGIN_NAME}] install_dir={self.manager.install_dir} "
            f"deploy_mode={self.manager.deploy_mode} installed={self.manager.is_installed()}"
        )

        if bool(self.config.get("auto_start", True)):
            self._auto_start_task = asyncio.ensure_future(self._auto_start_wrapper())

    # ------------------------------------------------------------------ #
    # Route registration
    # ------------------------------------------------------------------ #

    def _register_routes(self, context: Context) -> None:
        routes = [
            ("status", self.api_status, ["GET"], "SnowLuma 运行状态"),
            ("logs", self.api_logs, ["GET"], "插件 / 进程日志"),
            ("server-logs", self.api_server_logs, ["GET"], "SnowLuma 服务端日志"),
            ("sync", self.api_sync, ["POST"], "同步 OneBot 配置"),
            ("disconnect", self.api_disconnect, ["POST"], "移除 AstrBot 接入"),
            ("start", self.api_start, ["POST"], "启动 SnowLuma"),
            ("stop", self.api_stop, ["POST"], "停止 SnowLuma"),
            ("restart", self.api_restart, ["POST"], "重启 SnowLuma"),
            ("install", self.api_install, ["POST"], "安装 / 更新 SnowLuma"),
            ("uninstall", self.api_uninstall, ["POST"], "卸载 SnowLuma"),
            ("cleanup", self.api_cleanup, ["POST"], "清理残留进程"),
            ("releases", self.api_releases, ["GET"], "可用版本列表"),
            ("config", self.api_get_config, ["GET"], "读取插件配置"),
            ("config/save", self.api_save_config, ["POST"], "保存插件配置"),
            ("open-webui", self.api_open_webui, ["POST"], "打开 SnowLuma WebUI"),
            ("accept-agreements", self.api_accept_agreements, ["POST"], "记录协议同意"),
            ("password/reveal", self.api_reveal_password, ["GET"], "读取受管 WebUI 密码"),
            ("qq/launch", self.api_qq_launch, ["POST"], "启动 QQ"),
            ("qq/stop", self.api_qq_stop, ["POST"], "关闭本插件启动的 QQ"),
            ("hook/all", self.api_hook_all, ["POST"], "挂载全部 QQ 进程"),
            ("hook/load", self.api_hook_load, ["POST"], "挂载指定 QQ 进程"),
            ("hook/unload", self.api_hook_unload, ["POST"], "卸载指定 QQ 进程"),
            ("onboarding/done", self.api_onboarding_done, ["POST"], "完成引导"),
            ("update/check", self.api_update_check, ["GET"], "检查 SnowLuma 更新"),
        ]
        for path, handler, methods, desc in routes:
            try:
                context.register_web_api(f"/{PLUGIN_NAME}/{path}", handler, methods, desc)
            except TypeError:
                try:
                    context.register_web_api(f"/{PLUGIN_NAME}/{path}", handler, methods)
                except TypeError as exc:
                    logger.error(f"[{PLUGIN_NAME}] register_web_api failed {path}: {exc}")

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    @filter.command("snowluma")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def snowluma_cmd(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._status_text())
        except Exception as exc:  # pragma: no cover
            logger.error(f"[{PLUGIN_NAME}] status command failed: {exc}", exc_info=True)
            yield event.plain_result(f"SnowLuma 状态读取失败: {exc}")

    async def _status_text(self) -> str:
        snapshot = await self.manager.status()
        syncer = self.syncer.status_dict()
        lines = [
            "SnowLuma Go",
            "-" * 24,
            f"运行方式: {snapshot['deploy_mode']}",
            f"SnowLuma: {'运行中' if snapshot['running'] else '未运行'}"
            f"（{snapshot.get('installed_version') or '未安装'}）",
            f"WebUI: {snapshot.get('webui_url')}",
            f"登录状态: {'已登录' if snapshot.get('auth_ok') else '未登录'}"
            + ("（待同意协议）" if snapshot.get("consent_required") else ""),
            f"账号: {', '.join(str(a.get('uin')) for a in snapshot.get('accounts') or []) or '(无)'}",
            f"QQ 进程: {len(snapshot.get('qq_processes') or [])} 个可管理",
            f"AstrBot 接入: {syncer.get('ws_url') or '(未同步)'}",
            f"最近同步: {syncer.get('last_sync_msg')}",
        ]
        if snapshot.get("last_error"):
            lines.append(f"最近错误: {snapshot['last_error']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Auto start
    # ------------------------------------------------------------------ #

    async def _auto_start_wrapper(self) -> None:
        try:
            await asyncio.sleep(3)
            if self.manager.deploy_mode == "external" and not self.manager.password:
                logger.warning(f"[{PLUGIN_NAME}] external 模式缺少 WebUI 密码，跳过自动连接")
                return
            if self.manager.deploy_mode == "native" and not self.manager.is_installed():
                logger.warning(f"[{PLUGIN_NAME}] 尚未安装 SnowLuma，跳过自动启动（可在插件页面安装）")
                return
            ok = await self.manager.start()
            if ok:
                logger.info(f"[{PLUGIN_NAME}] auto_start: SnowLuma ready")
            else:
                logger.warning(f"[{PLUGIN_NAME}] auto_start failed: {self.manager.last_error}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] auto_start exception: {exc}", exc_info=True)

    # ------------------------------------------------------------------ #
    # Status / logs
    # ------------------------------------------------------------------ #

    async def api_status(self):
        try:
            snapshot = await self.manager.status()
            try:
                self.syncer.refresh()
            except Exception:
                pass
            data: Dict[str, Any] = dict(snapshot)
            data["syncer"] = self.syncer.status_dict()
            data["scan"] = self.syncer.scans()
            data["platform_asset"] = platform_asset_key()
            data["plugin"] = {
                "name": PLUGIN_NAME,
                "code_version": PLUGIN_CODE_VERSION,
                "config_version": CONFIG_VERSION,
                "astrbot_platform": "Windows" if IS_WINDOWS else ("Linux" if IS_LINUX else "other"),
            }
            return json_response(data)
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] status failed: {exc}", exc_info=True)
            return error_response(f"状态读取失败: {shorten(str(exc), 200)}")

    async def api_logs(self):
        try:
            since = request.query.get("since", 0, type=int) or 0
            return json_response(self.manager.logs_since(since))
        except Exception as exc:
            return error_response(f"读取日志失败: {shorten(str(exc), 200)}")

    async def api_server_logs(self):
        try:
            limit = request.query.get("limit", 200, type=int) or 200
            entries = await self.manager.fetch_server_logs(limit=limit)
            return json_response({"ok": True, "list": entries})
        except Exception as exc:
            return error_response(f"读取 SnowLuma 日志失败: {shorten(str(exc), 200)}")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def api_start(self):
        ok = await self.manager.start()
        return json_response(
            {"ok": ok, "message": "SnowLuma 已启动" if ok else (self.manager.last_error or "启动失败")}
        )

    async def api_stop(self):
        await self.manager.stop()
        return json_response({"ok": True, "message": "SnowLuma 已停止"})

    async def api_restart(self):
        ok = await self.manager.restart()
        return json_response(
            {"ok": ok, "message": "SnowLuma 已重启" if ok else (self.manager.last_error or "重启失败")}
        )

    async def api_cleanup(self):
        try:
            result = await self.manager.cleanup()
            return json_response(result)
        except Exception as exc:
            return error_response(f"清理失败: {shorten(str(exc), 200)}")

    # ------------------------------------------------------------------ #
    # Install / uninstall / releases
    # ------------------------------------------------------------------ #

    async def api_install(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        version = str(payload.get("version") or "").strip() or None
        flavor = str(payload.get("flavor") or "").strip().lower() or None
        if flavor not in (None, "full", "lite"):
            flavor = None
        ok = await self.manager.install(version=version, flavor=flavor)
        return json_response(
            {
                "ok": ok,
                "message": "安装完成" if ok else (self.manager.last_error or "安装失败"),
            }
        )

    async def api_uninstall(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        purge = bool(payload.get("purge", False))
        ok = await self.manager.uninstall(purge=purge)
        return json_response(
            {"ok": ok, "message": "已卸载 SnowLuma" if ok else (self.manager.last_error or "卸载失败")}
        )

    async def api_releases(self):
        try:
            force = bool(request.query.get("force", 0, type=int))
            releases = await self.manager.load_releases(force=force)
            return json_response(
                {
                    "ok": True,
                    "releases": releases,
                    "platform": platform_asset_key(),
                    "current": self.manager.installed_version(),
                }
            )
        except Exception as exc:
            return error_response(f"获取版本列表失败: {shorten(str(exc), 200)}")

    # ------------------------------------------------------------------ #
    # Sync
    # ------------------------------------------------------------------ #

    async def api_sync(self):
        try:
            ok, message = await self.syncer.sync(trigger="manual")
            return json_response({"ok": ok, "message": message, "syncer": self.syncer.status_dict()})
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] sync failed: {exc}", exc_info=True)
            return error_response(f"同步异常: {shorten(str(exc), 200)}")

    async def api_disconnect(self):
        try:
            ok, message = await self.syncer.disconnect()
            return json_response({"ok": ok, "message": message, "syncer": self.syncer.status_dict()})
        except Exception as exc:
            return error_response(f"断开失败: {shorten(str(exc), 200)}")

    # ------------------------------------------------------------------ #
    # QQ / hook
    # ------------------------------------------------------------------ #

    async def api_qq_launch(self):
        try:
            result = await self.manager.qq.launch()
            return json_response(result)
        except Exception as exc:
            return error_response(f"启动 QQ 失败: {shorten(str(exc), 200)}")

    async def api_qq_stop(self):
        try:
            return json_response(self.manager.qq.stop_launched())
        except Exception as exc:
            return error_response(f"关闭 QQ 失败: {shorten(str(exc), 200)}")

    async def api_hook_all(self):
        try:
            result = await self.manager.qq.hook_all()
            await self.manager.refresh_accounts()
            return json_response(result)
        except Exception as exc:
            return error_response(f"挂载失败: {shorten(str(exc), 200)}")

    async def _hook_one(self, action: str):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        try:
            pid = int((payload or {}).get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0:
            return error_response("缺少有效的 pid")
        if action == "unload":
            result = await self.manager.qq.unhook(pid)
        else:
            result = await self.manager.process_action(pid, action)
        await self.manager.refresh_accounts()
        return json_response(result)

    async def api_hook_load(self):
        return await self._hook_one("load")

    async def api_hook_unload(self):
        return await self._hook_one("unload")

    # ------------------------------------------------------------------ #
    # Agreements / WebUI
    # ------------------------------------------------------------------ #

    async def api_accept_agreements(self):
        try:
            result = await self.manager.accept_agreements()
            return json_response(result)
        except Exception as exc:
            return error_response(f"记录协议同意失败: {shorten(str(exc), 200)}")

    async def api_open_webui(self):
        url = self.manager.webui_url
        opened = False
        try:
            opened = bool(webbrowser.open(url))
        except Exception:
            opened = False
        return json_response({"ok": True, "url": url, "opened": opened})

    async def api_reveal_password(self):
        return json_response({"ok": True, "password": self.manager.password})

    async def api_update_check(self):
        try:
            api = await self.manager.ensure_api(quiet=True)
            if api is None:
                return error_response(self.manager.last_error or "SnowLuma 未就绪")
            info = await api.update_check(force=True)
            return json_response({"ok": True, "info": info, "current": self.manager.installed_version()})
        except Exception as exc:
            return error_response(f"检查更新失败: {shorten(str(exc), 200)}")

    # ------------------------------------------------------------------ #
    # Plugin config
    # ------------------------------------------------------------------ #

    async def api_get_config(self):
        data = dict(self.config)
        data["webui_password"] = ""
        return json_response(data)

    async def api_save_config(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        for key, default in DEFAULT_CONFIG.items():
            if key in ("webui_password", "onboarding_done"):
                continue
            if key in payload and payload[key] is not None:
                self.config[key] = _coerce(key, payload[key], default)
                self._overrides.add(key)

        password = payload.get("webui_password")
        if isinstance(password, str) and password.strip():
            self.config["webui_password"] = encrypt_secret(password.strip())
            self._overrides.add("webui_password")
            self.manager._load_password()
            self.manager.auth_ok = False
            if self.manager.api is not None:
                self.manager.api.token = ""

        self.manager._installed_version = ""
        self._save_config(self.config)
        self.manager._snapshot_ts = 0.0
        self.manager.deploy_mode = self.manager._resolve_deploy_mode()
        if self.manager.is_running_check() or self.manager.deploy_mode == "external":
            await self.manager.login()
        snapshot = await self.manager.status()
        try:
            self.syncer.refresh()
        except Exception:
            pass
        data = dict(snapshot)
        data["syncer"] = self.syncer.status_dict()
        data["scan"] = self.syncer.scans()
        return json_response({"ok": True, "message": "配置已保存", "status": data})

    async def api_onboarding_done(self):
        self.config["onboarding_done"] = True
        self._save_config(self.config)
        return json_response({"ok": True})

    # ------------------------------------------------------------------ #
    # Config persistence
    # ------------------------------------------------------------------ #

    def _load_config(self) -> Dict[str, Any]:
        config = dict(DEFAULT_CONFIG)
        persisted: Dict[str, Any] = {}
        if self.config_path.is_file():
            loaded = read_json(self.config_path, {})
            if isinstance(loaded, dict):
                persisted = loaded

        raw_overrides = persisted.get("__overrides__")
        self._overrides = {str(k) for k in raw_overrides} if isinstance(raw_overrides, list) else set()

        object_values: Dict[str, Any] = {}
        if self.astrbot_config is not None:
            try:
                for key in self.astrbot_config.keys():
                    try:
                        object_values[key] = self.astrbot_config[key]
                    except Exception:
                        continue
            except Exception:
                object_values = {}

        # Precedence: keys explicitly saved from the plugin panel override the
        # AstrBot plugin-config page; everything else follows AstrBot's schema.
        # That keeps both entry points usable instead of silently freezing the
        # first-seen values forever.
        for key, default in DEFAULT_CONFIG.items():
            if key == "webui_password":
                continue
            if key in self._overrides and persisted.get(key) is not None:
                config[key] = _coerce(key, persisted[key], default)
            elif object_values.get(key) is not None:
                config[key] = _coerce(key, object_values[key], default)
            else:
                config[key] = default

        stored = str(
            persisted.get("webui_password")
            or object_values.get("webui_password")
            or ""
        )
        plain = decrypt_secret(stored) if stored else ""
        config["webui_password"] = encrypt_secret(plain) if plain else ""

        self._persist(config)
        return config

    def _persist(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config if config is not None else self.config
        data: Dict[str, Any] = {
            "__config_version__": CONFIG_VERSION,
            "__overrides__": sorted(self._overrides),
        }
        for key in self._overrides:
            if key in DEFAULT_CONFIG and key != "webui_password":
                data[key] = cfg.get(key, DEFAULT_CONFIG[key])
        if cfg.get("webui_password"):
            data["webui_password"] = cfg["webui_password"]
        if cfg.get("onboarding_done"):
            data["onboarding_done"] = True
        write_json(self.config_path, data)

    def _save_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        if config is not None:
            self.config = config
        self._persist()

    @property
    def plugin_dir(self) -> Path:
        return self.plugin_path

    # ------------------------------------------------------------------ #
    # Teardown
    # ------------------------------------------------------------------ #

    async def terminate(self):
        task = self._auto_start_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self.manager.stop()
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] stop on terminate failed: {exc}")
            try:
                await self.manager.cleanup()
            except Exception:
                pass
