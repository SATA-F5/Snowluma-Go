"""Download / verify / install SnowLuma release archives.

SnowLuma publishes per-platform bundles on GitHub Releases:

    SnowLuma-<tag>-win-x64[-lite].zip
    SnowLuma-<tag>-linux-x64[-lite].tar.gz
    SnowLuma-<tag>-linux-arm64[-lite].tar.gz

``full`` bundles ship a Node runtime (``node.exe`` / ``node``), ``lite``
bundles do not and therefore require a system Node.js matching
``^24.22.1 || >=26.12.1``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from .utils import (
    GITHUB_API,
    GITHUB_MIRRORS,
    IS_WINDOWS,
    SNOWLUMA_RELEASE_BASE,
    archive_extension,
    find_node_executable,
    is_supported_node,
    make_ssl_context,
    node_version_text,
    node_version_tuple,
    platform_asset_key,
    release_asset_name,
    shorten,
)

# Safe extraction limits: SnowLuma archives are ~100–120MB unpacked; refuse
# archives that would explode far beyond that.
MAX_ARCHIVE_MEMBERS = 20_000
MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024

# A mirror that starts fast and then crawls can otherwise hold the install
# forever: aiohttp's sock_read timeout only fires when *no* bytes arrive, and
# the total timeout is unlimited. Abort any source that stays below this
# rolling speed after a short grace period and move to the next mirror.
MIN_DOWNLOAD_KBPS = 20.0
LOW_SPEED_GRACE_SECONDS = 15.0
SPEED_WINDOW_SECONDS = 8.0


class SnowLumaDownloadMixin:
    """Installing / upgrading SnowLuma from GitHub Releases."""

    # ------------------------------------------------------------------ #
    # Release metadata
    # ------------------------------------------------------------------ #

    async def _github_json(self, url: str) -> Any:
        ssl_ctx = make_ssl_context(strict=True)
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=25, sock_connect=10),
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "snowluma-go-plugin",
                },
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"GitHub API HTTP {resp.status}")
                return await resp.json()

    async def list_releases(self, force: bool = False) -> List[Dict[str, Any]]:
        """List recent releases, annotating which ones ship our platform assets."""
        now = time.time()
        cached = getattr(self, "_releases_cache", None)
        if cached and not force and now - cached[0] < 600:
            return cached[1]

        key = platform_asset_key()
        out: List[Dict[str, Any]] = []
        try:
            data = await self._github_json(f"{GITHUB_API}/releases?per_page=30")
        except Exception as e:
            self._log(f"[releases] 拉取版本列表失败: {e}", "warning")
            return []

        if not isinstance(data, list):
            return []

        for rel in data:
            if not isinstance(rel, dict):
                continue
            tag = str(rel.get("tag_name") or "").strip()
            if not tag:
                continue
            assets = [str((a or {}).get("name") or "") for a in (rel.get("assets") or [])]
            full_name = f"SnowLuma-{tag}-{key}{archive_extension()}" if key else ""
            lite_name = f"SnowLuma-{tag}-{key}-lite{archive_extension()}" if key else ""
            out.append(
                {
                    "tag": tag,
                    "name": str(rel.get("name") or tag),
                    "published_at": str(rel.get("published_at") or ""),
                    "prerelease": bool(rel.get("prerelease")),
                    "body": shorten(str(rel.get("body") or ""), 500),
                    "has_full": full_name in assets,
                    "has_lite": lite_name in assets,
                    "assets": assets,
                }
            )

        self._releases_cache = (now, out)
        return out

    async def resolve_release(self, version: Optional[str] = None) -> Dict[str, Any]:
        """Resolve ``version`` (``latest`` or a tag) into a concrete asset."""
        version = str(version or "latest").strip() or "latest"
        api_url = f"{GITHUB_API}/releases/latest" if version == "latest" else f"{GITHUB_API}/releases/tags/{version}"

        flavor = str(self.plugin.config.get("release_flavor", "full") or "full").lower()
        result: Dict[str, Any] = {
            "tag": version,
            "flavor": flavor,
            "asset_name": None,
            "asset_url": None,
            "asset_size": 0,
            "published_at": "",
            "platform": platform_asset_key(),
        }

        try:
            data = await self._github_json(api_url)
        except Exception as e:
            self._log(f"[releases] [warn] GitHub API 解析版本失败（{e}），回退直链", "warning")
            data = {}

        tag = str(data.get("tag_name") or version).strip() or version
        result["tag"] = tag
        result["published_at"] = str(data.get("published_at") or "")

        assets = [a for a in (data.get("assets") or []) if isinstance(a, dict)]
        desired = release_asset_name(tag, flavor)
        chosen: Optional[Dict[str, Any]] = None

        for asset in assets:
            if str(asset.get("name") or "") == desired:
                chosen = asset
                break

        if chosen is None and assets:
            # Requested flavor missing (e.g. only lite published): fall back to
            # the other flavor and tell the user.
            alternative = release_asset_name(tag, "lite" if flavor == "full" else "full")
            for asset in assets:
                if str(asset.get("name") or "") == alternative:
                    chosen = asset
                    result["flavor"] = "lite" if flavor == "full" else "full"
                    self._log(
                        f"[releases] [warn] 该版本没有 {flavor} 包，改用 {result['flavor']} 包",
                        "warning",
                    )
                    break

        if platform_asset_key() is None:
            raise RuntimeError(
                f"SnowLuma 没有当前平台（{os.name}/{os.uname().machine if hasattr(os, 'uname') else '?'}）的发行包"
            )

        if chosen is not None:
            result["asset_name"] = str(chosen.get("name"))
            result["asset_url"] = str(chosen.get("browser_download_url") or "")
            try:
                result["asset_size"] = int(chosen.get("size") or 0)
            except (TypeError, ValueError):
                result["asset_size"] = 0
        else:
            name = release_asset_name(tag, result["flavor"])
            result["asset_name"] = name
            result["asset_url"] = f"{SNOWLUMA_RELEASE_BASE}/{tag}/{name}"
            if assets:
                self._log(
                    f"[releases] [warn] 未在资产列表中找到 {name}，按命名规则拼出直链",
                    "warning",
                )
        return result

    # ------------------------------------------------------------------ #
    # Download
    # ------------------------------------------------------------------ #

    def _reset_download_state(self) -> None:
        self.download_state.update(
            {
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
        )

    def _mirror_prefixes(self) -> List[str]:
        """Return download prefixes: user mirror, NapCat_Go order, official.

        The built-in candidates intentionally mirror the NapCat_Go plugin's
        list and order, so both plugins reach for the fastest mirror first.
        """
        user = str(self.plugin.config.get("download_mirror", "") or "").strip()
        prefixes: List[str] = []

        def add(prefix: str) -> None:
            normalized = prefix.rstrip("/") + "/" if prefix else ""
            if normalized and normalized not in prefixes:
                prefixes.append(normalized)

        # A mirror that worked for the previous install/update is the best
        # first guess for the next one (still only for this plugin process).
        add(str(getattr(self, "_last_good_mirror", "") or ""))
        if user:
            add(user)
        for mirror in GITHUB_MIRRORS:
            add(mirror)
        prefixes.append("")  # official last
        return prefixes

    async def _download_to_file(
        self,
        url: str,
        dest: Path,
        label: str,
        index: int,
        total: int,
        expected_size: int = 0,
    ) -> bool:
        ssl_ctx = make_ssl_context(strict=(label == "official"))
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30),
                    headers={"User-Agent": "snowluma-go-plugin"},
                ) as resp:
                    if resp.status != 200:
                        self._log(f"[download] {label} HTTP {resp.status}，换下一个源")
                        return False
                    content_length = int(resp.content_length or 0)
                    # GitHub's asset metadata usually has the exact size even
                    # when a mirror omits Content-Length; use it so the UI
                    # doesn't sit at 0% for the whole download.
                    expected = content_length or max(0, int(expected_size or 0))
                    received = 0
                    started = time.time()
                    last_report = started
                    window_started = started
                    window_received = 0
                    with open(dest, "wb") as fh:
                        async for chunk in resp.content.iter_chunked(128 * 1024):
                            fh.write(chunk)
                            received += len(chunk)
                            now = time.time()
                            if now - last_report >= 0.4:
                                elapsed = max(0.001, now - started)
                                self.download_state.update(
                                    {
                                        "downloaded_mb": round(received / 1048576, 2),
                                        "speed_kbps": round(received / elapsed / 1024, 1),
                                        "current_mirror": label,
                                    }
                                )
                                if expected > 0:
                                    self.download_state["total_mb"] = round(expected / 1048576, 2)
                                    self.download_state["percent"] = min(99, received * 100 // expected)
                                last_report = now
                            if now - window_started >= SPEED_WINDOW_SECONDS:
                                window_speed = (received - window_received) / max(
                                    0.001, now - window_started
                                )
                                if now - started >= LOW_SPEED_GRACE_SECONDS and (
                                    window_speed / 1024 < MIN_DOWNLOAD_KBPS
                                ):
                                    self._log(
                                        f"[download] {label} 速度过低"
                                        f"（{window_speed / 1024:.1f}KB/s），换下一个源",
                                        "warning",
                                    )
                                    return False
                                window_started = now
                                window_received = received
                    if expected > 0 and received != expected:
                        self._log(
                            f"[download] {label} 大小不符（{received}/{expected}），丢弃",
                            "warning",
                        )
                        return False
                    return True
        except Exception as e:
            self._log(f"[download] {label} 下载失败: {shorten(str(e), 160)}")
            return False

    # ------------------------------------------------------------------ #
    # Install
    # ------------------------------------------------------------------ #

    async def install_release(self, version: Optional[str] = None, flavor: Optional[str] = None) -> bool:
        """Download and install a release into ``self.install_dir``.

        User state (``config/``, ``data/``, ``logs/`` …) is preserved: only the
        top-level entries shipped inside the new archive are replaced.
        """
        if getattr(self, "_downloading", False):
            self.download_state["message"] = "已有下载任务在进行"
            return False
        if self.is_running_check():
            self._log("[install] [err] SnowLuma 正在运行，请先停止后再安装", "error")
            return False

        if flavor:
            flavor = flavor.lower()
            if flavor not in ("full", "lite"):
                flavor = "full"
            self.plugin.config["release_flavor"] = flavor

        self._downloading = True
        self._reset_download_state()
        self.download_state.update(
            {
                "downloading": True,
                "phase": "resolving",
                "message": "正在解析 SnowLuma 版本信息",
                "current_mirror": "GitHub API",
            }
        )

        staging: Optional[Path] = None
        archive: Optional[Path] = None
        try:
            resolved = await self.resolve_release(version or self.plugin.config.get("snowluma_version"))
            tag = resolved["tag"]
            asset_name = resolved.get("asset_name") or ""
            asset_url = resolved.get("asset_url") or ""
            if not asset_url:
                raise RuntimeError("无法解析下载地址")
            try:
                asset_size = max(0, int(resolved.get("asset_size") or 0))
            except (TypeError, ValueError):
                asset_size = 0

            self.download_state.update(
                {
                    "phase": "downloading",
                    "message": f"正在下载 {asset_name}",
                    "percent": 0,
                    "downloaded_mb": 0.0,
                    "speed_kbps": 0.0,
                    "total_mb": round(asset_size / 1048576, 2) if asset_size else 0.0,
                }
            )
            self._log(f"[install] 目标版本 {tag}（{self.download_state['message']}）")

            downloads = self.persistent_dir / "downloads"
            downloads.mkdir(parents=True, exist_ok=True)
            archive = downloads / asset_name
            if archive.exists():
                try:
                    archive.unlink()
                except Exception:
                    pass

            prefixes = self._mirror_prefixes()
            self.download_state["total_mirrors"] = len(prefixes)
            verified = False
            for idx, prefix in enumerate(prefixes, 1):
                url = asset_url if not prefix else f"{prefix.rstrip('/')}/{asset_url}"
                label = prefix or "official"
                self.download_state.update(
                    {
                        "current_index": idx,
                        "current_mirror": label,
                        "percent": 0,
                        "downloaded_mb": 0.0,
                        "speed_kbps": 0.0,
                    }
                )
                self._log(f"[download] [{idx}/{len(prefixes)}] 尝试源: {label}")
                if not await self._download_to_file(
                    url, archive, label, idx, len(prefixes), expected_size=asset_size
                ):
                    try:
                        if archive.exists():
                            archive.unlink()
                    except Exception:
                        pass
                    continue
                # A captive portal / mirror error page may still answer 200, so
                # validate every downloaded file before accepting a source.
                self.download_state.update({"phase": "verifying", "message": "校验压缩包", "percent": 100})
                if await self._verify_archive(archive):
                    verified = True
                    self._last_good_mirror = prefix
                    break
                try:
                    archive.unlink()
                except Exception:
                    pass
            if not verified:
                raise RuntimeError("所有下载源均失败或压缩包校验不通过")

            digest = self._sha256_file(archive)
            self._log(f"[download] [ok] 下载完成 {asset_name}（SHA256 {digest[:16]}…）")

            self.download_state.update({"phase": "extracting", "message": "解压安装包"})
            staging = self.persistent_dir / "downloads" / f".staging-{int(time.time())}"
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._extract_archive, archive, staging)

            payload_root = self._locate_payload_root(staging)
            if payload_root is None:
                raise RuntimeError("压缩包内未找到 index.mjs，可能不是完整的 SnowLuma 发行包")

            self.download_state.update({"phase": "installing", "message": "写入安装目录"})
            await asyncio.to_thread(self._apply_payload, payload_root)

            # Remember the installed release metadata for the UI.
            release_meta = self.read_installed_release()
            installed_tag = str(release_meta.get("version") or tag)
            self.plugin.config["snowluma_version"] = tag
            raw = str(self.plugin.config.get("release_flavor", "full") or "full").lower()
            self.plugin.config["release_flavor"] = "lite" if raw == "lite" else "full"
            self._installed_version = installed_tag
            try:
                overrides = getattr(self.plugin, "_overrides", None)
                if isinstance(overrides, set):
                    overrides.update({"release_flavor", "snowluma_version"})
                self.plugin._save_config(self.plugin.config)
            except Exception:
                pass
            self._node_cache = None
            self._snapshot_ts = 0.0

            self.download_state.update({"phase": "done", "percent": 100, "message": "安装完成"})
            self._log(f"[install] [ok] SnowLuma {tag} 安装完成: {self.install_dir}")
            return True

        except Exception as e:
            self.download_state.update({"phase": "failed", "message": str(e)})
            self._log(f"[install] [err] 安装失败: {e}", "error")
            record = getattr(self, "_record_error", None)
            if callable(record):
                record(f"安装失败: {e}")
            return False
        finally:
            self._downloading = False
            self.download_state["downloading"] = False
            for path in (staging,):
                if path and path.exists():
                    shutil.rmtree(path, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Archive helpers (blocking; called via asyncio.to_thread)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 256), b""):
                digest.update(chunk)
        return digest.hexdigest()

    async def _verify_archive(self, archive: Path) -> bool:
        def _check() -> bool:
            try:
                if archive.name.lower().endswith(".zip"):
                    with zipfile.ZipFile(archive) as zf:
                        bad = zf.testzip()
                        if bad is not None:
                            self._log(f"[install] [err] zip 内文件损坏: {bad}", "error")
                            return False
                        names = zf.namelist()
                else:
                    with tarfile.open(archive, "r:gz") as tf:
                        members = tf.getmembers()
                        if len(members) > MAX_ARCHIVE_MEMBERS:
                            self._log("[install] [err] 压缩包条目过多，拒绝解压", "error")
                            return False
                        total = sum(m.size for m in members if m.isfile())
                        if total > MAX_UNPACKED_BYTES:
                            self._log("[install] [err] 解压体积异常，拒绝解压", "error")
                            return False
                        names = [m.name for m in members]
                if not any(n.replace("\\", "/").endswith("index.mjs") for n in names):
                    self._log("[install] [err] 压缩包内没有 index.mjs", "error")
                    return False
                return True
            except Exception as e:
                self._log(f"[install] [err] 压缩包无效: {e}", "error")
                return False

        return await asyncio.to_thread(_check)

    def _extract_archive(self, archive: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        if archive.name.lower().endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    self._safe_zip_member(info, target, zf)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                try:
                    tf.extractall(target, filter="data")  # Python 3.12+
                except TypeError:
                    for member in tf.getmembers():
                        if member.name.startswith(("/", "\\")) or ".." in Path(member.name).parts:
                            continue
                        tf.extract(member, target)
        # Ensure launcher / node are executable on POSIX.
        if not IS_WINDOWS:
            for path in target.rglob("*"):
                if path.is_file() and (path.name in ("launcher.sh", "node") or path.suffix == ".sh"):
                    try:
                        path.chmod(path.stat().st_mode | 0o555)
                    except Exception:
                        pass

    @staticmethod
    def _safe_zip_member(info: zipfile.ZipInfo, target: Path, zf: zipfile.ZipFile) -> None:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            return
        destination = target / name
        try:
            destination.resolve().relative_to(target.resolve())
        except Exception:
            return
        if name.endswith("/"):
            destination.mkdir(parents=True, exist_ok=True)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(destination, "wb") as dst:
            shutil.copyfileobj(src, dst)

    @staticmethod
    def _locate_payload_root(staging: Path) -> Optional[Path]:
        def is_root(path: Path) -> bool:
            try:
                return (path / "index.mjs").is_file()
            except OSError:
                return False

        if is_root(staging):
            return staging
        try:
            children = [c for c in staging.iterdir() if c.is_dir()]
        except OSError:
            return None
        if len(children) == 1 and is_root(children[0]):
            return children[0]
        for child in children:
            if is_root(child):
                return child
        for child in children:
            try:
                for sub in child.iterdir():
                    if sub.is_dir() and is_root(sub):
                        return sub
            except OSError:
                continue
        return None

    def _apply_payload(self, payload_root: Path) -> None:
        """Move a fresh payload over the install dir, preserving user state."""
        self.install_dir.mkdir(parents=True, exist_ok=True)
        payload_names = {entry.name for entry in payload_root.iterdir()}

        # Remove what the new release ships plus what the previous release
        # declared, but never touch user-created files (config/, data/, logs/…).
        managed = set(payload_names)
        try:
            import json

            old_release = self.install_dir / "release.json"
            if old_release.is_file():
                with open(old_release, "r", encoding="utf-8-sig") as fh:
                    old_meta = json.load(fh)
                entries = old_meta.get("entries") if isinstance(old_meta, dict) else None
                if isinstance(entries, list):
                    managed.update(str(name).split("/")[0] for name in entries if name)
        except Exception:
            pass

        # Forget the old release.json's *contents* before deleting it; the new
        # payload supplies a fresh one.
        for entry in list(self.install_dir.iterdir()):
            if entry.name not in managed:
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
            except Exception as e:
                self._log(f"[install] [warn] 清理旧文件 {entry.name} 失败: {e}", "warning")

        for entry in payload_root.iterdir():
            destination = self.install_dir / entry.name
            try:
                if destination.exists():
                    if destination.is_dir():
                        shutil.rmtree(destination, ignore_errors=True)
                    else:
                        destination.unlink()
            except Exception:
                pass
            shutil.move(str(entry), str(destination))

        if not IS_WINDOWS:
            for path in self.install_dir.rglob("*"):
                if path.is_file() and (path.name in ("launcher.sh", "node") or path.suffix == ".sh"):
                    try:
                        path.chmod(path.stat().st_mode | 0o555)
                    except Exception:
                        pass

    # ------------------------------------------------------------------ #
    # Installed release info
    # ------------------------------------------------------------------ #

    def read_installed_release(self) -> Dict[str, Any]:
        data = {}
        path = self.install_dir / "release.json"
        if path.is_file():
            try:
                import json

                with open(path, "r", encoding="utf-8-sig") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    data.update(loaded)
            except Exception:
                pass
        if not data:
            pkg = self.install_dir / "package.json"
            if pkg.is_file():
                try:
                    import json

                    with open(pkg, "r", encoding="utf-8-sig") as fh:
                        loaded = json.load(fh)
                    if isinstance(loaded, dict):
                        data.update(
                            {
                                "version": loaded.get("version"),
                                "name": loaded.get("name"),
                                "target": None,
                            }
                        )
                except Exception:
                    pass
        return data

    # ------------------------------------------------------------------ #
    # Node runtime diagnostics
    # ------------------------------------------------------------------ #

    def resolve_node_runtime(self) -> Dict[str, Any]:
        bundled = None
        for candidate in (
            self.install_dir / ("node.exe" if IS_WINDOWS else "node"),
            self.install_dir / "bin" / "node",
        ):
            try:
                if candidate.is_file():
                    bundled = str(candidate)
                    break
            except OSError:
                continue
        system = find_node_executable(None)
        chosen = bundled or system
        version = node_version_tuple(chosen) if chosen else None
        return {
            "bundled": bundled,
            "system": system,
            "chosen": chosen,
            "version": version,
            "version_text": node_version_text(version),
            "supported": is_supported_node(version),
        }
