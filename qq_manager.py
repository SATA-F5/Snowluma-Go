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
import base64
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from astrbot.api import logger

from .linux_tools import run_sudo, sudo_password
from .utils import (
    CREATE_NO_WINDOW,
    IS_WINDOWS,
    PLUGIN_NAME,
    decrypt_secret,
    kill_pid_tree,
    list_pids_by_name,
    port_is_open,
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


def process_uid(pid: int) -> Optional[int]:
    """Return the Linux real UID for a PID, or None when unavailable."""
    if IS_WINDOWS or int(pid) <= 0:
        return None
    try:
        for line in Path(f"/proc/{int(pid)}/status").read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1])
    except (OSError, ValueError):
        return None
    return None


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
        self._xvfb_proc: Optional[subprocess.Popen] = None
        self._openbox_proc: Optional[subprocess.Popen] = None
        self._vnc_proc: Optional[subprocess.Popen] = None
        self._vnc_local_only = True
        self._display = ""

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

    # ---------------- Linux display / VNC / QR ----------------

    def _display_name(self) -> str:
        configured = str(self.manager.plugin.config.get("qq_display", "") or "").strip()
        display = configured or os.environ.get("DISPLAY", "") or ":99"
        display = display.strip()
        # Users often type "99" instead of ":99"; Xvfb/open_connection need the
        # colon form. A bare numeric display was previously treated as a remote
        # X11 target, so Xvfb/x11vnc never started.
        if display and display[0].isdigit():
            display = ":" + display
        return display

    def _display_socket(self, display: str) -> Path:
        token = display.lstrip(":").split(".")[0]
        return Path(f"/tmp/.X11-unix/X{token}")

    @staticmethod
    def _tail_log(path: Path, limit: int = 400) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            return text[-limit:]
        except Exception:
            return ""

    def _lock_pid(self, display: str) -> int:
        token = display.lstrip(":").split(".")[0]
        try:
            return int(Path(f"/tmp/.X{token}-lock").read_text(encoding="utf-8", errors="ignore").strip())
        except Exception:
            return 0

    def _display_alive(self, display: str) -> bool:
        xdpyinfo = shutil.which("xdpyinfo")
        if xdpyinfo:
            try:
                result = subprocess.run(
                    [xdpyinfo, "-display", display],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                return result.returncode == 0
            except Exception:
                return False
        token = display.lstrip(":").split(".")[0]
        lock = Path(f"/tmp/.X{token}-lock")
        if lock.exists():
            try:
                pid = int(lock.read_text(encoding="utf-8", errors="ignore").strip())
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                # The lock belongs to another user (usually root); we cannot
                # access that X server from AstrBot's account.
                return False
            except (OSError, ValueError):
                pass
        return self._display_socket(display).exists()

    def _pick_free_display(self, preferred: str) -> str:
        """Find a display whose lock/socket is not held by another user."""
        token = preferred.lstrip(":").split(".")[0]
        try:
            base = int(token)
        except ValueError:
            return preferred
        for number in [base, *range(base + 1, base + 20)]:
            candidate = f":{number}"
            if self._display_alive(candidate):
                return candidate
            lock = Path(f"/tmp/.X{number}-lock")
            sock = self._display_socket(candidate)
            if not lock.exists() and not sock.exists():
                return candidate
            # If the lock names a live process it is NOT stale, even when
            # xdpyinfo is unavailable or failed transiently.
            lock_pid = self._lock_pid(candidate)
            if lock_pid > 0:
                try:
                    os.kill(lock_pid, 0)
                    continue
                except ProcessLookupError:
                    pass
                except OSError:
                    continue
            # Stale files owned by this user can be removed; root-owned locks
            # stay and we simply move on to the next display number.
            for stale in (lock, sock):
                try:
                    stale.unlink()
                except Exception:
                    pass
            if not lock.exists() and not sock.exists():
                return candidate
        return preferred

    def ensure_display(self) -> str:
        """Start Xvfb/openbox/x11vnc when the host has no visible display."""
        if IS_WINDOWS:
            return ""
        display = self._display_name()
        if not display:
            return ""
        # SSH X11 forwarding / remote display (host:10.0) is used as-is.
        if not display.startswith(":"):
            self._display = display
            return display

        started = False
        owned_running = (
            bool(self._display)
            and self._xvfb_proc is not None
            and self._xvfb_proc.poll() is None
        )
        if owned_running:
            # Reuse the Xvfb we started earlier in this process instead of
            # recomputing :99 -> :101 and spawning a duplicate server.
            display = self._display
            started = True
        elif not self._display_alive(display):
            # A killed Xvfb leaves /tmp/.X11-unix/X99 and /tmp/.X99-lock
            # behind; a root-owned lock cannot be unlinked by astrbot, so fall
            # back to :100, :101, ... instead of failing every launch.
            token = display.lstrip(":").split(".")[0]
            for stale in (self._display_socket(display), Path(f"/tmp/.X{token}-lock")):
                try:
                    stale.unlink()
                except Exception:
                    pass
            free_display = self._pick_free_display(display)
            if free_display != display:
                self.manager._log(
                    f"[qq] [warn] 显示 {display} 的锁不可用，改用 {free_display}",
                    "warning",
                )
                display = free_display
            if self._display_alive(display):
                # The picked display is an already-running Xvfb (often an
                # orphan from the previous plugin process). Reuse it instead of
                # immediately spawning a competing server on the same display.
                self.manager._log(f"[qq] 复用已有虚拟显示 {display}")
            else:
                xvfb = shutil.which("Xvfb")
                if not xvfb:
                    self.manager._log(
                        "[qq] [warn] 未安装 Xvfb，无法创建虚拟显示；可点「安装 Linux 依赖」",
                        "warning",
                    )
                    return os.environ.get("DISPLAY", "")
                log_path = self.manager.persistent_dir / "xvfb.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with open(log_path, "ab") as log:
                        self._xvfb_proc = subprocess.Popen(
                            [xvfb, display, "-screen", "0", "1280x800x24", "-ac"],
                            stdout=log,
                            stderr=log,
                            start_new_session=True,
                        )
                    time.sleep(1.5)
                    code = self._xvfb_proc.poll()
                    if code is not None:
                        self.manager._log(
                            f"[qq] [warn] Xvfb 启动后退出（code={code}）: "
                            f"{shorten(self._tail_log(log_path), 240)}",
                            "warning",
                        )
                        self._xvfb_proc = None
                        return os.environ.get("DISPLAY", "")
                    started = True
                    self.manager._log(f"[qq] 已启动虚拟显示 {display}")
                except Exception as exc:
                    self.manager._log(f"[qq] [warn] 启动 Xvfb 失败: {shorten(str(exc), 140)}", "warning")
                    return os.environ.get("DISPLAY", "")

        # A real desktop (DISPLAY=:0) should not get an openbox/x11vnc sidecar.
        # A headless server with a manually/previously started Xvfb should.
        configured_display = str(self.manager.plugin.config.get("qq_display", "") or "").strip()
        env_display = os.environ.get("DISPLAY", "").strip()
        # ":99" is the plugin's own virtual display; even if the service env
        # already exports DISPLAY=:99 we still need to start x11vnc on it.
        headless_managed = (
            bool(configured_display)
            or not env_display
            or display == ":99"
            or display != self._display_name()
        )
        if not started and self._xvfb_proc is None and not headless_managed:
            self._display = display
            return display

        env = {**os.environ, "DISPLAY": display}
        if self._openbox_proc is None or self._openbox_proc.poll() is not None:
            openbox = shutil.which("openbox")
            if openbox:
                try:
                    self._openbox_proc = subprocess.Popen(
                        [openbox],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except Exception:
                    pass

        try:
            port = int(self.manager.plugin.config.get("vnc_port", 5900) or 5900)
        except (TypeError, ValueError):
            port = 5900
        if not port_is_open("127.0.0.1", port, timeout=0.2):
            x11vnc = shutil.which("x11vnc")
            if not x11vnc:
                self.manager._log(
                    "[qq] [warn] 未安装 x11vnc，VNC 不可用；可点「安装 Linux 依赖」",
                    "warning",
                )
                self._display = display
                return display

            raw_pw = str(self.manager.plugin.config.get("vnc_password", "") or "")
            vnc_password = decrypt_secret(raw_pw) if raw_pw else ""
            args = [
                x11vnc,
                "-display",
                display,
                "-forever",
                "-shared",
                "-rfbport",
                str(port),
                "-noxdamage",
                "-quiet",
            ]
            if vnc_password:
                passfile = self.manager.persistent_dir / ".vncpass"
                try:
                    subprocess.run(
                        [x11vnc, "-storepasswd", vnc_password, str(passfile)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                    )
                    try:
                        passfile.chmod(0o600)
                    except Exception:
                        pass
                    args += ["-rfbauth", str(passfile)]
                except Exception:
                    args += ["-localhost", "-nopw"]
                    self._vnc_local_only = True
            else:
                # Without a password never expose VNC to the LAN.
                args += ["-localhost", "-nopw"]
            self._vnc_local_only = not vnc_password or "-localhost" in args

            vnc_log = self.manager.persistent_dir / "x11vnc.log"
            vnc_log.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(vnc_log, "ab") as log:
                    self._vnc_proc = subprocess.Popen(
                        args,
                        env=env,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
                time.sleep(1.0)
                code = self._vnc_proc.poll()
                if code is not None:
                    self.manager._log(
                        f"[qq] [warn] x11vnc 启动后退出（code={code}）: "
                        f"{shorten(self._tail_log(vnc_log), 240)}",
                        "warning",
                    )
                    self._vnc_proc = None
                else:
                    self.manager._log(
                        f"[qq] 已启动 x11vnc: {display}，端口 {port}"
                        + ("（仅本机，可用 SSH 隧道）" if self._vnc_local_only else "")
                    )
            except Exception as exc:
                self.manager._log(f"[qq] [warn] 启动 x11vnc 失败: {shorten(str(exc), 140)}", "warning")
                self._vnc_proc = None
        self._display = display
        return display

    async def vnc_info(self) -> Dict[str, Any]:
        if self._vnc_proc is not None and self._vnc_proc.poll() is not None:
            code = self._vnc_proc.returncode
            self._vnc_proc = None
            self.manager._log(
                f"[qq] [warn] x11vnc 已退出（code={code}）: "
                f"{shorten(self._tail_log(self.manager.persistent_dir / 'x11vnc.log'), 200)}",
                "warning",
            )
        try:
            port = int(self.manager.plugin.config.get("vnc_port", 5900) or 5900)
        except (TypeError, ValueError):
            port = 5900
        return {
            "port": port,
            "display": self._display or self._display_name(),
            "running": await asyncio.to_thread(port_is_open, "127.0.0.1", port, 0.2),
            "local_only": self._vnc_local_only,
        }

    def _capture_display(self, display: str, path: Path) -> bool:
        if IS_WINDOWS:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "DISPLAY": display}
        attempts = [
            (["scrot", "-o", str(path)], "scrot"),
            (["import", "-display", display, "-window", "root", str(path)], "import"),
            (
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "x11grab",
                    "-video_size",
                    "1280x800",
                    "-i",
                    display,
                    "-frames:v",
                    "1",
                    str(path),
                ],
                "ffmpeg",
            ),
        ]
        for command, executable in attempts:
            if not shutil.which(executable):
                continue
            try:
                subprocess.run(
                    command,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=25,
                    check=False,
                )
                if path.is_file() and path.stat().st_size > 1024:
                    return True
            except Exception:
                continue
        return False

    async def login_qr(self) -> Dict[str, Any]:
        if IS_WINDOWS:
            return {"ok": False, "message": "Windows 会直接显示 QQ 登录窗口，无需截图"}
        display = await asyncio.to_thread(self.ensure_display)
        if not display:
            return {
                "ok": False,
                "message": "没有可用的图形显示。请先安装 Xvfb / x11vnc / scrot，或在配置里填写 DISPLAY。",
            }

        if not running_qq_main_pids():
            result = await self.launch()
            if not result.get("ok"):
                return result
            # QQ needs a moment to paint the login window; the user can refresh
            # if the first capture is still blank.
            await asyncio.sleep(2.5)
        else:
            await asyncio.sleep(0.4)
        path = self.manager.persistent_dir / "qq-login.png"
        captured = await asyncio.to_thread(self._capture_display, display, path)
        if not captured:
            tools = [name for name in ("scrot", "import", "ffmpeg") if shutil.which(name)]
            if not tools:
                message = "未找到截图工具 scrot / import / ffmpeg，请点「安装 Linux 依赖」。"
            else:
                message = (
                    f"截图工具 {', '.join(tools)} 已存在，但无法读取 DISPLAY={display}；"
                    "请查看插件日志中的 Xvfb / x11vnc 错误。"
                )
            return {
                "ok": False,
                "message": message,
                "display": display,
                "vnc": await self.vnc_info(),
            }
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {
            "ok": True,
            "image": "data:image/png;base64," + encoded,
            "display": display,
            "vnc": await self.vnc_info(),
            "message": "已截取当前虚拟屏幕",
        }

    def stop_vnc(self) -> None:
        if self._vnc_proc is not None and self._vnc_proc.poll() is None:
            try:
                self._vnc_proc.terminate()
            except Exception:
                pass
        self._vnc_proc = None

    def shutdown_display(self) -> None:
        for proc in (self._vnc_proc, self._openbox_proc, self._xvfb_proc):
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            except Exception:
                pass
        self._vnc_proc = None
        self._openbox_proc = None
        self._xvfb_proc = None

    def cleanup_displays(self) -> List[int]:
        """Reap Xvfb/x11vnc processes for the plugin's virtual displays.

        The explicit "清理残留" action is allowed to clean displays left behind
        by a previous plugin process. Only the current Unix user's processes
        are touched, and only displays in our managed range.
        """
        if IS_WINDOWS:
            return []
        display = self._display or self._display_name()
        managed = {self._display_name(), display, ":99"}
        managed.update(f":{number}" for number in range(100, 111))
        current_uid = os.getuid() if hasattr(os, "getuid") else None
        killed: List[int] = []
        self.shutdown_display()
        for process_name in ("Xvfb", "x11vnc"):
            for pid in list_pids_by_name(process_name):
                if pid in killed:
                    continue
                uid = process_uid(pid)
                if current_uid is not None and uid is not None and uid != current_uid:
                    continue
                try:
                    cmdline = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\x00", b" ").decode(
                        "utf-8", errors="ignore"
                    )
                except Exception:
                    continue
                if not any(candidate in cmdline for candidate in managed):
                    continue
                try:
                    kill_pid_tree(pid)
                    killed.append(pid)
                except Exception:
                    pass
        if killed:
            self.manager._log(f"[qq] 已清理虚拟显示进程: {sorted(killed)}")
        return killed

    # ---------------- launch / stop ----------------

    async def launch(self) -> Dict[str, Any]:
        if self._launching:
            return {"ok": False, "message": "QQ 正在启动中"}
        if not IS_WINDOWS:
            try:
                scope = int(Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip())
                if scope > 0:
                    self.manager._log(
                        f"[qq] [warn] kernel.yama.ptrace_scope={scope}，SnowLuma 可能无法读取 QQ "
                        "进程；请在配置里点「修复 QQ 权限」",
                        "warning",
                    )
            except Exception:
                pass
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
        display = await asyncio.to_thread(self.ensure_display) if not IS_WINDOWS else ""
        launch_env = os.environ.copy()
        if display:
            launch_env["DISPLAY"] = display
        try:
            if IS_WINDOWS:
                subprocess.Popen(
                    [exe],
                    cwd=str(Path(exe).parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW | 0x00000200,  # CREATE_NEW_PROCESS_GROUP
                    env=launch_env,
                )
            else:
                cmd = [exe]
                if not display and not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
                    cmd = [
                        "xvfb-run",
                        "-a",
                        "-s",
                        "-screen 0 1280x800x24",
                        exe,
                    ]
                    self.manager._log(
                        "[qq] [warn] 未配置 qq_display，使用不可见的 xvfb-run；"
                        "无桌面服务器请先启动 Xvfb/VNC，并在配置里填写 DISPLAY（如 :99）",
                        "warning",
                    )
                subprocess.Popen(
                    cmd,
                    cwd=str(Path(exe).parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=launch_env,
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

    async def cleanup_all(self) -> Dict[str, Any]:
        """Kill QQ processes owned by this user, plus foreign ones via sudo."""
        pids = running_qq_pids()
        if not pids:
            return {"ok": True, "killed": [], "skipped": [], "message": "没有运行中的 QQ 进程"}

        current_uid = os.getuid() if hasattr(os, "getuid") else None
        targets: List[int] = []
        foreign: List[int] = []
        for pid in pids:
            uid = process_uid(pid)
            if current_uid is not None and uid is not None and uid != current_uid:
                foreign.append(pid)
            else:
                targets.append(pid)

        killed: List[int] = []
        skipped: List[Dict[str, Any]] = []
        for pid in targets:
            try:
                await asyncio.to_thread(kill_pid_tree, pid)
                killed.append(pid)
            except Exception as exc:
                self.manager._log(
                    f"[qq] [warn] 结束 QQ 进程 {pid} 失败: {shorten(str(exc), 120)}",
                    "warning",
                )

        # An old root-owned QQ is exactly why the panel can see a process but
        # SnowLuma cannot hook it. Use the encrypted sudo password for these.
        if foreign:
            if sudo_password(self.manager.plugin):
                code, output = await run_sudo(
                    self.manager.plugin,
                    ["kill", "-9", *[str(pid) for pid in foreign]],
                    timeout=30,
                )
                if code == 0:
                    killed.extend(foreign)
                    self.manager._log(f"[qq] 已通过 sudo 结束其他用户的 QQ 进程: {sorted(foreign)}")
                else:
                    skipped.extend({"pid": pid, "uid": process_uid(pid)} for pid in foreign)
                    self.manager._log(f"[qq] [warn] sudo 清理其他用户 QQ 失败: {shorten(output, 160)}", "warning")
            else:
                skipped.extend({"pid": pid, "uid": process_uid(pid)} for pid in foreign)

        self.launched_pids.clear()
        # Prevent the auto flow from immediately launching QQ again after a
        # deliberate cleanup; the manual start button still works.
        self.manager._qq_autostarted = True
        self._cache = None

        if skipped:
            message = (
                f"已结束 {len(killed)} 个 QQ 进程；还有 {len(skipped)} 个属于其他系统用户，"
                "请配置 sudo 密码或手动结束"
            )
            self.manager._log(f"[qq] [warn] {message}", "warning")
        else:
            message = f"已结束 {len(killed)} 个 QQ 进程" if killed else "没有可结束的 QQ 进程"
        self.manager._log(f"[qq] {message}")
        return {
            "ok": bool(killed) or not pids,
            "killed": killed,
            "skipped": skipped,
            "message": message,
        }

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
