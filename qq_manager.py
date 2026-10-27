"""QQ client discovery / launch / hook helpers for SnowLuma Go.

SnowLuma does not log in by itself: it attaches to a running QQNT process via
its native hook. This module therefore only automates the boring half of the
job — finding QQ, starting it (optionally under Xvfb on headless Linux) and
asking SnowLuma's API to inject the bridge into the processes it can see.

The plugin never kills QQ processes it did not start, so an operator's own
chat client cannot be terminated by accident.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from astrbot.api import logger

from .utils import (
    CREATE_NO_WINDOW,
    IS_WINDOWS,
    PLUGIN_NAME,
    list_pids_by_name,
    shorten,
)

_QQ_NAME_HINTS = ("qq.exe", "qqnt.exe", "qq", "linuxqq")


def _registry_qq_paths() -> List[str]:
    if not IS_WINDOWS:
        return []
    paths: List[str] = []
    try:
        import winreg  # type: ignore
    except Exception:
        return paths

    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
    ]
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                values: Dict[str, str] = {}
                for name in ("DisplayIcon", "InstallLocation", "UninstallString", "DisplayVersion"):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                        values[name] = str(value or "")
                    except OSError:
                        continue
        except OSError:
            continue

        icon = values.get("DisplayIcon", "").split(",")[0].strip().strip('"')
        if icon:
            paths.append(icon)
        install = values.get("InstallLocation", "").strip().strip('"')
        if install:
            paths.append(str(Path(install) / "QQ.exe"))
        uninstall = values.get("UninstallString", "").strip().strip('"')
        if uninstall:
            paths.append(str(Path(uninstall).with_name("QQ.exe")))

    return paths


def _common_qq_paths() -> List[str]:
    if IS_WINDOWS:
        roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        suffixes = [
            r"Tencent\QQNT\QQ.exe",
            r"Tencent\QQ\QQ.exe",
            r"Tencent\QQNT\Bin\QQ.exe",
            r"Programs\Tencent\QQ\QQ.exe",
        ]
        out: List[str] = []
        for root in roots:
            if not root:
                continue
            for suffix in suffixes:
                out.append(str(Path(root) / suffix))
        return out
    return [
        "/opt/QQ/qq",
        "/opt/qq/qq",
        "/opt/linuxqq/qq",
        "/usr/bin/qq",
        "/usr/local/bin/qq",
        "/usr/lib/qq/qq",
    ]


def find_qq_executable(manual: str = "") -> Optional[str]:
    """Best-effort QQNT executable lookup (manual path wins)."""
    manual = (manual or "").strip().strip('"')
    if manual:
        path = Path(manual)
        if path.is_dir():
            for name in ("QQ.exe", "qq"):
                candidate = path / name
                if candidate.is_file():
                    return str(candidate)
        if path.is_file():
            return str(path)

    candidates: List[str] = []
    candidates.extend(_registry_qq_paths())
    candidates.extend(_common_qq_paths())
    for name in ("QQ", "qq"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    seen: Set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            resolved = str(Path(candidate).resolve())
        except Exception:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if Path(resolved).is_file():
                return resolved
        except OSError:
            continue
    return None


def qq_version() -> str:
    if IS_WINDOWS:
        try:
            import winreg  # type: ignore

            for hive, subkey in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
            ):
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        value, _ = winreg.QueryValueEx(key, "DisplayVersion")
                        if value:
                            return str(value)
                except OSError:
                    continue
        except Exception:
            pass
        return ""
    try:
        proc = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "linuxqq"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return ""


def running_qq_pids() -> List[int]:
    pids: Set[int] = set()
    if IS_WINDOWS:
        for name in ("QQ.exe", "QQNT.exe"):
            pids.update(list_pids_by_name(name))
    else:
        for name in ("qq", "linuxqq"):
            pids.update(list_pids_by_name(name))
    return sorted(pids)


def is_qq_helper_pid(pid: int) -> bool:
    """Linux QQ is Electron: one launch creates many ``qq`` child processes.

    They are named ``qq`` too, but carry ``--type=renderer|gpu-process|...``
    on the command line. Treating them as separate QQ clients made the panel
    look like it had launched half a dozen instances.
    """
    if IS_WINDOWS or int(pid) <= 0:
        return False
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except OSError:
        return False
    return b"--type=" in raw.replace(b"\x00", b" ")


def running_qq_main_pids() -> List[int]:
    return [pid for pid in running_qq_pids() if not is_qq_helper_pid(pid)]


def is_qq_process(info: Dict[str, Any]) -> bool:
    pid = int(info.get("pid") or 0)
    if pid > 0 and is_qq_helper_pid(pid):
        return False
    name = str(info.get("name") or "").lower()
    path = str(info.get("path") or "").lower()
    if name in _QQ_NAME_HINTS:
        return True
    return "qq" in name or "\\qqnt\\" in path or "/qqnt/" in path or path.endswith("/qq")


class QQManager:
    """QQ process lifecycle on top of SnowLuma's hook manager API."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.launched_pids: Set[int] = set()
        self._launching = False
        self._cache: Optional[tuple] = None

    # ---------------- detection ----------------

    def detect(self, *, force: bool = False) -> Dict[str, Any]:
        """QQ installation / process snapshot (5s cache: registry + tasklist)."""
        now = time.time()
        if not force and self._cache is not None and now - self._cache[0] < 5:
            return dict(self._cache[1])

        path = find_qq_executable(str(self.manager.plugin.config.get("qq_path", "") or ""))
        data = {
            "installed": bool(path),
            "path": path or "",
            "version": qq_version(),
            # Only the real QQ main process, not Electron renderer/GPU helpers.
            "running_pids": running_qq_main_pids(),
            "launched_pids": sorted(self.launched_pids),
            "platform": platform.system(),
        }
        self._cache = (now, data)
        return dict(data)

    # ---------------- launch / stop ----------------

    async def launch(self) -> Dict[str, Any]:
        if self._launching:
            return {"ok": False, "message": "QQ 正在启动中"}
        info = self.detect()
        if not info["installed"]:
            return {
                "ok": False,
                "message": "未检测到 QQ，请先安装 QQNT，或在配置里填写 QQ 可执行文件路径",
            }

        info = self.detect(force=True)
        if info["running_pids"]:
            return {
                "ok": True,
                "pids": info["running_pids"],
                "message": "QQ 已在运行，未重复启动",
            }
        self._launching = True
        before_all = set(running_qq_pids())
        before_main = set(info["running_pids"])
        exe = info["path"]
        try:
            if IS_WINDOWS:
                subprocess.Popen(
                    [exe],
                    cwd=str(Path(exe).parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW | 0x00000200,  # CREATE_NEW_PROCESS_GROUP
                )
            else:
                cmd = [exe]
                if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
                    cmd = [
                        "xvfb-run",
                        "-a",
                        "-s",
                        "-screen 0 1280x800x24",
                        exe,
                    ]
                subprocess.Popen(
                    cmd,
                    cwd=str(Path(exe).parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            self.manager._log(f"[qq] 已启动 QQ: {exe}")
            for _ in range(10):
                await asyncio.sleep(1)
                after_all = set(running_qq_pids())
                new_main = {
                    pid for pid in after_all
                    if not is_qq_helper_pid(pid)
                } - before_main
                if new_main:
                    # Track the helper processes too so "stop" can clean up the
                    # whole Electron tree, but only report the real client.
                    self.launched_pids.update(after_all - before_all)
                    self.manager._log(f"[qq] 检测到 QQ 主进程: {sorted(new_main)}")
                    return {"ok": True, "pids": sorted(new_main), "message": "QQ 已启动"}
            self.launched_pids.update(set(running_qq_pids()) - before_all)
            return {
                "ok": True,
                "pids": [],
                "message": "QQ 启动命令已发出，但暂未检测到主进程（QQ 可能正在初始化）",
            }
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] launch QQ failed: {e}")
            return {"ok": False, "message": f"启动 QQ 失败: {shorten(str(e), 160)}"}
        finally:
            self._launching = False

    def stop_launched(self) -> Dict[str, Any]:
        """Terminate only the QQ processes this plugin started."""
        current = set(running_qq_pids())
        targets = sorted(current & self.launched_pids)
        if not targets:
            return {"ok": False, "message": "没有由本插件启动的 QQ 进程可关闭"}
        for pid in targets:
            try:
                if IS_WINDOWS:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW,
                        timeout=15,
                    )
                else:
                    os.kill(pid, 15)
            except Exception:
                continue
        self.launched_pids.clear()
        self.manager._log(f"[qq] 已关闭本插件启动的 QQ 进程: {targets}")
        return {"ok": True, "pids": targets, "message": "已关闭本插件启动的 QQ"}

    # ---------------- hooking ----------------

    async def hookable_processes(self) -> List[Dict[str, Any]]:
        api = await self.manager.ensure_api()
        if api is None:
            return []
        try:
            processes = await api.processes()
        except Exception as e:
            self.manager._log(f"[qq] 读取进程列表失败: {e}", "warning")
            return []
        return [item for item in processes if is_qq_process(item)]

    async def hook_all(self) -> Dict[str, Any]:
        """Load the SnowLuma bridge into every QQ process that is not loaded yet."""
        api = await self.manager.ensure_api()
        if api is None:
            return {"ok": False, "message": "SnowLuma 未就绪，无法挂载 QQ"}
        processes = await self.hookable_processes()
        if not processes:
            return {"ok": False, "message": "未发现可挂载的 QQ 进程，请先启动 QQ"}
        loaded: List[int] = []
        errors: List[str] = []
        for info in processes:
            pid = int(info.get("pid") or 0)
            if pid <= 0:
                continue
            status = str(info.get("status") or "")
            if info.get("injected") or status in ("loaded", "online", "loading", "connecting"):
                continue
            try:
                result = await api.process_action(pid, "load")
                if result.get("success"):
                    loaded.append(pid)
                    self.manager._log(f"[qq] 已挂载 QQ 进程 {pid}")
                else:
                    message = str(result.get("message") or info.get("error") or "未知错误")
                    errors.append(f"{pid}: {message}")
            except Exception as e:
                errors.append(f"{pid}: {shorten(str(e), 120)}")
        if loaded:
            return {"ok": True, "loaded": loaded, "errors": errors, "message": f"已挂载 {len(loaded)} 个 QQ 进程"}
        if errors:
            return {"ok": False, "errors": errors, "message": "挂载失败: " + "; ".join(errors[:3])}
        return {"ok": True, "loaded": [], "message": "所有 QQ 进程都已处于挂载状态"}

    async def unhook(self, pid: int) -> Dict[str, Any]:
        api = await self.manager.ensure_api()
        if api is None:
            return {"ok": False, "message": "SnowLuma 未就绪"}
        try:
            result = await api.process_action(int(pid), "unload")
            return {"ok": bool(result.get("success")), "message": str(result.get("message") or "")}
        except Exception as e:
            return {"ok": False, "message": shorten(str(e), 160)}

    def account_of(self, info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        account = info.get("account")
        if isinstance(account, dict) and account.get("uin"):
            return account
        uin = str(info.get("uin") or "").strip()
        if uin:
            return {"uin": uin, "nickname": ""}
        return None
