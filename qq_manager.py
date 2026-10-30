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
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from astrbot.api import logger

from .linux_tools import fix_qq_permissions, run_sudo
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


_BIN_DIRS = (
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/local/sbin",
    "/usr/sbin",
    "/sbin",
    "/snap/bin",
)


def find_system_libssh2() -> str:
    """Locate the distro's libssh2.

    Linux QQ ships its own ``libssh2.so.1``. When the system ``libcurl`` is
    newer than that copy, ``node-loader`` fails with
    ``undefined symbol: libssh2_session_set_read_timeout``; the login window
    is never created and Node only reports an unhandled rejection. Preloading
    the system library is the documented workaround.
    """
    if IS_WINDOWS:
        return ""
    for candidate in (
        "/usr/lib/x86_64-linux-gnu/libssh2.so.1",
        "/lib/x86_64-linux-gnu/libssh2.so.1",
        "/usr/lib/aarch64-linux-gnu/libssh2.so.1",
        "/lib/aarch64-linux-gnu/libssh2.so.1",
        "/usr/lib64/libssh2.so.1",
        "/usr/lib/libssh2.so.1",
        "/usr/lib/libssh2.so",
    ):
        try:
            if Path(candidate).is_file():
                return candidate
        except OSError:
            continue
    try:
        result = subprocess.run(
            ["ldconfig", "-p"], capture_output=True, text=True, timeout=5
        )
        for line in (result.stdout or "").splitlines():
            if "libssh2.so" not in line or "=>" not in line:
                continue
            path = line.split("=>")[-1].strip()
            if Path(path).is_file():
                return path
    except Exception:
        pass
    return ""


def find_qq_bundled_libssh2() -> List[str]:
    """List libssh2 copies shipped inside the QQ installation."""
    if IS_WINDOWS:
        return []
    found: List[str] = []
    for root in ("/opt/QQ/resources/app", "/opt/qq/resources/app"):
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            for candidate in base.rglob("libssh2.so*"):
                if candidate.is_file():
                    found.append(str(candidate))
        except OSError:
            continue
    return found


def find_qq_app_dir() -> Optional[Path]:
    """Locate QQ's ``resources/app`` directory holding the Electron entry."""
    if IS_WINDOWS:
        return None
    for root in (
        "/opt/QQ/resources/app",
        "/opt/qq/resources/app",
        "/opt/napcat-qq/resources/app",
    ):
        candidate = Path(root)
        if (candidate / "package.json").is_file():
            return candidate
    return None


# Linux QQ's original Electron entry. NapCat-style installers replace it with
# ./loadNapCat.js, which breaks QQ completely once NapCat's files are gone.
QQ_DEFAULT_MAIN = "./application.asar/app_launcher/index.js"
QQ_MAIN_FALLBACKS = (
    "./app_launcher/index.js",
    "./application.asar/index.js",
    "./index.js",
)


def find_binary(name: str) -> Optional[str]:
    """Resolve a binary even when the service has a minimal ``PATH``.

    AstrBot is often started by systemd/supervisord with ``PATH`` limited to
    the virtualenv, so ``shutil.which("fluxbox")`` can miss ``/usr/bin``
    binaries that exist perfectly well for the login shell. That mismatch is
    what made the plugin silently fall back to openbox.
    """
    found = shutil.which(name)
    if found:
        return found
    for base in _BIN_DIRS:
        candidate = Path(base) / name
        try:
            if candidate.is_file() and os.access(str(candidate), os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


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
        self._wm_name = ""
        self._qq_proc: Optional[subprocess.Popen] = None
        self._qq_log_offset = 0

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
        xdpyinfo = find_binary("xdpyinfo")
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

    def _stop_stale_window_managers(self, display: str) -> None:
        """Kill window managers left over from a previous plugin process.

        Two window managers fighting over the same display keeps QQ's login
        dialog unmapped, which looks exactly like a black VNC frame.
        """
        if IS_WINDOWS:
            return
        current_uid = os.getuid() if hasattr(os, "getuid") else None
        our_pid = os.getpid()
        for process_name in ("fluxbox", "openbox"):
            for pid in list_pids_by_name(process_name):
                if pid == our_pid:
                    continue
                uid = process_uid(pid)
                if current_uid is not None and uid is not None and uid != current_uid:
                    continue
                target = ""
                try:
                    raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
                    for item in raw.split(b"\x00"):
                        if item.startswith(b"DISPLAY="):
                            target = item.split(b"=", 1)[1].decode("utf-8", errors="ignore").strip()
                            break
                except Exception:
                    target = ""
                if target and target.split(".")[0] != display.split(".")[0]:
                    continue
                try:
                    kill_pid_tree(pid)
                    self.manager._log(
                        f"[qq] 已停止占用 {display} 的旧窗口管理器 {process_name}(pid={pid})"
                    )
                except Exception:
                    pass

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
                xvfb = find_binary("Xvfb")
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
                            # Compact virtual screen: the QQ login window is
                            # ~400x600, so 800x600 keeps QR captures small.
                            [xvfb, display, "-screen", "0", "800x600x16", "-ac"],
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
        # x11vnc exits with "Wayland display server detected" when the service
        # environment advertises Wayland, even though DISPLAY points at Xvfb.
        env.pop("WAYLAND_DISPLAY", None)
        env["XDG_SESSION_TYPE"] = "x11"
        # SnowLuma's manual Linux guide uses fluxbox specifically; openbox is
        # only a fallback. Without a real WM the QQ login dialog often never
        # gets mapped, which is exactly a black VNC screen. If fluxbox was
        # installed while we were already running openbox, switch immediately.
        fluxbox = find_binary("fluxbox")
        wm = fluxbox or find_binary("openbox")
        running_wm = self._openbox_proc is not None and self._openbox_proc.poll() is None
        if running_wm and wm and Path(wm).name != (self._wm_name or ""):
            self.manager._log(
                f"[qq] 检测到 {Path(wm).name}，切换当前窗口管理器（原 {self._wm_name or '未知'}）"
            )
            try:
                self._openbox_proc.terminate()
                self._openbox_proc.wait(timeout=2)
            except Exception:
                try:
                    self._openbox_proc.kill()
                except Exception:
                    pass
            self._openbox_proc = None
            running_wm = False
        if not running_wm:
            self._stop_stale_window_managers(display)
            if wm and Path(wm).name != "fluxbox":
                self.manager._log(
                    "[qq] [warn] 未安装 fluxbox，暂用 openbox；QQ 登录窗口可能无法映射，"
                    "请安装 fluxbox 后点「清理残留」再启动",
                    "warning",
                )
            if wm:
                try:
                    wm_log = self.manager.persistent_dir / "window-manager.log"
                    wm_log.parent.mkdir(parents=True, exist_ok=True)
                    with open(wm_log, "ab") as log:
                        self._openbox_proc = subprocess.Popen(
                            [wm],
                            env=env,
                            stdout=log,
                            stderr=log,
                            start_new_session=True,
                        )
                    time.sleep(0.6)
                    if self._openbox_proc.poll() is not None:
                        tail = self._tail_log(wm_log, 300)
                        self.manager._log(
                            f"[qq] [warn] 窗口管理器 {Path(wm).name} 立即退出"
                            f"（code={self._openbox_proc.returncode}）: {tail or '无输出'}",
                            "warning",
                        )
                        self._openbox_proc = None
                        fallback = find_binary("openbox") if Path(wm).name == "fluxbox" else None
                        if fallback:
                            with open(wm_log, "ab") as log:
                                self._openbox_proc = subprocess.Popen(
                                    [fallback],
                                    env=env,
                                    stdout=log,
                                    stderr=log,
                                    start_new_session=True,
                                )
                            wm = fallback
                    if self._openbox_proc is not None:
                        self._wm_name = Path(wm).name
                        self.manager._log(
                            f"[qq] 已启动窗口管理器 {self._wm_name}: {display}（{wm}）"
                        )
                except Exception as exc:
                    self.manager._log(
                        f"[qq] [warn] 启动窗口管理器失败: {shorten(str(exc), 140)}",
                        "warning",
                    )
            else:
                self.manager._log(
                    "[qq] [warn] 未安装 fluxbox/openbox，QQ 窗口可能无法正常显示"
                    f"（PATH={shorten(os.environ.get('PATH', ''), 160)}）",
                    "warning",
                )

        try:
            port = int(self.manager.plugin.config.get("vnc_port", 5900) or 5900)
        except (TypeError, ValueError):
            port = 5900
        if not port_is_open("127.0.0.1", port, timeout=0.2):
            x11vnc = find_binary("x11vnc")
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
                try:
                    vnc_log.write_bytes(b"")
                except Exception:
                    pass
                args += ["-o", str(vnc_log)]
                self._vnc_proc = subprocess.Popen(
                    args,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                time.sleep(1.0)
                code = self._vnc_proc.poll()
                if code is not None:
                    detail = self._tail_log(vnc_log, 400) or "x11vnc 没有输出（可尝试手动运行同样的命令）"
                    self.manager._log(
                        f"[qq] [warn] x11vnc 启动后退出（code={code}，DISPLAY={display}，端口 {port}）: "
                        f"{detail}",
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
            # Live control inside the panel uses HTTP screenshots + xdotool,
            # which works inside AstrBot's sandboxed iframe (no WebSocket).
            "input_available": bool(find_binary("xdotool")),
        }

    def _image_mean(self, path: Path) -> Optional[float]:
        identify = find_binary("identify")
        if not identify:
            return None
        try:
            result = subprocess.run(
                [identify, "-format", "%[fx:mean]", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return float((result.stdout or "").strip())
        except Exception:
            return None

    def _image_center_white_ratio(self, path: Path) -> Optional[float]:
        """Fraction of near-white pixels in the centre of the frame.

        QQ's startup splash is a small logo on a black canvas, while the real
        login page has a large white QR card in the middle. That difference is
        what lets us wait for the QR instead of returning the splash.
        """
        convert = find_binary("convert") or find_binary("magick")
        if not convert:
            return None
        try:
            result = subprocess.run(
                [
                    convert,
                    str(path),
                    "-gravity",
                    "center",
                    "-crop",
                    "50%x50%+0+0",
                    "+repage",
                    "-colorspace",
                    "Gray",
                    "-threshold",
                    "55%",
                    "-format",
                    "%[fx:mean]",
                    "info:",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return float((result.stdout or "").strip())
        except Exception:
            return None

    def _qq_log_tail(self, limit: int = 400) -> str:
        return self._tail_log(self.manager.persistent_dir / "qq.log", limit)

    def _qq_log_excerpt(self, limit: int = 1200) -> str:
        """First real error of the current QQ run, with Chromium noise removed.

        Electron writes a lot of ``[pid:date:WARNING:...]`` chatter that hides
        the actual Node/QQ error. We only look at output produced since the
        current launch and anchor on the first rejection/error line, because
        the failure header is always *before* the stack tail.
        """
        path = self.manager.persistent_dir / "qq.log"
        try:
            raw = path.read_bytes()
        except Exception:
            return ""
        offset = min(self._qq_log_offset, len(raw))
        data = raw[offset:]
        if len(data) > 400_000:
            data = data[-400_000:]
        text = data.decode("utf-8", errors="ignore")
        kept: List[str] = []
        for line in text.splitlines():
            if re.match(r"^\[\d+:\d{4}/\d{6}\.", line.strip()):
                continue
            kept.append(line)
        cleaned = "\n".join(kept).strip()
        if not cleaned:
            return self._qq_log_tail(limit)
        positions = [
            cleaned.find(marker)
            for marker in (
                "UnhandledPromiseRejectionWarning",
                "ERR_MODULE_NOT_FOUND",
                "Cannot find module",
                "Error:",
            )
        ]
        positions = [pos for pos in positions if pos >= 0]
        if positions:
            start = max(0, min(positions) - 120)
            return cleaned[start:start + limit]
        return cleaned[-limit:]

    def _diagnose_display(self, display: str) -> str:
        """Compact diagnostics shown when QQ never maps a window."""
        lines: List[str] = []
        xwininfo = find_binary("xwininfo")
        if xwininfo:
            try:
                result = subprocess.run(
                    [xwininfo, "-display", display, "-root", "-tree"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                tree = (result.stdout or result.stderr or "").strip()
                lines.append("xwininfo: " + (shorten(tree, 700) or "无输出"))
            except Exception as exc:
                lines.append(f"xwininfo 失败: {shorten(str(exc), 120)}")
        pids = sorted(running_qq_main_pids())
        lines.append("QQ 主进程: " + (", ".join(str(pid) for pid in pids) or "无"))
        for pid in pids[:2]:
            try:
                cmdline = (
                    Path(f"/proc/{pid}/cmdline")
                    .read_bytes()
                    .replace(b"\x00", b" ")
                    .decode("utf-8", errors="ignore")
                    .strip()
                )
                lines.append(f"PID {pid}: {shorten(cmdline, 220)}")
                raw = Path(f"/proc/{pid}/environ").read_bytes()
                for item in raw.split(b"\x00"):
                    if item.startswith(
                        (b"DISPLAY=", b"QT_QPA_PLATFORM=", b"WAYLAND_DISPLAY=")
                    ):
                        lines.append("  " + item.decode("utf-8", errors="ignore"))
            except Exception as exc:
                lines.append(f"PID {pid}: 无法读取 ({shorten(str(exc), 80)})")
        tail = self._qq_log_excerpt(900)
        if tail:
            lines.append("qq.log: " + tail)
        versions = self._qq_versions_dir()
        try:
            builds = sorted(p.name for p in versions.iterdir() if p.is_dir()) if versions.is_dir() else []
        except Exception:
            builds = []
        lines.append(
            "QQ 热更新版本目录: "
            + (f"{versions} -> {', '.join(builds)}" if builds else f"{versions}（不存在）")
        )
        bundled = find_qq_bundled_libssh2()
        lines.append(
            "libssh2: 系统="
            + (find_system_libssh2() or "未安装")
            + "；QQ 自带="
            + (", ".join(bundled) or "无")
        )
        entry = self.qq_entry_state()
        if entry.get("found"):
            lines.append(f"QQ 启动入口(main): {entry.get('main') or '未知'}")
            if entry.get("hijacked"):
                lines.append("QQ 启动入口被 NapCat 补丁接管，请点「修复 QQ 登录界面」")
        return "\n".join(lines)

    def _display_has_qq_window(self, display: str, *, log_missing: bool = True) -> bool:
        """Best-effort check that QQ actually mapped a window on this display."""
        xwininfo = find_binary("xwininfo")
        if not xwininfo:
            self.manager._log(
                "[qq] [warn] 未安装 xwininfo，无法检查 QQ 窗口；可点「安装 Linux 依赖」",
                "warning",
            )
            return True
        try:
            result = subprocess.run(
                [xwininfo, "-display", display, "-root", "-tree"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            tree = result.stdout or ""
            window_line = ""
            for line in tree.splitlines():
                lowered = line.lower()
                if "qq" not in lowered and "登录" not in lowered:
                    continue
                match = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", line)
                if not match:
                    continue
                width, height = int(match.group(1)), int(match.group(2))
                if width >= 120 and height >= 120:
                    window_line = line.strip()
                    break
            if window_line:
                return True
            if log_missing:
                self.manager._log(
                    f"[qq] [warn] {display} 没有可见的 QQ 窗口；当前窗口树: "
                    f"{shorten(tree.strip(), 500)}",
                    "warning",
                )
            return False
        except Exception:
            return True

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
                    "800x600",
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
            if not find_binary(executable):
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

    def _display_geometry(self, display: str) -> Tuple[int, int]:
        xdotool = find_binary("xdotool")
        if xdotool:
            try:
                env = {**os.environ, "DISPLAY": display, "XDG_SESSION_TYPE": "x11"}
                env.pop("WAYLAND_DISPLAY", None)
                result = subprocess.run(
                    [xdotool, "getdisplaygeometry"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                parts = (result.stdout or "").split()
                if len(parts) == 2:
                    return int(parts[0]), int(parts[1])
            except Exception:
                pass
        return 0, 0

    def _capture_live(self, display: str, path: Path) -> bool:
        """Grab the display as a small JPEG for the in-panel live view."""
        png = path.with_suffix(".png")
        if not self._capture_display(display, png):
            return False
        convert = find_binary("convert") or find_binary("magick")
        if convert:
            try:
                result = subprocess.run(
                    [convert, str(png), "-quality", "72", str(path)],
                    capture_output=True,
                    timeout=12,
                )
                if result.returncode == 0 and path.is_file() and path.stat().st_size > 1024:
                    try:
                        png.unlink()
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
        # No ImageMagick: fall back to the PNG frame.
        try:
            png.replace(path)
        except Exception:
            return False
        return path.is_file()

    async def remote_screen(self) -> Dict[str, Any]:
        """Screenshot for the panel's HTTP live view (no WebSocket needed)."""
        if IS_WINDOWS:
            return {"ok": False, "message": "该功能仅支持 Linux"}
        display = await asyncio.to_thread(self.ensure_display)
        if not display:
            return {"ok": False, "message": "没有可用的虚拟显示"}
        path = self.manager.persistent_dir / "qq-live.jpg"
        ok = await asyncio.to_thread(self._capture_live, display, path)
        if not ok:
            return {"ok": False, "message": "截图失败，请查看 Xvfb 日志"}
        width, height = await asyncio.to_thread(self._display_geometry, display)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        return {
            "ok": True,
            "image": f"data:{mime};base64,{encoded}",
            "width": width,
            "height": height,
            "display": display,
        }

    async def remote_input(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send a click / key / typed text to the virtual display via xdotool."""
        if IS_WINDOWS:
            return {"ok": False, "message": "该功能仅支持 Linux"}
        xdotool = find_binary("xdotool")
        if not xdotool:
            return {
                "ok": False,
                "message": "未安装 xdotool，请点「安装 Linux 依赖」",
            }
        display = self._display or self._display_name()
        env = {**os.environ, "DISPLAY": display, "XDG_SESSION_TYPE": "x11"}
        env.pop("WAYLAND_DISPLAY", None)
        kind = str(payload.get("type") or "").strip()

        def call(args: List[str]) -> subprocess.CompletedProcess:
            return subprocess.run(
                [xdotool, *args],
                env=env,
                capture_output=True,
                timeout=8,
            )

        try:
            if kind == "click":
                try:
                    x = int(payload.get("x", 0))
                    y = int(payload.get("y", 0))
                except (TypeError, ValueError):
                    return {"ok": False, "message": "坐标无效"}
                if x < 0 or y < 0 or x > 20000 or y > 20000:
                    return {"ok": False, "message": "坐标超出范围"}
                result = await asyncio.to_thread(
                    call, ["mousemove", str(x), str(y), "click", "1"]
                )
            elif kind == "key":
                key = str(payload.get("key") or "")
                allowed = {
                    "Return",
                    "Tab",
                    "Escape",
                    "BackSpace",
                    "space",
                    "Up",
                    "Down",
                    "Left",
                    "Right",
                    "Page_Up",
                    "Page_Down",
                }
                if key not in allowed:
                    return {"ok": False, "message": "不支持的按键"}
                result = await asyncio.to_thread(call, ["key", key])
            elif kind == "type":
                text = str(payload.get("text") or "")
                if not text or len(text) > 200:
                    return {"ok": False, "message": "文本为空或过长"}
                result = await asyncio.to_thread(
                    call, ["type", "--delay", "40", "--", text]
                )
            else:
                return {"ok": False, "message": "不支持的操作"}
        except Exception as exc:
            return {"ok": False, "message": shorten(str(exc), 160)}
        if result.returncode != 0:
            detail = (result.stderr or b"").decode("utf-8", errors="ignore").strip()
            return {"ok": False, "message": shorten(detail or "xdotool 执行失败", 180)}
        return {"ok": True}

    async def restart_qq(self, password: str = "") -> Dict[str, Any]:
        """Restart QQ so it moves onto the plugin's managed virtual display."""
        await self.cleanup_all(password)
        await asyncio.sleep(0.8)
        return await self.launch(password)

    @staticmethod
    def _qq_versions_dir() -> Path:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(base) / "QQ" / "versions"

    def qq_entry_state(self) -> Dict[str, Any]:
        """Report whether QQ's Electron entry was hijacked by a NapCat loader."""
        app_dir = find_qq_app_dir()
        if app_dir is None:
            return {"found": False}
        pkg = app_dir / "package.json"
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
        except Exception as exc:
            return {"found": True, "app_dir": str(app_dir), "error": shorten(str(exc), 120)}
        main = str(data.get("main") or "")
        hijacked = "loadnapcat" in main.lower()
        loader = app_dir / "loadNapCat.js"
        napcat = app_dir / "napcat" / "napcat.mjs"
        return {
            "found": True,
            "app_dir": str(app_dir),
            "main": main,
            "hijacked": hijacked,
            "loader_exists": loader.is_file(),
            "napcat_exists": napcat.is_file(),
            # NapCat's loader does `import("./napcat/napcat.mjs")`. When either
            # file is gone QQ dies before creating the login window.
            "broken": hijacked and not (loader.is_file() and napcat.is_file()),
        }

    def qq_config_writable(self) -> bool:
        """Whether QQ can create its own ``~/.config/QQ/log`` directory."""
        if IS_WINDOWS:
            return True
        config = Path.home() / ".config" / "QQ"
        try:
            config.mkdir(parents=True, exist_ok=True)
            log_dir = config / "log"
            log_dir.mkdir(parents=True, exist_ok=True)
            probe = log_dir / ".snowluma-write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return True
        except Exception:
            return False

    def ptrace_scope(self) -> int:
        """Current Yama ptrace scope (0 = SnowLuma can attach to QQ)."""
        if IS_WINDOWS:
            return 0
        try:
            return int(Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip())
        except Exception:
            return 0

    def foreign_qq_pids(self) -> List[int]:
        """QQ processes owned by another system user (typically a stale root one)."""
        if IS_WINDOWS:
            return []
        uid = os.getuid() if hasattr(os, "getuid") else None
        if uid is None:
            return []
        foreign: List[int] = []
        try:
            pids = list(self.detect().get("running_pids") or [])
        except Exception:
            pids = running_qq_main_pids()
        for pid in pids:
            owner = process_uid(pid)
            if owner is not None and owner != uid:
                foreign.append(pid)
        return sorted(foreign)

    def qq_env_status(self) -> List[str]:
        """Cheap, side-effect-free version of ``_qq_env_problems`` for status."""
        if IS_WINDOWS:
            return []
        problems: List[str] = []
        config = Path.home() / ".config" / "QQ"
        log_dir = config / "log"
        target = log_dir if log_dir.exists() else config
        try:
            if target.exists():
                if not os.access(str(target), os.W_OK):
                    problems.append("~/.config/QQ 不可写")
            elif not os.access(str(config.parent), os.W_OK):
                problems.append("~/.config 不可写")
        except Exception:
            problems.append("~/.config/QQ 不可写")
        scope = self.ptrace_scope()
        if scope > 0:
            problems.append(f"kernel.yama.ptrace_scope={scope}")
        foreign = self.foreign_qq_pids()
        if foreign:
            problems.append("其他用户启动的 QQ 进程 " + ", ".join(str(pid) for pid in foreign))
        return problems

    def _qq_env_problems(self) -> List[str]:
        problems: List[str] = []
        if not self.qq_config_writable():
            problems.append("~/.config/QQ 不可写")
        scope = self.ptrace_scope()
        if scope > 0:
            problems.append(f"kernel.yama.ptrace_scope={scope}")
        foreign = self.foreign_qq_pids()
        if foreign:
            problems.append("其他用户启动的 QQ 进程 " + ", ".join(str(pid) for pid in foreign))
        return problems

    async def prepare_qq_environment(self, password: str = "") -> Dict[str, Any]:
        """Pre-launch fixes: NapCat injection, QQ config perms and ptrace.

        All three make QQ die before mapping a window (or make SnowLuma's hook
        fail with COMPONENT_LOAD_FAILED), so they are checked before every
        start; the WebUI supplies the sudo password when one is needed.
        """
        if IS_WINDOWS:
            return {"ok": True, "skipped": True}
        napcat = await self.auto_repair_napcat_entry(password)
        if napcat.get("need_sudo") or not napcat.get("ok"):
            return napcat

        problems = self._qq_env_problems()
        if not problems:
            return {
                "ok": True,
                "skipped": napcat.get("skipped", True),
                "message": napcat.get("message") or "",
            }
        reason = "，".join(problems)
        if not password:
            self.manager._log(
                f"[qq] [warn] QQ 启动环境需要修复：{reason}；需要 sudo 密码",
                "warning",
            )
            return {
                "ok": False,
                "need_sudo": True,
                "message": "需要 sudo 密码修复：" + reason,
            }

        # fix_qq_permissions covers chown/chmod of ~/.config/QQ, ptrace_scope
        # and setcap on the SnowLuma node binary in one pass.
        fixed = await fix_qq_permissions(self.manager.plugin, password)
        if not fixed.get("ok"):
            self.manager._log(
                f"[qq] [warn] 部分权限修复失败: {fixed.get('message')}", "warning"
            )
        foreign = self.foreign_qq_pids()
        if foreign:
            self.manager._log(
                f"[qq] 正在结束其他用户的 QQ 进程: {foreign}", "warning"
            )
            await self.cleanup_all(password)
        remaining = self._qq_env_problems()
        if remaining:
            return {
                "ok": False,
                "message": "权限修复未完成：" + "，".join(remaining),
            }
        self.manager._log(f"[qq] 已修复 QQ 启动环境：{reason}")
        return {"ok": True, "skipped": False, "message": "已修复 QQ 启动环境"}

    async def auto_repair_napcat_entry(self, password: str = "") -> Dict[str, Any]:
        """Detect and undo a NapCat injection that hides QQ's login window.

        Called automatically from the start / launch flow: a leftover
        ``loadNapCat.js`` entry throws ``ERR_MODULE_NOT_FOUND`` inside QQ's
        main process, so no window is ever created. Repair needs sudo because
        ``resources/app`` is root-owned; without it we only log the reason.
        """
        if IS_WINDOWS:
            return {"ok": True, "skipped": True, "message": "仅 Linux 需要"}
        try:
            state = self.qq_entry_state()
        except Exception as exc:
            return {"ok": False, "message": f"检测 NapCat 注入失败: {shorten(str(exc), 120)}"}
        if not state.get("found") or not state.get("hijacked"):
            return {"ok": True, "skipped": True}
        if not state.get("broken"):
            return {"ok": True, "skipped": True, "message": "NapCat 注入文件完整，未自动修改"}
        if not password:
            self.manager._log(
                "[qq] [warn] 检测到 NapCat 注入已破坏 QQ 启动入口"
                f"（main={state.get('main')}，napcat.mjs 缺失），"
                "需要 sudo 密码才能自动修复；请在 WebUI 输入后重试",
                "warning",
            )
            return {
                "ok": False,
                "need_sudo": True,
                "message": "需要 sudo 密码自动修复 NapCat 注入",
            }
        self.manager._log(
            "[qq] 检测到 NapCat 注入导致 QQ 无法创建登录窗口，正在自动恢复启动入口"
        )
        result = await self.repair_qq_entry(password)
        if result.get("ok"):
            self.manager._log(
                f"[qq] [ok] 已自动修复 NapCat 注入（main={result.get('main')}）"
            )
            running = sorted(running_qq_main_pids())
            if running:
                self.manager._log(
                    f"[qq] [warn] QQ 正在运行（PID {running}），需要重启 QQ 后修复才会生效",
                    "warning",
                )
                result = dict(result)
                result["message"] = "已修复 NapCat 注入，需要重启 QQ 生效"
        else:
            self.manager._log(
                f"[qq] [err] 自动修复 NapCat 注入失败: {result.get('message')}",
                "error",
            )
        return result

    async def repair_qq_entry(self, password: str = "") -> Dict[str, Any]:
        """Restore QQ's original ``main`` after a NapCat-style installer.

        NapCat patches ``resources/app/package.json`` to run ``loadNapCat.js``,
        which imports ``napcat/napcat.mjs`` from an absolute path. When the old
        plugin data dir disappears, that import throws inside QQ's main process
        and the login window is never created.
        """
        if IS_WINDOWS:
            return {"ok": False, "message": "该修复仅适用于 Linux"}
        state = self.qq_entry_state()
        app_dir = state.get("app_dir")
        if not state.get("found"):
            return {"ok": False, "message": "未找到 /opt/QQ/resources/app/package.json"}
        if state.get("error"):
            return {"ok": False, "message": f"读取 package.json 失败: {state['error']}"}
        if not state.get("hijacked"):
            return {"ok": True, "already": True, "message": "QQ 启动入口正常，无需修复"}
        if not password:
            return {
                "ok": False,
                "need_sudo": True,
                "message": (
                    "恢复 QQ 启动入口需要 sudo 密码；"
                    "请在 WebUI 中输入后重试"
                ),
            }
        base = Path(app_dir)
        target_main = ""
        if (base / "application.asar").exists():
            # Works for both the packed asar file and the unpacked directory.
            target_main = QQ_DEFAULT_MAIN
        else:
            for alt in QQ_MAIN_FALLBACKS:
                if (base / alt).exists():
                    target_main = alt
                    break
            if not target_main:
                return {
                    "ok": False,
                    "message": (
                        "找不到 QQ 原始入口文件，无法自动恢复；"
                        "请重装 Linux QQ（sudo apt-get install --reinstall ./linuxqq_*.deb）"
                    ),
                }
        pkg_path = base / "package.json"
        try:
            data = json.loads(pkg_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception as exc:
            return {"ok": False, "message": f"读取 package.json 失败: {shorten(str(exc), 140)}"}
        data["main"] = target_main
        tmp = self.manager.persistent_dir / "package.json.snowluma"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        steps = []
        for args in (
            ["cp", str(pkg_path), f"{pkg_path}.napcat-bak"],
            ["cp", str(tmp), str(pkg_path)],
            ["chmod", "644", str(pkg_path)],
        ):
            code, output = await run_sudo(self.manager.plugin, args, password)
            steps.append({"command": " ".join(args), "ok": code == 0, "output": output})
            if code != 0:
                return {
                    "ok": False,
                    "message": f"恢复 QQ 启动入口失败: {shorten(output, 180)}",
                    "steps": steps,
                }
        loader = base / "loadNapCat.js"
        if loader.is_file():
            code, output = await run_sudo(
                self.manager.plugin, ["mv", str(loader), f"{loader}.disabled"], password
            )
            steps.append(
                {"command": f"mv {loader} {loader}.disabled", "ok": code == 0, "output": output}
            )
        self.manager._log(f"[qq] 已恢复 QQ 启动入口: {target_main}")
        return {
            "ok": True,
            "main": target_main,
            "steps": steps,
            "message": f"已恢复 QQ 启动入口为 {target_main}，请重新「启动 QQ」",
        }

    async def repair_qq_ui(self, password: str = "") -> Dict[str, Any]:
        """Roll back a broken QQ hot update, a known cause of a blank login UI.

        QQNT hot-updates itself into ``~/.config/QQ/versions/<build>``. Some
        downloaded builds ship an unusable ``libssh2.so.1`` and then the login
        window never appears (upstream reports "only background" or no window).
        Renaming the build folder makes QQ fall back to the bundled version.
        """
        if IS_WINDOWS:
            return {"ok": False, "message": "该修复仅适用于 Linux"}
        await self.cleanup_all(password)
        entry = await self.repair_qq_entry(password)
        if entry.get("ok") and not entry.get("already"):
            return entry
        if not entry.get("ok") and entry.get("message", "").startswith("QQ 启动入口被"):
            return entry
        base = self._qq_versions_dir()
        renamed: List[str] = []
        stamp = time.strftime("%Y%m%d%H%M%S")
        try:
            if base.is_dir():
                for entry in sorted(base.iterdir()):
                    if not entry.is_dir() or entry.name.startswith("."):
                        continue
                    target = entry.with_name(f"{entry.name}.bak-{stamp}")
                    try:
                        entry.rename(target)
                        renamed.append(entry.name)
                    except Exception:
                        continue
        except Exception as exc:
            return {"ok": False, "message": f"处理 QQ 版本目录失败: {shorten(str(exc), 160)}"}
        if not renamed:
            system_ssh2 = find_system_libssh2()
            bundled = find_qq_bundled_libssh2()
            if not system_ssh2:
                message = (
                    "未发现 QQ 热更新版本，但系统缺少 libssh2。请先安装："
                    "sudo apt-get install -y libssh2-1，然后重新启动 QQ"
                )
            elif bundled:
                message = (
                    "未发现 QQ 热更新版本。已检测到 QQ 自带 libssh2，启动时会自动预加载系统 "
                    f"{system_ssh2}；请点「启动 QQ」重试"
                )
            else:
                message = (
                    f"未发现 QQ 热更新版本目录（{base}），也没有找到 QQ 自带的 libssh2；"
                    "请查看 qq.log 与诊断信息"
                )
            return {"ok": False, "message": message}
        self.manager._log(f"[qq] 已回滚 QQ 热更新版本: {renamed}")
        return {
            "ok": True,
            "renamed": renamed,
            "message": "已回滚 QQ 热更新版本，请重新「启动 QQ」",
        }

    async def login_qr(self, password: str = "") -> Dict[str, Any]:
        if IS_WINDOWS:
            return {"ok": False, "message": "Windows 会直接显示 QQ 登录窗口，无需截图"}
        display = await asyncio.to_thread(self.ensure_display)
        if not display:
            return {
                "ok": False,
                "message": "没有可用的图形显示。请先安装 Xvfb / x11vnc / scrot，或在配置里填写 DISPLAY。",
            }

        if not running_qq_main_pids():
            result = await self.launch(password)
            if not result.get("ok"):
                return result
            # QQ needs a moment to paint the login window; the user can refresh
            # if the first capture is still blank.
            await asyncio.sleep(2.5)
        else:
            await asyncio.sleep(0.4)
        if not await asyncio.to_thread(self._display_has_qq_window, display):
            return {
                "ok": False,
                "message": (
                    "当前虚拟显示里还没有 QQ 窗口（QQ 可能运行在别的 DISPLAY）。"
                    "请先点「清理 QQ 进程」，再点「启动 QQ」，等登录窗口出现后刷新。"
                ),
                "detail": await asyncio.to_thread(self._diagnose_display, display),
                "display": display,
                "vnc": await self.vnc_info(),
            }
        path = self.manager.persistent_dir / "qq-login.png"
        # QQ paints a black splash with a small logo first; the QR card only
        # shows up a few seconds later. Poll instead of returning the splash.
        captured = False
        white_ratio: Optional[float] = None
        deadline = time.monotonic() + 24.0
        while True:
            captured = await asyncio.to_thread(self._capture_display, display, path)
            if not captured:
                break
            white_ratio = await asyncio.to_thread(self._image_center_white_ratio, path)
            if white_ratio is None or white_ratio >= 0.18:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(2.0)
        if not captured:
            tools = [name for name in ("scrot", "import", "ffmpeg") if find_binary(name)]
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
        mean = await asyncio.to_thread(self._image_mean, path)
        if mean is not None and mean < 0.02 and (white_ratio is None or white_ratio <= 0.001):
            return {
                "ok": False,
                "message": (
                    "截图是黑屏：QQ 没有画在当前虚拟显示上，通常说明它仍在旧的 DISPLAY 中运行。"
                    "请点下方「重启 QQ 并刷新」，或先点「清理 QQ 进程」再启动 QQ。"
                ),
                "display": display,
                "vnc": await self.vnc_info(),
                "black": True,
            }
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        still_loading = white_ratio is not None and white_ratio < 0.18
        result = {
            "ok": True,
            "image": "data:image/png;base64," + encoded,
            "display": display,
            "vnc": await self.vnc_info(),
            "loading": still_loading,
            "message": (
                "QQ 仍在加载登录界面（当前是启动画面），请稍等几秒后再点「刷新二维码」"
                if still_loading
                else "已截取当前虚拟屏幕"
            ),
        }
        if still_loading:
            result["detail"] = await asyncio.to_thread(self._diagnose_display, display)
        return result

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
        self._wm_name = ""

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
        # On a headless host, orphaned window managers from a previous plugin
        # process can prevent fluxbox from taking over the display.
        if not os.environ.get("DISPLAY"):
            for process_name in ("fluxbox", "openbox"):
                for pid in list_pids_by_name(process_name):
                    if pid in killed:
                        continue
                    uid = process_uid(pid)
                    if current_uid is not None and uid is not None and uid != current_uid:
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

    async def launch(self, password: str = "") -> Dict[str, Any]:
        if self._launching:
            return {"ok": False, "message": "QQ 正在启动中"}
        # A stale NapCat injection kills QQ before it can map the login window;
        # repair it before every launch attempt (also covers restart flows).
        repaired = {"ok": True, "skipped": True}
        try:
            repaired = await self.prepare_qq_environment(password)
        except Exception as exc:
            self.manager._log(
                f"[qq] [warn] 启动前环境检查失败: {shorten(str(exc), 140)}",
                "warning",
            )
        if not repaired.get("ok"):
            payload = {
                "ok": False,
                "message": repaired.get("message")
                or "QQ 启动环境检查未通过，已停止启动",
            }
            if repaired.get("need_sudo"):
                payload["need_sudo"] = True
            return payload
        if not IS_WINDOWS:
            try:
                scope = int(Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip())
                if scope > 0:
                    if password:
                        code, output = await run_sudo(
                            self.manager.plugin,
                            ["sysctl", "-w", "kernel.yama.ptrace_scope=0"],
                            password,
                            timeout=30,
                        )
                        if code == 0:
                            self.manager._log(
                                "[qq] 已自动设置 kernel.yama.ptrace_scope=0（sudo）"
                            )
                        else:
                            self.manager._log(
                                f"[qq] [warn] 自动设置 ptrace_scope 失败: {shorten(output, 160)}",
                                "warning",
                            )
                    else:
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
            message = "QQ 已在运行，未重复启动"
            if repaired.get("ok") and not repaired.get("skipped"):
                message = "已修复 NapCat 注入，需要重启 QQ 生效"
            return {
                "ok": True,
                "pids": info["running_pids"],
                "message": message,
            }
        entry = await asyncio.to_thread(self.qq_entry_state)
        if entry.get("hijacked"):
            self.manager._log(
                "[qq] [warn] QQ 启动入口仍由 NapCat 注入接管（main="
                f"{entry.get('main')}）；如果 NapCat 已卸载，请点「修复 QQ 登录界面」",
                "warning",
            )
        self._launching = True
        before_all = set(running_qq_pids())
        before_main = set(info["running_pids"])
        exe = info["path"]
        display = await asyncio.to_thread(self.ensure_display) if not IS_WINDOWS else ""
        launch_env = os.environ.copy()
        # A leftover NapCat env var would make loadNapCat.js look for a module
        # in a plugin directory that no longer exists.
        launch_env.pop("NAPCAT_BOOTMAIN", None)
        if display:
            launch_env["DISPLAY"] = display
            # Force Qt/Electron onto X11. A stale WAYLAND_DISPLAY in the
            # service environment would otherwise make QQ render off-screen.
            launch_env.pop("WAYLAND_DISPLAY", None)
            launch_env["QT_QPA_PLATFORM"] = "xcb"
            launch_env["ELECTRON_OZONE_PLATFORM_HINT"] = "x11"
            launch_env["XDG_SESSION_TYPE"] = "x11"
            launch_env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
            launch_env.setdefault("QT_X11_NO_MITSHM", "1")
            launch_env.setdefault("NO_AT_BRIDGE", "1")
            system_ssh2 = await asyncio.to_thread(find_system_libssh2)
            if system_ssh2:
                preload = launch_env.get("LD_PRELOAD", "").strip()
                launch_env["LD_PRELOAD"] = f"{system_ssh2} {preload}".strip()
                self.manager._log(f"[qq] 已预加载系统 libssh2: {system_ssh2}")
            try:
                runtime_dir = Path(f"/tmp/runtime-{os.getuid()}")
                runtime_dir.mkdir(mode=0o700, exist_ok=True)
                launch_env["XDG_RUNTIME_DIR"] = str(runtime_dir)
            except Exception:
                pass
            await asyncio.to_thread(self._ensure_session_bus, launch_env)
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
                if not display:
                    return {
                        "ok": False,
                        "message": (
                            "无法创建 Xvfb 虚拟显示，QQ 不会在不可见的 xvfb-run 中启动。"
                            "请先点「安装 Linux 依赖」并查看 xvfb.log。"
                        ),
                    }
                # QQNT/Electron on Xvfb often creates the process but never
                # maps a window unless the Chromium sandbox/GPU paths used by
                # the official Linux launcher are disabled. Output goes to
                # qq.log: without it a silent renderer failure is invisible.
                cmd = [
                    exe,
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-gpu-compositing",
                    "--disable-dev-shm-usage",
                    "--ozone-platform=x11",
                    "--enable-logging=stderr",
                ]
                qq_log = self.manager.persistent_dir / "qq.log"
                qq_log.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if qq_log.exists() and qq_log.stat().st_size > 2 * 1024 * 1024:
                        qq_log.write_bytes(b"")
                    self._qq_log_offset = qq_log.stat().st_size if qq_log.exists() else 0
                except Exception:
                    self._qq_log_offset = 0
                with open(qq_log, "ab") as log:
                    self._qq_proc = subprocess.Popen(
                        cmd,
                        cwd=str(Path(exe).parent),
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                        env=launch_env,
                    )
            self.manager._log(f"[qq] 已启动 QQ: {exe}（DISPLAY={display}）")
            for _ in range(10):
                await asyncio.sleep(1)
                if self._qq_proc is not None and self._qq_proc.poll() is not None:
                    self.manager._log(
                        "[qq] [warn] QQ 启动后立即退出（code="
                        f"{self._qq_proc.returncode}）: {self._qq_log_excerpt(800) or '无输出'}",
                        "warning",
                    )
                    break
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
                    if display:
                        window_ok = False
                        for _ in range(8):
                            await asyncio.sleep(1)
                            if await asyncio.to_thread(
                                self._display_has_qq_window, display, log_missing=False
                            ):
                                window_ok = True
                                break
                        if window_ok:
                            self.manager._log(f"[qq] {display} 已检测到 QQ 窗口，可以扫码登录")
                        else:
                            self.manager._log(
                                f"[qq] [warn] QQ 主进程已启动，但 {display} 上仍未出现窗口"
                                f"；qq.log: {self._qq_log_excerpt(1000) or '无输出'}",
                                "warning",
                            )
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

    def _ensure_session_bus(self, env: Dict[str, str]) -> None:
        """Give QQ a private D-Bus session on headless hosts.

        Electron/Qt fall back to a degraded mode (or hang before mapping a
        window) when no session bus exists. ``dbus-x11`` is part of SnowLuma's
        documented dependency list for exactly this reason.
        """
        if env.get("DBUS_SESSION_BUS_ADDRESS"):
            return
        dbus = find_binary("dbus-daemon")
        if not dbus:
            return
        try:
            result = subprocess.run(
                [dbus, "--session", "--fork", "--print-address=1"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            address = ""
            for line in (result.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("unix:"):
                    address = line
                    break
            if address:
                env["DBUS_SESSION_BUS_ADDRESS"] = address
                self.manager._log("[qq] 已创建独立 D-Bus 会话")
        except Exception:
            pass

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
        self._qq_proc = None
        self.manager._log(f"[qq] 已关闭本插件启动的 QQ 进程: {targets}")
        return {"ok": True, "pids": targets, "message": "已关闭本插件启动的 QQ"}

    async def cleanup_all(self, password: str = "") -> Dict[str, Any]:
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
        # SnowLuma cannot hook it. Use the one-shot sudo password for these.
        if foreign:
            if password:
                code, output = await run_sudo(
                    self.manager.plugin,
                    ["kill", "-9", *[str(pid) for pid in foreign]],
                    password,
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
        self._qq_proc = None
        # Prevent the auto flow from immediately launching QQ again after a
        # deliberate cleanup; the manual start button still works.
        self.manager._qq_autostarted = True
        self._cache = None

        if skipped:
            message = (
                f"已结束 {len(killed)} 个 QQ 进程；还有 {len(skipped)} 个属于其他系统用户，"
                "请在弹出的 sudo 窗口输入密码或手动结束"
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
