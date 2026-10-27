"""Shared helpers for the Snowluma Go plugin.

Everything in here is dependency free (stdlib only) so the module can be
imported on every platform AstrBot runs on.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

# --------------------------------------------------------------------------- #
# Identity / defaults
# --------------------------------------------------------------------------- #

PLUGIN_NAME = "pulids_snowluma_go_to_astrbot"
CONFIG_VERSION = 1

DEFAULT_SNOWLUMA_PORT = 5099
DEFAULT_SNOWLUMA_VERSION = "latest"
DEFAULT_RELEASE_FLAVOR = "full"
DEFAULT_DOWNLOAD_MIRROR = "https://gh.zwy.one/"
DEFAULT_CONNECTION_NAME = "astrbot"

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MACOS = platform.system() == "Darwin"

CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# Node.js range required by SnowLuma's launcher (see check-node-version.cjs).
SNOWLUMA_NODE_RANGE = "^24.22.1 || >=26.12.1"

DEPLOY_MODES = {"auto", "native", "external"}
RELEASE_FLAVORS = {"full", "lite"}

DEFAULT_CONFIG: Dict[str, Any] = {
    "auto_start": True,
    "auto_sync": True,
    "auto_hook": True,
    "auto_start_qq": False,
    "deploy_mode": "auto",
    "snowluma_port": DEFAULT_SNOWLUMA_PORT,
    "snowluma_version": DEFAULT_SNOWLUMA_VERSION,
    "release_flavor": DEFAULT_RELEASE_FLAVOR,
    "download_mirror": DEFAULT_DOWNLOAD_MIRROR,
    "webui_password": "",
    "external_host": "127.0.0.1",
    "external_port": DEFAULT_SNOWLUMA_PORT,
    "qq_path": "",
    "qq_number": "",
    "astrbot_config_path": "",
    "ws_host_override": "",
    "auto_accept_agreements": False,
    "sync_connection_name": DEFAULT_CONNECTION_NAME,
    "onboarding_done": False,
}

# Mirror prefixes tried in order for GitHub release downloads.
# Same candidates (and the same order) as the NapCat_Go plugin; the official
# GitHub URL is always tried last by download.py.
GITHUB_MIRRORS: List[str] = [
    "https://gh.zwy.one/",
    "https://raw.ihtw.moe/",
    "https://gh.llkk.cc/",
    "https://gh.xxooo.cf/",
    "https://ghfile.geekertao.top/",
    "https://ghproxy.cxkpro.top/",
    "https://git.yylx.win/",
    "https://gh.h233.eu.org/",
    "https://cdn.crashmc.com/",
    "https://githubproxy.cc/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "https://ghfast.top/",
]

GITHUB_API = "https://api.github.com/repos/SnowLuma/SnowLuma"
SNOWLUMA_RELEASE_BASE = "https://github.com/SnowLuma/SnowLuma/releases/download"

# Files that prove a directory is a SnowLuma runtime root.
INSTALL_MARKERS = ("index.mjs", "check-node-version.cjs", "package.json")

QQ_PROCESS_NAMES_WINDOWS = ("QQ.exe", "QQNT.exe")
QQ_PROCESS_NAMES_LINUX = ("qq", "linuxqq")

_SECRET_SALT = b"slgo-webui-secret-v1"

# --------------------------------------------------------------------------- #
# Persistent storage
# --------------------------------------------------------------------------- #


def resolve_persistent_dir(context: Any) -> Path:
    """Return (and create) the per-plugin data directory.

    AstrBot exposes ``context.get_data_dir()`` for plugin-scoped storage; when a
    newer/older host does not, fall back to ``data/plugin_data/<plugin>``.
    """
    candidate: Optional[Path] = None
    if hasattr(context, "get_data_dir"):
        try:
            candidate = Path(context.get_data_dir())
        except Exception:
            candidate = None
    if candidate is None:
        candidate = Path("data") / "plugin_data" / PLUGIN_NAME
    elif candidate.name != PLUGIN_NAME:
        candidate = candidate / PLUGIN_NAME
    if not candidate.is_absolute():
        try:
            candidate = (Path.cwd() / candidate).resolve()
        except Exception:
            candidate = candidate.absolute()
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def plugin_dir_of(context: Any) -> Path:
    try:
        if hasattr(context, "plugin_dir"):
            return Path(context.plugin_dir)
    except Exception:
        pass
    return Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Secrets at rest (WebUI password / bootstrap password)
# --------------------------------------------------------------------------- #


def _machine_master_key() -> bytes:
    parts: List[str] = []
    try:
        parts.append(str(uuid.getnode()))
    except Exception:
        pass
    for getter in (platform.node, lambda: str(Path.home().resolve())):
        try:
            parts.append(getter())
        except Exception:
            pass
    raw = "|".join(parts).encode("utf-8", errors="ignore")
    return hashlib.pbkdf2_hmac("sha256", raw, _SECRET_SALT, 120_000, dklen=32)


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    data = plain.encode("utf-8")
    try:
        from cryptography.fernet import Fernet  # type: ignore

        key = base64.urlsafe_b64encode(_machine_master_key())
        return "enc:fernet:" + Fernet(key).encrypt(data).decode("ascii")
    except Exception:
        salt = os.urandom(16)
        stream = b""
        counter = 0
        while len(stream) < len(data):
            stream += hashlib.pbkdf2_hmac(
                "sha256", _machine_master_key(), salt + counter.to_bytes(4, "big"), 1, dklen=32
            )
            counter += 1
        blob = bytes(b ^ stream[i] for i, b in enumerate(data))
        return "enc:v1:" + base64.b64encode(salt + blob).decode("ascii")


def decrypt_secret(stored: str) -> str:
    if not stored:
        return ""
    try:
        if stored.startswith("enc:fernet:"):
            from cryptography.fernet import Fernet  # type: ignore

            key = base64.urlsafe_b64encode(_machine_master_key())
            return Fernet(key).decrypt(stored[len("enc:fernet:") :].encode("ascii")).decode("utf-8")
        if stored.startswith("enc:v1:"):
            blob = base64.b64decode(stored[len("enc:v1:") :])
            salt, enc = blob[:16], blob[16:]
            stream = b""
            counter = 0
            while len(stream) < len(enc):
                stream += hashlib.pbkdf2_hmac(
                    "sha256", _machine_master_key(), salt + counter.to_bytes(4, "big"), 1, dklen=32
                )
                counter += 1
            return bytes(b ^ stream[i] for i, b in enumerate(enc)).decode("utf-8")
    except Exception:
        return ""
    return stored


def mask_secret(value: Optional[str], keep: int = 3) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}***{text[-keep:]}"


def generate_strong_password(length: int = 24) -> str:
    """Generate a password satisfying SnowLuma's strength rules."""
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits
    symbols = "!@#$%^&*()-_=+[]{}"
    while True:
        body = "".join(secrets.choice(alphabet) for _ in range(max(8, length - 4)))
        candidate = body + secrets.choice(string.ascii_uppercase) + secrets.choice("!@#$%^&*") + secrets.choice("0123456789") + secrets.choice(symbols)
        chars = list(candidate)
        for _ in range(3):
            i = secrets.randbelow(len(chars))
            j = secrets.randbelow(len(chars))
            chars[i], chars[j] = chars[j], chars[i]
        candidate = "".join(chars)
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(not c.isalnum() for c in candidate)
        ):
            return candidate


# --------------------------------------------------------------------------- #
# JSON / filesystem helpers
# --------------------------------------------------------------------------- #


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: Path, data: Any, indent: int = 2) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning(f"[{PLUGIN_NAME}] write json failed {path}: {e}")
        return False


def human_size(num: float) -> str:
    try:
        n = float(num)
    except Exception:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0:
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


# --------------------------------------------------------------------------- #
# Platform / release assets
# --------------------------------------------------------------------------- #


def platform_asset_key() -> Optional[str]:
    """Map the current host to SnowLuma's release asset platform slug."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        return None
    if IS_WINDOWS:
        return f"win-{arch}"
    if IS_LINUX:
        return f"linux-{arch}"
    return None


def archive_extension() -> str:
    return ".zip" if IS_WINDOWS else ".tar.gz"


def release_asset_name(tag: str, flavor: str) -> Optional[str]:
    key = platform_asset_key()
    if not key:
        return None
    suffix = "-lite" if (flavor or "").lower() == "lite" else ""
    return f"SnowLuma-{tag}-{key}{suffix}{archive_extension()}"


def node_binary_candidates(root: Path) -> List[Path]:
    if IS_WINDOWS:
        return [root / "node.exe", root / "node", root / "runtime" / "node.exe"]
    return [root / "node", root / "bin" / "node", root / "runtime" / "node"]


def find_node_executable(root: Optional[Path] = None) -> Optional[str]:
    if root is not None:
        for candidate in node_binary_candidates(root):
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    for name in ("node", "nodejs"):
        found = shutil.which(name)
        if found:
            return found
    return None


def node_version_tuple(binary: str) -> Optional[Tuple[int, int, int]]:
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", (proc.stdout or "") + (proc.stderr or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_supported_node(version: Optional[Tuple[int, int, int]]) -> bool:
    """Mirror of SnowLuma's ``isSupportedNodeVersion``."""
    if not version:
        return False
    major, minor, patch = version
    if major == 24:
        return minor > 22 or (minor == 22 and patch >= 1)
    if major == 26:
        return minor > 12 or (minor == 12 and patch >= 1)
    return major > 26


def node_version_text(version: Optional[Tuple[int, int, int]]) -> str:
    if not version:
        return ""
    return "v" + ".".join(str(part) for part in version)


# --------------------------------------------------------------------------- #
# Processes / ports
# --------------------------------------------------------------------------- #


def list_pids_by_name(name: str) -> List[int]:
    pids: List[int] = []
    if IS_WINDOWS:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=12,
            ).decode("utf-8", errors="ignore")
        except Exception:
            return pids
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("INFO:"):
                continue
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1].strip('"')))
                except ValueError:
                    continue
        return pids
    try:
        out = subprocess.check_output(["pgrep", "-x", name], stderr=subprocess.DEVNULL, timeout=10).decode()
        for line in out.splitlines():
            if line.strip().isdigit():
                pids.append(int(line.strip()))
    except Exception:
        pass
    return pids


def kill_pid_tree(pid: int) -> None:
    if not pid or pid <= 0:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=15,
            )
        except Exception:
            pass
        return
    try:
        os.kill(pid, 15)
        time.sleep(0.4)
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    except Exception:
        pass


def port_is_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def find_available_port(start: int, host: str = "127.0.0.1", tries: int = 50) -> int:
    port = max(1, min(65535, int(start)))
    for _ in range(max(1, tries)):
        if port > 65535:
            break
        if not port_is_open(host, port):
            return port
        port += 1
    return int(start)


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


def make_ssl_context(strict: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not strict:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def normalize_base_url(host: str, port: int) -> str:
    host = (host or "127.0.0.1").strip()
    if host.startswith("http://") or host.startswith("https://"):
        base = host.rstrip("/")
        return base
    return f"http://{host}:{int(port)}"


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def shorten(text: str, limit: int = 200) -> str:
    text = str(text or "").replace("\r", "").strip()
    return text if len(text) <= limit else text[:limit] + "…"
