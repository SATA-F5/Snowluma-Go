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
import base64
import os
import shutil
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

try:  # sudo password transport (also used by the Fernet helpers in utils)
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    from cryptography.hazmat.primitives.asymmetric import rsa as asym_rsa
except ImportError:  # pragma: no cover
    asym_padding = None  # type: ignore[assignment]
    asym_rsa = None  # type: ignore[assignment]

try:  # AstrBot exposes this from 4.x; keep the plugin importable otherwise.
    from astrbot.api import AstrBotConfig
except ImportError:  # pragma: no cover
    AstrBotConfig = dict  # type: ignore

from .config_syncer import ConfigSyncer
from .linux_tools import fix_qq_permissions, install_linux_deps, missing_linux_deps
from .snowluma_manager import SnowLumaManager
from .utils import (
    CONFIG_VERSION,
    DEFAULT_CONFIG,
    GITHUB_MIRRORS,
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

PLUGIN_CODE_VERSION = "1.0.2"

# Bump together with ONBOARD_VERSION in bridge.js so upgraded users get the
# refreshed guide exactly once.
ONBOARD_CONFIG_VERSION = 3

# Whitelisted onboarding images (name -> mime). Served via /guide/asset so the
# page never depends on static asset routing or platform path separators.
GUIDE_ASSETS = {
    "astbot-create-bot.png": "image/png",
}

# Stored encrypted at rest and never returned by the config API.
SECRET_KEYS = ("webui_password", "vnc_password")


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
        self.manager.deploy_mode = self.manager._resolve_deploy_mode(probe=False)
        self._auto_start_task: Optional[asyncio.Task] = None
        self._post_install_task: Optional[asyncio.Task] = None
        self._linux_deps_task: Optional[asyncio.Task] = None
        # Ephemeral key used to receive one-shot sudo passwords from the WebUI.
        # Nothing is persisted; a restart invalidates previously encrypted blobs.
        self._sudo_key = (
            asym_rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if asym_rsa is not None
            else None
        )

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
            ("password/reset", self.api_reset_password, ["POST"], "重置 WebUI 密码"),
            ("qq/launch", self.api_qq_launch, ["POST"], "启动 QQ"),
            ("qq/stop", self.api_qq_stop, ["POST"], "关闭本插件启动的 QQ"),
            ("qq/cleanup", self.api_qq_cleanup, ["POST"], "清理当前用户的 QQ 进程"),
            ("qq/restart", self.api_qq_restart, ["POST"], "重启 QQ 到虚拟显示"),
            ("qq/login-qr", self.api_qq_login_qr, ["POST"], "截取 QQ 登录二维码"),
            ("qq/repair-ui", self.api_qq_repair_ui, ["POST"], "修复 QQ 登录界面"),
            ("linux/fix-permissions", self.api_linux_fix_permissions, ["POST"], "修复 QQ 权限"),
            ("linux/install-deps", self.api_linux_install_deps, ["POST"], "安装 Linux 依赖"),
            ("hook/all", self.api_hook_all, ["POST"], "挂载全部 QQ 进程"),
            ("hook/load", self.api_hook_load, ["POST"], "挂载指定 QQ 进程"),
            ("hook/unload", self.api_hook_unload, ["POST"], "卸载指定 QQ 进程"),
            ("onboarding/done", self.api_onboarding_done, ["POST"], "完成引导"),
            ("download/options", self.api_save_download_options, ["POST"], "保存下载设置"),
            ("guide/asset", self.api_guide_asset, ["GET"], "读取引导图片"),
            ("sudo/key", self.api_sudo_key, ["GET"], "获取 sudo 加密公钥"),
            ("deps/status", self.api_deps_status, ["GET"], "检查 Linux 依赖"),
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

    # ------------------------------------------------------------------ #
    # One-shot sudo password (RSA-encrypted by the WebUI, never persisted)
    # ------------------------------------------------------------------ #

    async def api_sudo_key(self):
        if self._sudo_key is None:
            return error_response("缺少 cryptography 依赖，无法加密 sudo 密码")
        numbers = self._sudo_key.public_key().public_numbers()
        n_hex = format(numbers.n, "x")
        if len(n_hex) % 2:
            n_hex = "0" + n_hex
        return json_response(
            {
                "ok": True,
                "n": n_hex,
                "e": format(numbers.e, "x"),
                "size": (numbers.n.bit_length() + 7) // 8,
            }
        )

    async def api_deps_status(self):
        if not IS_LINUX:
            return json_response({"ok": True, "missing": [], "linux": False})
        return json_response({"ok": True, "missing": missing_linux_deps(), "linux": True})

    def _sudo_from_payload(self, payload: Any) -> tuple:
        """Decrypt ``sudo_password_enc`` from a request body.

        Returns ``(password, error)``; an empty password simply means the
        caller did not provide one.
        """
        if not isinstance(payload, dict):
            return "", ""
        encoded = str(payload.get("sudo_password_enc") or "").strip()
        if not encoded:
            return "", ""
        if self._sudo_key is None or asym_padding is None:
            return "", "缺少 cryptography 依赖，无法解密 sudo 密码"
        try:
            raw = base64.b64decode(encoded, validate=True)
            plain = self._sudo_key.decrypt(raw, asym_padding.PKCS1v15())
            return plain.decode("utf-8").strip(), ""
        except Exception:
            return "", "sudo 密码解密失败，请重试"

    async def _read_sudo_payload(self) -> tuple:
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return payload, *self._sudo_from_payload(payload)

    async def _post_start_qq_repair(self) -> None:
        """Appended to the start flow: fix NapCat injection / QQ config perms.

        NapCat rewrites QQ's Electron entry to ``loadNapCat.js``, and a
        root-owned ``~/.config/QQ`` makes QQ die with EACCES before mapping a
        window. Both are detected and repaired when SnowLuma starts.
        """
        try:
            result = await self.manager.qq.prepare_qq_environment("")
            if result.get("ok") and not result.get("skipped"):
                logger.info(f"[{PLUGIN_NAME}] QQ 启动环境已自动修复: {result.get('message')}")
            elif result.get("need_sudo"):
                logger.warning(
                    f"[{PLUGIN_NAME}] QQ 启动环境需要 sudo 密码修复，"
                    "请在 WebUI 点击启动 / 启动 QQ 并按提示输入"
                )
            elif not result.get("ok"):
                logger.warning(f"[{PLUGIN_NAME}] QQ 启动环境修复失败: {result.get('message')}")
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] QQ 启动环境检测失败: {exc}")

    async def _auto_start_wrapper(self) -> None:
        try:
            await asyncio.sleep(3)
            await self.manager.refresh_deploy_mode()
            if self.manager.deploy_mode == "external" and not self.manager.password:
                logger.warning(f"[{PLUGIN_NAME}] external 模式缺少 WebUI 密码，跳过自动连接")
                return
            if self.manager.deploy_mode == "native" and not self.manager.is_installed():
                logger.warning(f"[{PLUGIN_NAME}] 尚未安装 SnowLuma，跳过自动启动（可在插件页面安装）")
                return
            ok = await self.manager.start()
            if ok:
                logger.info(f"[{PLUGIN_NAME}] auto_start: SnowLuma ready")
                await self._post_start_qq_repair()
            else:
                logger.warning(f"[{PLUGIN_NAME}] auto_start failed: {self.manager.last_error}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] auto_start exception: {exc}", exc_info=True)

    async def _start_after_install(self) -> None:
        """Start once the download/install task has fully released its lock."""
        try:
            await asyncio.sleep(0.8)
            if await self.manager.is_running_check_async():
                return
            logger.info(f"[{PLUGIN_NAME}] install finished, starting SnowLuma automatically")
            if await self.manager.start():
                logger.info(f"[{PLUGIN_NAME}] post-install auto_start: SnowLuma ready")
                await self._post_start_qq_repair()
            else:
                logger.warning(
                    f"[{PLUGIN_NAME}] post-install auto_start failed: {self.manager.last_error}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] post-install auto_start exception: {exc}", exc_info=True)

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
        _payload, password, sudo_err = await self._read_sudo_payload()
        if sudo_err:
            return error_response(sudo_err)
        if IS_LINUX:
            pre = await self.manager.qq.auto_repair_napcat_entry(password)
            if pre.get("need_sudo"):
                return json_response(
                    {"ok": False, "need_sudo": True, "message": pre.get("message")}
                )
        ok = await self.manager.start()
        if ok:
            await self._post_start_qq_repair()
        return json_response(
            {"ok": ok, "message": "SnowLuma 已启动" if ok else (self.manager.last_error or "启动失败")}
        )

    async def api_stop(self):
        await self.manager.stop()
        return json_response({"ok": True, "message": "SnowLuma 已停止"})

    async def api_restart(self):
        _payload, password, sudo_err = await self._read_sudo_payload()
        if sudo_err:
            return error_response(sudo_err)
        if IS_LINUX:
            pre = await self.manager.qq.auto_repair_napcat_entry(password)
            if pre.get("need_sudo"):
                return json_response(
                    {"ok": False, "need_sudo": True, "message": pre.get("message")}
                )
        ok = await self.manager.restart()
        if ok:
            await self._post_start_qq_repair()
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
        password, sudo_err = self._sudo_from_payload(payload)
        if sudo_err:
            return error_response(sudo_err)
        if IS_LINUX and missing_linux_deps():
            if not password:
                return json_response(
                    {
                        "ok": False,
                        "need_sudo": True,
                        "message": "缺少 Linux 依赖，需要 sudo 密码后才能安装",
                    }
                )
        ok = await self.manager.install(
            version=version, flavor=flavor, sudo_password=password
        )
        auto_starting = False
        if ok and bool(self.config.get("auto_start_after_install", True)):
            self._post_install_task = asyncio.ensure_future(self._start_after_install())
            auto_starting = True
        message = self.manager.last_error or "安装失败"
        if ok:
            message = "安装完成，正在自动启动 SnowLuma" if auto_starting else "安装完成"
        return json_response({"ok": ok, "message": message, "auto_starting": auto_starting})

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
            _payload, password, sudo_err = await self._read_sudo_payload()
            if sudo_err:
                return error_response(sudo_err)
            result = await self.manager.qq.launch(password)
            return json_response(result)
        except Exception as exc:
            return error_response(f"启动 QQ 失败: {shorten(str(exc), 200)}")

    async def api_qq_stop(self):
        try:
            return json_response(self.manager.qq.stop_launched())
        except Exception as exc:
            return error_response(f"关闭 QQ 失败: {shorten(str(exc), 200)}")

    async def api_qq_cleanup(self):
        try:
            _payload, password, sudo_err = await self._read_sudo_payload()
            if sudo_err:
                return error_response(sudo_err)
            result = await self.manager.qq.cleanup_all(password)
            try:
                await self.manager.refresh_accounts()
            except Exception:
                pass
            return json_response(result)
        except Exception as exc:
            return error_response(f"清理 QQ 进程失败: {shorten(str(exc), 200)}")

    async def api_qq_restart(self):
        try:
            _payload, password, sudo_err = await self._read_sudo_payload()
            if sudo_err:
                return error_response(sudo_err)
            result = await self.manager.qq.restart_qq(password)
            return json_response(result)
        except Exception as exc:
            return error_response(f"重启 QQ 失败: {shorten(str(exc), 200)}")

    async def api_qq_login_qr(self):
        try:
            _payload, password, sudo_err = await self._read_sudo_payload()
            if sudo_err:
                return error_response(sudo_err)
            return json_response(await self.manager.qq.login_qr(password))
        except Exception as exc:
            return error_response(f"截取 QQ 登录二维码失败: {shorten(str(exc), 200)}")

    async def api_qq_repair_ui(self):
        try:
            _payload, password, sudo_err = await self._read_sudo_payload()
            if sudo_err:
                return error_response(sudo_err)
            result = await self.manager.qq.repair_qq_ui(password)
            if result.get("ok"):
                try:
                    await self.manager.refresh_accounts()
                except Exception:
                    pass
            return json_response(result)
        except Exception as exc:
            return error_response(f"修复 QQ 登录界面失败: {shorten(str(exc), 200)}")

    async def api_linux_fix_permissions(self):
        try:
            _payload, password, sudo_err = await self._read_sudo_payload()
            if sudo_err:
                return error_response(sudo_err)
            return json_response(await fix_qq_permissions(self, password))
        except Exception as exc:
            return error_response(f"修复权限失败: {shorten(str(exc), 200)}")

    async def api_linux_install_deps(self):
        if self._linux_deps_task and not self._linux_deps_task.done():
            return json_response({"ok": True, "message": "Linux 依赖正在安装中，请查看插件日志"})
        _payload, password, sudo_err = await self._read_sudo_payload()
        if sudo_err:
            return error_response(sudo_err)
        if not password:
            return json_response(
                {"ok": False, "need_sudo": True, "message": "该操作需要 sudo 密码"}
            )
        self._linux_deps_task = asyncio.ensure_future(self._run_linux_deps(password))
        return json_response({"ok": True, "message": "已开始安装 Linux 依赖，请稍后查看插件日志"})

    async def _run_linux_deps(self, password: str) -> None:
        try:
            result = await install_linux_deps(self, password)
            if result.get("ok"):
                logger.info(f"[{PLUGIN_NAME}] Linux 依赖安装完成，正在启动虚拟显示 / VNC")
                try:
                    display = await asyncio.to_thread(self.manager.qq.ensure_display)
                    vnc = await self.manager.qq.vnc_info()
                    vnc_state = "running" if vnc.get("running") else "not running"
                    vnc_scope = "仅本机（设置 VNC 密码后可局域网访问）" if vnc.get("local_only") else "局域网"
                    logger.info(
                        f"[{PLUGIN_NAME}] Linux display={display or '-'} "
                        f"vnc={vnc_state} port={vnc.get('port')} {vnc_scope}"
                    )
                    if not vnc.get("running"):
                        if not shutil.which("x11vnc"):
                            logger.warning(f"[{PLUGIN_NAME}] 未找到 x11vnc，VNC 不可用")
                        elif not display:
                            logger.warning(f"[{PLUGIN_NAME}] 未能创建虚拟显示，VNC 不可用")
                        else:
                            logger.warning(
                                f"[{PLUGIN_NAME}] x11vnc 未监听端口 {vnc.get('port')}，"
                                "请检查 Xvfb / 端口占用"
                            )
                except Exception as exc:
                    logger.warning(f"[{PLUGIN_NAME}] 启动虚拟显示失败: {exc}")
            else:
                logger.warning(
                    f"[{PLUGIN_NAME}] Linux 依赖安装失败: {result.get('message')}"
                )
                for step in result.get("steps", []):
                    if not step.get("ok"):
                        logger.warning(
                            f"[{PLUGIN_NAME}] {step.get('command')}: "
                            f"{shorten(str(step.get('output') or ''), 300)}"
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] Linux dependency install exception: {exc}", exc_info=True)

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
        # Headless Linux servers have no browser to launch; the page opens the
        # URL in the operator's own browser instead.
        headless = IS_LINUX and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if not headless:
            try:
                opened = bool(webbrowser.open(url))
            except Exception:
                opened = False
        return json_response({"ok": True, "url": url, "opened": opened, "headless": headless})

    async def api_reveal_password(self):
        return json_response({"ok": True, "password": self.manager.password})

    async def api_reset_password(self):
        try:
            result = await self.manager.reset_password()
            return json_response(result)
        except Exception as exc:
            return error_response(f"重置密码失败: {shorten(str(exc), 200)}")

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
        for key in SECRET_KEYS:
            data[key] = ""
        # Read-only helper for the config dialog's mirror picker.
        data["download_mirrors"] = list(GITHUB_MIRRORS)
        return json_response(data)

    async def api_save_config(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        for key, default in DEFAULT_CONFIG.items():
            if key in SECRET_KEYS or key in ("onboarding_done", "onboarding_version"):
                continue
            if key in payload and payload[key] is not None:
                self.config[key] = _coerce(key, payload[key], default)
                self._overrides.add(key)

        for key in SECRET_KEYS:
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            self.config[key] = encrypt_secret(value.strip())
            self._overrides.add(key)
            if key == "webui_password":
                self.manager._load_password()
                self.manager.auth_ok = False
                if self.manager.api is not None:
                    self.manager.api.token = ""
            elif key == "vnc_password":
                self.manager.qq.stop_vnc()
        if "vnc_port" in payload:
            self.manager.qq.stop_vnc()

        self.manager._installed_version = ""
        self._save_config(self.config)
        self.manager._snapshot_ts = 0.0
        await self.manager.refresh_deploy_mode()
        if await self.manager.is_running_check_async() or self.manager.deploy_mode == "external":
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

    async def api_save_download_options(self):
        """Persist download-time options immediately.

        The download page has no "save" button: picking a mirror (or a
        version/flavor) must apply to the very next install.
        """
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return error_response("下载设置需要 JSON 对象")
        changed: Dict[str, Any] = {}
        if "download_mirror" in payload:
            changed["download_mirror"] = str(payload.get("download_mirror") or "").strip()
        if "snowluma_version" in payload:
            version = str(payload.get("snowluma_version") or "latest").strip()
            changed["snowluma_version"] = version or "latest"
        if "release_flavor" in payload:
            flavor = str(payload.get("release_flavor") or "full").strip().lower()
            changed["release_flavor"] = flavor if flavor in ("full", "lite") else "full"
        for key, value in changed.items():
            self.config[key] = value
            self._overrides.add(key)
        if changed:
            self._save_config(self.config)
            self.manager._snapshot_ts = 0.0
        return json_response({"ok": True, "message": "下载设置已生效", "options": changed})

    async def api_guide_asset(self):
        """Serve onboarding images as base64.

        AstrBot mounts plugin pages differently across versions and platforms
        (and relative ``assets/`` URLs break when the page has no trailing
        slash), so the guide never relies on static file routing.
        """
        name = str(request.query.get("name", "") or "").strip()
        if name not in GUIDE_ASSETS:
            return error_response("不支持的引导图片")
        path = Path(__file__).resolve().parent / "pages" / "snowluma_go" / "assets" / name
        try:
            payload = path.read_bytes()
            if len(payload) > 4 * 1024 * 1024:
                return error_response("引导图片过大")
        except Exception as exc:
            return error_response(f"读取引导图片失败: {shorten(str(exc), 160)}")
        return json_response(
            {
                "ok": True,
                "name": name,
                "mime": GUIDE_ASSETS[name],
                "data": base64.b64encode(payload).decode("ascii"),
            }
        )

    async def api_onboarding_done(self):
        self.config["onboarding_done"] = True
        self.config["onboarding_version"] = ONBOARD_CONFIG_VERSION
        self._overrides.update({"onboarding_done", "onboarding_version"})
        self._save_config(self.config)
        return json_response({"ok": True, "version": ONBOARD_CONFIG_VERSION})

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
            if key in SECRET_KEYS:
                continue
            if key in self._overrides and persisted.get(key) is not None:
                config[key] = _coerce(key, persisted[key], default)
            elif object_values.get(key) is not None:
                config[key] = _coerce(key, object_values[key], default)
            else:
                config[key] = default

        # Keep the DISPLAY setting non-empty so the WebUI always shows and
        # persists the value it will actually use (99 == :99).
        if not str(config.get("qq_display") or "").strip():
            config["qq_display"] = DEFAULT_CONFIG["qq_display"]
            self._overrides.add("qq_display")

        for key in SECRET_KEYS:
            stored = str(persisted.get(key) or object_values.get(key) or "")
            plain = decrypt_secret(stored) if stored else ""
            config[key] = encrypt_secret(plain) if plain else ""

        self._persist(config)
        return config

    def _persist(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config if config is not None else self.config
        data: Dict[str, Any] = {
            "__config_version__": CONFIG_VERSION,
            "__overrides__": sorted(self._overrides),
        }
        for key in self._overrides:
            if key in DEFAULT_CONFIG and key not in SECRET_KEYS:
                data[key] = cfg.get(key, DEFAULT_CONFIG[key])
        for key in SECRET_KEYS:
            if cfg.get(key):
                data[key] = cfg[key]
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
        for task in (self._auto_start_task, self._post_install_task, self._linux_deps_task):
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
        try:
            self.manager.qq.shutdown_display()
        except Exception:
            pass
