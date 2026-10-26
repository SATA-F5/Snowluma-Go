"""Bridge AstrBot's aiocqhttp (OneBot v11) bot into SnowLuma — via the API.

NapCat-era plugins rewrote ``onebot11_<uin>.json`` on disk and restarted the
process. SnowLuma exposes ``POST /api/config/<uin>`` which validates, persists
and *hot-reloads* the adapter set, so this module only needs to:

1. read AstrBot's ``cmd_config.json`` to find the reverse-WS listener,
2. read SnowLuma's current OneBot config for the logged-in UIN,
3. upsert a ``wsClients`` entry pointing at AstrBot,
4. POST it back and report whether the live session applied it.

No restarts, no file races, and no assumptions about SnowLuma's on-disk schema
beyond the documented API contract.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

from .snowluma_api import SnowLumaAuthError, SnowLumaConsentRequired
from .utils import (
    DEFAULT_CONNECTION_NAME,
    IS_LINUX,
    IS_MACOS,
    IS_WINDOWS,
    PLUGIN_NAME,
    mask_secret,
    read_json,
    resolve_persistent_dir,
    shorten,
    write_json,
)


class ConfigSyncer:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.context = plugin.context
        self.plugin_dir = Path(plugin.context.plugin_dir) if hasattr(plugin.context, "plugin_dir") else Path(__file__).parent
        self.persistent_dir = resolve_persistent_dir(self.context)
        self.backup_dir = self.persistent_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self.astrbot_config_path: Optional[Path] = None
        self.astrbot_bot: Optional[Dict[str, Any]] = None
        self.uin: str = ""
        self.ws_url: str = ""
        self.last_sync_ok = False
        self.last_sync_msg = "尚未同步"
        self.last_sync_time = ""
        self.last_applied = False
        self.last_online = False
        self.last_errors: List[Dict[str, Any]] = []
        self.last_trigger = ""

    # ------------------------------------------------------------------ #
    # AstrBot config discovery
    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        self.astrbot_config_path = self._find_astrbot_config_path()
        self.astrbot_bot = self._read_astrbot_bot()
        if self.astrbot_bot:
            self.ws_url = self._build_ws_url(self.astrbot_bot)

    def _find_astrbot_config_path(self) -> Optional[Path]:
        manual = str(self.plugin.config.get("astrbot_config_path", "") or "").strip()
        if manual:
            return Path(manual).expanduser()

        candidates: List[Path] = []
        try:
            candidates.append(self.plugin_dir.parent.parent / "cmd_config.json")
        except Exception:
            pass
        if hasattr(self.context, "get_data_dir"):
            try:
                candidates.append(Path(self.context.get_data_dir()) / "cmd_config.json")
            except Exception:
                pass
        candidates += [
            Path("data/cmd_config.json"),
            Path("../data/cmd_config.json"),
            Path("../../data/cmd_config.json"),
            Path.cwd() / "data" / "cmd_config.json",
        ]

        seen = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except Exception:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.is_file():
                return resolved
        return None

    def _read_astrbot_bot(self) -> Optional[Dict[str, Any]]:
        if not self.astrbot_config_path or not self.astrbot_config_path.is_file():
            return None
        config = read_json(self.astrbot_config_path, {}) or {}
        if not isinstance(config, dict):
            return None

        platforms = config.get("platform") or []
        if not isinstance(platforms, list):
            return None

        fallback: Optional[Dict[str, Any]] = None
        for platform in platforms:
            if not isinstance(platform, dict):
                continue
            if str(platform.get("type") or "") != "aiocqhttp":
                continue
            if platform.get("enable", True) is False:
                continue
            port = platform.get("ws_reverse_port")
            try:
                port = int(port)
            except (TypeError, ValueError):
                continue
            if port <= 0:
                continue

            host = str(platform.get("ws_reverse_host", "0.0.0.0") or "0.0.0.0").strip()
            if host in ("0.0.0.0", "[::]", "::", ""):
                host = "127.0.0.1"
            token = platform.get("ws_reverse_token") or platform.get("access_token") or ""
            path = str(platform.get("ws_reverse_path", "/ws/") or "/ws/")
            if not path.startswith("/"):
                path = "/" + path
            entry = {
                "id": str(platform.get("id") or ""),
                "name": str(platform.get("name") or ""),
                "host": host,
                "port": port,
                "path": path,
                "token": str(token or ""),
            }
            if fallback is None:
                fallback = entry
            if entry["id"]:
                return entry
        return fallback

    def _build_ws_url(self, bot: Dict[str, Any]) -> str:
        override = str(self.plugin.config.get("ws_host_override", "") or "").strip()
        host = override or str(bot.get("host") or "127.0.0.1")
        host = host.strip()
        if host.startswith("ws://") or host.startswith("wss://"):
            return host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = int(bot.get("port") or 0)
        path = str(bot.get("path") or "/ws/")
        if not path.startswith("/"):
            path = "/" + path
        return f"ws://{host}:{port}{path}"

    # ------------------------------------------------------------------ #
    # SnowLuma side
    # ------------------------------------------------------------------ #

    def _connection_name(self) -> str:
        return str(self.plugin.config.get("sync_connection_name", DEFAULT_CONNECTION_NAME) or DEFAULT_CONNECTION_NAME).strip() or DEFAULT_CONNECTION_NAME

    @staticmethod
    def _network_lists(config: Dict[str, Any]) -> Dict[str, List[Any]]:
        networks = config.get("networks")
        if not isinstance(networks, dict):
            networks = {}
            config["networks"] = networks
        for key in ("httpServers", "httpClients", "wsServers", "wsClients"):
            if not isinstance(networks.get(key), list):
                networks[key] = []
        return networks  # type: ignore[return-value]

    def _build_ws_client(self, bot: Dict[str, Any]) -> Dict[str, Any]:
        # Keep exactly the fields SnowLuma persists back: it drops ``enabled``
        # (defaults to true) and empty ``accessToken`` values, so including them
        # here would make every re-sync look like a change.
        return {
            "name": self._connection_name(),
            "url": self.ws_url,
            "accessToken": str(bot.get("token") or ""),
            "messageFormat": "array",
            "reportSelfMessage": False,
            "role": "Universal",
            "reconnectIntervalMs": 5000,
        }

    def _upsert_client(self, networks: Dict[str, List[Any]], entry: Dict[str, Any]) -> Tuple[str, bool]:
        """Insert/replace our wsClients entry.

        Returns ``(action, changed)``; ``changed`` is false when the stored
        entry already equals the desired one, so repeated syncs stay no-ops.
        """
        clients = networks["wsClients"]
        name = entry["name"]
        for index, client in enumerate(list(clients)):
            if not isinstance(client, dict):
                continue
            same_name = str(client.get("name") or "") == name
            same_url = str(client.get("url") or "") == entry["url"]
            if same_name or (not same_name and same_url):
                if client == entry:
                    return "unchanged", False
                clients[index] = entry
                return "updated", True
        clients.append(entry)
        return "created", True

    def _backup(self, uin: str, config: Dict[str, Any]) -> Optional[Path]:
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.backup_dir / f"onebot_{uin}.{stamp}.json"
            write_json(path, config)
            return path
        except Exception:
            return None

    async def sync(self, trigger: str = "manual") -> Tuple[bool, str]:
        self.refresh()
        self.last_trigger = trigger

        if not self.astrbot_config_path:
            return self._fail("未找到 AstrBot 配置文件 cmd_config.json")
        if not self.astrbot_bot:
            return self._fail("AstrBot 中没有已启用的 OneBot v11 (aiocqhttp) 机器人")

        manager = self.plugin.manager
        api = await manager.ensure_api(quiet=True)
        if api is None:
            return self._fail(manager.last_error or "SnowLuma 未就绪（未运行或未登录）")

        uin = manager.primary_uin()
        if not uin:
            return self._fail("SnowLuma 里还没有在线账号：请先启动 QQ 并完成登录")
        self.uin = uin

        try:
            current = await api.get_onebot_config(uin)
        except SnowLumaConsentRequired:
            return self._fail("请先在插件页面同意 SnowLuma 用户协议 / 隐私政策，再执行同步")
        except SnowLumaAuthError:
            return self._fail("SnowLuma 会话已失效，请检查 WebUI 密码后重试")
        except Exception as e:
            return self._fail(f"读取 SnowLuma OneBot 配置失败: {shorten(str(e), 160)}")

        config = json.loads(json.dumps(current))  # defensive deep copy
        networks = self._network_lists(config)
        entry = self._build_ws_client(self.astrbot_bot)
        action, changed = self._upsert_client(networks, entry)

        if not changed:
            self.last_sync_ok = True
            self.last_applied = True
            self.last_errors = []
            self.last_sync_msg = f"配置已是最新 → {self.ws_url}"
            self.last_sync_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return True, self.last_sync_msg

        backup = self._backup(uin, current)
        try:
            result = await api.save_onebot_config(uin, config)
        except SnowLumaConsentRequired:
            return self._fail("请先在插件页面同意 SnowLuma 用户协议 / 隐私政策，再执行同步")
        except Exception as e:
            return self._fail(f"写入 SnowLuma OneBot 配置失败: {shorten(str(e), 200)}")

        saved = bool(result.get("saved", True)) and bool(result.get("success", True))
        applied = bool(result.get("applied"))
        online = bool(result.get("online"))
        errors = result.get("errors") or []
        self.last_applied = applied
        self.last_online = online
        self.last_errors = errors if isinstance(errors, list) else []

        if not saved:
            message = str(result.get("message") or "SnowLuma 拒绝了配置")
            return self._fail(message)

        if applied:
            message = f"已{'更新' if action == 'updated' else '创建'}反向 WS 连接 → {self.ws_url}（已热重载）"
        elif online:
            message = f"已保存反向 WS 连接 → {self.ws_url}，但热重载未完全生效，请查看 SnowLuma 日志"
        else:
            message = f"已保存反向 WS 连接 → {self.ws_url}，账号重新上线后生效"

        if self.last_errors:
            message += f"（{len(self.last_errors)} 条节点错误）"
        if backup:
            message += "，原配置已备份"

        self.last_sync_ok = True
        self.last_sync_msg = message
        self.last_sync_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"[{PLUGIN_NAME}] OneBot 同步（{trigger}）: {message}")
        return True, message

    async def disconnect(self) -> Tuple[bool, str]:
        """Remove this plugin's wsClients entry (leaves other adapters intact)."""
        self.refresh()
        manager = self.plugin.manager
        api = await manager.ensure_api(quiet=True)
        if api is None:
            return False, manager.last_error or "SnowLuma 未就绪"
        uin = manager.primary_uin()
        if not uin:
            return False, "没有可操作的 SnowLuma 账号"

        try:
            config = await api.get_onebot_config(uin)
        except Exception as e:
            return False, f"读取配置失败: {shorten(str(e), 160)}"

        networks = self._network_lists(config)
        name = self._connection_name()
        before = len(networks["wsClients"])
        networks["wsClients"] = [
            client
            for client in networks["wsClients"]
            if not (isinstance(client, dict) and str(client.get("name") or "") == name)
        ]
        if len(networks["wsClients"]) == before:
            return True, "未找到本插件创建的连接，无需断开"

        try:
            result = await api.save_onebot_config(uin, config)
        except Exception as e:
            return False, f"写入配置失败: {shorten(str(e), 160)}"

        self.last_sync_ok = False
        self.last_sync_msg = "已断开 AstrBot 接入"
        self.last_sync_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_applied = bool(result.get("applied"))
        return True, "已从 SnowLuma 移除 AstrBot 反向 WS 连接"

    def _fail(self, message: str) -> Tuple[bool, str]:
        self.last_sync_ok = False
        self.last_sync_msg = message
        self.last_sync_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.warning(f"[{PLUGIN_NAME}] OneBot 同步失败: {message}")
        return False, message

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    def scans(self) -> List[Dict[str, Any]]:
        """Diagnostic list of SnowLuma OneBot config files (read-only)."""
        results: List[Dict[str, Any]] = []
        manager = self.plugin.manager
        try:
            config_dir = manager.config_dir
        except Exception:
            return results
        try:
            if not config_dir.is_dir():
                return results
            for path in sorted(config_dir.glob("onebot*.json")):
                results.append({"file": path.name, "dir": str(config_dir), "size": path.stat().st_size})
        except Exception:
            return results
        return results

    def status_dict(self) -> Dict[str, Any]:
        bot = self.astrbot_bot or {}
        manager = self.plugin.manager
        uin = self.uin or manager.primary_uin()
        connection = None
        for item in manager.connections:
            if str(item.get("uin") or "") == uin:
                connection = item
                break
        return {
            "platform": "Windows" if IS_WINDOWS else ("Linux" if IS_LINUX else ("macOS" if IS_MACOS else "unknown")),
            "astrbot_config_path": str(self.astrbot_config_path) if self.astrbot_config_path else None,
            "astrbot_bot_found": self.astrbot_bot is not None,
            "astrbot_bot_id": bot.get("id") or "",
            "astrbot_bot_name": bot.get("name") or "",
            "astrbot_bot_host": bot.get("host") or "",
            "astrbot_bot_port": bot.get("port"),
            "astrbot_bot_path": bot.get("path") or "",
            "astrbot_bot_token": mask_secret(bot.get("token", "")),
            "connection_name": self._connection_name(),
            "uin": uin,
            "ws_url": self.ws_url,
            "connection": connection,
            "last_sync_ok": self.last_sync_ok,
            "last_sync_msg": self.last_sync_msg,
            "last_sync_time": self.last_sync_time,
            "last_applied": self.last_applied,
            "last_online": self.last_online,
            "last_errors": self.last_errors[:10],
            "last_trigger": self.last_trigger,
        }
