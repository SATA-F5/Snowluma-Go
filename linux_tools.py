"""Small Linux-only helpers: sudo commands and QQ permission repair."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .utils import IS_LINUX, decrypt_secret, shorten


def sudo_password(plugin: Any) -> str:
    raw = str(plugin.config.get("sudo_password", "") or "")
    return decrypt_secret(raw) if raw else ""


def sudo_available() -> bool:
    return bool(shutil.which("sudo"))


async def run_sudo(plugin: Any, args: List[str], timeout: float = 180.0) -> Tuple[int, str]:
    if not IS_LINUX:
        return 1, "该操作仅支持 Linux"
    if not sudo_available():
        return 1, "系统没有 sudo"
    password = sudo_password(plugin)
    if not password:
        return 1, "未配置 sudo 密码"
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo",
            "-S",
            "-p",
            "",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as exc:
        return 1, f"启动 sudo 失败: {shorten(str(exc), 160)}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate((password + "\n").encode()), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 1, "sudo 命令超时"
    text = (out or b"").decode("utf-8", errors="ignore").strip()
    return int(proc.returncode or 0), text or "(无输出)"


async def fix_qq_permissions(plugin: Any) -> Dict[str, Any]:
    """Fix the common Linux hook blockers: QQ config ownership and ptrace."""
    if not IS_LINUX:
        return {"ok": False, "message": "该操作仅支持 Linux"}
    if not sudo_password(plugin):
        return {"ok": False, "message": "请先在配置里填写 sudo 密码"}

    uid = os.getuid()
    gid = os.getgid()
    qq_config = Path.home() / ".config" / "QQ"
    steps: List[Dict[str, Any]] = []
    ok = True

    if qq_config.exists():
        for args in (
            ["chown", "-R", f"{uid}:{gid}", str(qq_config)],
            ["chmod", "-R", "u+rwX", str(qq_config)],
        ):
            code, output = await run_sudo(plugin, args)
            steps.append({"command": " ".join(args), "ok": code == 0, "output": output})
            ok = ok and code == 0
    else:
        steps.append({"command": f"ls {qq_config}", "ok": True, "output": "目录不存在，跳过"})

    # SnowLuma is a sibling of QQ, not its parent. Under Yama ptrace_scope=1
    # it cannot read /proc/<qq-pid>/environ, which breaks the native hook.
    code, output = await run_sudo(plugin, ["sysctl", "-w", "kernel.yama.ptrace_scope=0"])
    steps.append({"command": "sysctl -w kernel.yama.ptrace_scope=0", "ok": code == 0, "output": output})
    if code == 0:
        code, output = await run_sudo(
            plugin,
            [
                "sh",
                "-c",
                "echo 'kernel.yama.ptrace_scope=0' > "
                "/etc/sysctl.d/99-snowluma-ptrace.conf",
            ],
        )
        steps.append(
            {
                "command": "写入 /etc/sysctl.d/99-snowluma-ptrace.conf",
                "ok": code == 0,
                "output": output,
            }
        )
    ok = ok and code == 0

    if ok:
        message = "QQ 权限与 ptrace 设置已修复，请重启 QQ 后再挂载"
    else:
        failed = next((step for step in steps if not step["ok"]), None)
        detail = ""
        if failed:
            detail = f"（{failed['command']}: {shorten(str(failed['output']), 160)}）"
        message = "部分修复失败" + detail
    return {"ok": ok, "message": message, "steps": steps}


async def install_linux_deps(plugin: Any) -> Dict[str, Any]:
    if not IS_LINUX:
        return {"ok": False, "message": "该操作仅支持 Linux"}
    if not sudo_password(plugin):
        return {"ok": False, "message": "请先在配置里填写 sudo 密码"}
    apt = shutil.which("apt-get")
    if not apt:
        return {"ok": False, "message": "未找到 apt-get（仅支持 Debian / Ubuntu）"}

    steps: List[Dict[str, Any]] = []
    code, output = await run_sudo(plugin, ["apt-get", "update"], timeout=300)
    steps.append({"command": "apt-get update", "ok": code == 0, "output": output})
    code, output = await run_sudo(
        plugin,
        [
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "install",
            "-y",
            "xvfb",
            "x11vnc",
            "openbox",
            "scrot",
            "imagemagick",
            "x11-utils",
        ],
        timeout=600,
    )
    steps.append(
        {
            "command": "apt-get install -y xvfb x11vnc openbox scrot imagemagick x11-utils",
            "ok": code == 0,
            "output": output,
        }
    )
    missing = [
        name
        for name in ("Xvfb", "x11vnc", "openbox", "xwininfo")
        if not shutil.which(name)
    ]
    if not any(shutil.which(name) for name in ("scrot", "import", "ffmpeg")):
        missing.append("scrot/imagemagick")
    if missing:
        steps.append(
            {
                "command": "检查依赖",
                "ok": False,
                "output": "缺少: " + ", ".join(missing),
            }
        )
    # ``apt-get update`` can fail on a stale mirror while the install command
    # still succeeds from cache; only the install result + binaries matter.
    ok = code == 0 and not missing
    return {
        "ok": ok,
        "message": "Linux 依赖安装完成" if ok else "依赖安装失败或缺组件，请查看详情",
        "steps": steps,
    }
