"""Small explicit CLI; no background jobs, automatic installs or credential export."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import struct
import sys
import uuid
import zlib

from . import downloader
from .discovery import _material_tasks, _platform_restriction_code


def data_root() -> Path:
    return Path.cwd() / ".local"


@contextmanager
def browser(runtime: str):
    root = Path(runtime).resolve()
    if not root.is_dir():
        raise RuntimeError("BROWSER_RUNTIME_NOT_FOUND")
    # Inspect the pinned 0.5.x cache layout without invoking package-manager
    # repair/cleanup paths. An arbitrary directory must never be cleaned up.
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
        active = config["active_version"]
        if not isinstance(active, str) or not active:
            raise ValueError
        installed = (root / active).resolve()
        if (not (root / ".0.5_FLAG").is_file()
                or not installed.is_relative_to(root)
                or not (installed / "camoufox.exe").is_file()
                or not (installed / "version.json").is_file()):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("BROWSER_RUNTIME_NOT_INSTALLED") from None
    try:
        import platformdirs
    except ImportError:
        raise RuntimeError("BROWSER_DEPENDENCIES_MISSING") from None
    original = platformdirs.user_cache_dir
    platformdirs.user_cache_dir = lambda appname=None, *a, **kw: (
        str(root) if appname == "camoufox" else original(appname, *a, **kw)
    )
    try:
        # Camoufox imports its cache path once: configure it BEFORE importing it.
        try:
            from camoufox.sync_api import Camoufox
            from camoufox import DefaultAddons
            from camoufox.pkgman import INSTALL_DIR
        except ImportError:
            raise RuntimeError("BROWSER_DEPENDENCIES_MISSING") from None
        if INSTALL_DIR.resolve() != root:
            raise RuntimeError("BROWSER_RUNTIME_ROOT_MISMATCH")
        profile = data_root() / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        with Camoufox(
            headless=False, os="windows", locale="zh-CN",
            persistent_context=True, user_data_dir=str(profile),
            exclude_addons=[DefaultAddons.UBO], enable_cache=True,
        ) as context:
            yield context
    finally:
        platformdirs.user_cache_dir = original


def check_platform(page) -> None:
    code = _platform_restriction_code(
        page.locator("body").inner_text(timeout=10_000), page.url
    )
    if code:
        raise RuntimeError("PLATFORM_BLOCKED:" + code)


def save_tasks(tasks: list[dict], referer: str | None = None) -> dict:
    root = data_root() / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    session = "sessions/" + uuid.uuid4().hex
    previous = os.environ.get("CAMOUFOX_WORK_ROOT")
    os.environ["CAMOUFOX_WORK_ROOT"] = str(root)
    try:
        result = downloader.download_materials({
            "contractVersion": 1, "sessionRelative": session,
            "referer": referer, "tasks": tasks,
        })
    finally:
        if previous is None:
            os.environ.pop("CAMOUFOX_WORK_ROOT", None)
        else:
            os.environ["CAMOUFOX_WORK_ROOT"] = previous
    manifest = root / session / "manifest.json"
    temporary = manifest.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest)
    print(json.dumps({"ok": True, "counts": result["downloadCounts"],
                      "manifest": str(manifest)}, ensure_ascii=True))
    return result


def synthetic_png(red: int, green: int, blue: int) -> bytes:
    """Generate an original 8x8 solid-color PNG without external image files."""
    def chunk(kind: bytes, content: bytes) -> bytes:
        return (struct.pack(">I", len(content)) + kind + content
                + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff))
    pixels = b"".join(b"\0" + bytes([red, green, blue]) * 8 for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def demo() -> dict:
    """Exercise real download/ZIP/hash code with in-memory transport, no network."""
    from io import BytesIO
    from unittest.mock import patch
    images = {
        "/jfs/demo-main.png": synthetic_png(42, 102, 180),
        "/jfs/demo-detail.png": synthetic_png(230, 170, 40),
    }
    class Response(BytesIO):
        status = 200

        def __init__(self, payload):
            super().__init__(payload)
            self.headers = {"Content-Type": "image/png", "Content-Length": str(len(payload))}

    def transport(request, timeout):
        from urllib.parse import urlsplit
        return Response(images[urlsplit(request.full_url).path])

    with patch.object(downloader, "open_media", side_effect=transport):
        return save_tasks([
            {"category": "MAIN_IMAGE", "url": "https://img10.360buyimg.com/jfs/demo-main.png"},
            {"category": "DETAIL_IMAGE", "url": "https://img30.360buyimg.com/jfs/demo-detail.png"},
        ])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="京东商品主图与详情图下载流程示例")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="离线演示：生成两张原创色块图并校验、归档")
    login = commands.add_parser("login", help="在本目录独立浏览器中手动登录")
    login.add_argument("--runtime-root", required=True, help="已安装的 Camoufox 缓存根目录")
    collect = commands.add_parser("collect", help="受控采集一个商品的主图、详情图")
    collect.add_argument("--runtime-root", required=True)
    collect.add_argument("--sku", required=True)
    return result


def main(argv=None) -> int:
    options = parser().parse_args(argv)
    try:
        if options.command == "demo":
            demo()
            return 0
        if options.command == "collect" and not re.fullmatch(r"[0-9]{6,20}", options.sku):
            raise RuntimeError("SKU_INVALID")
        with browser(options.runtime_root) as context:
            page = context.new_page()
            if options.command == "login":
                page.goto("https://www.jd.com/", wait_until="domcontentloaded", timeout=45_000)
                input("请在新浏览器中亲自登录；完成后回到终端按回车关闭。")
                return 0
            url = f"https://item.jd.com/{options.sku}.html"
            response = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            if response and response.status in (401, 403, 429):
                raise RuntimeError(f"PLATFORM_BLOCKED:HTTP_{response.status}")
            check_platform(page)
            page.wait_for_timeout(8_000)
            check_platform(page)
            tasks = [task for task in _material_tasks(page, options.sku)
                     if task["category"] in {"MAIN_IMAGE", "DETAIL_IMAGE"}]
            if not any(t["category"] == "MAIN_IMAGE" for t in tasks):
                raise RuntimeError("MAIN_IMAGES_MISSING")
            if not any(t["category"] == "DETAIL_IMAGE" for t in tasks):
                raise RuntimeError("DETAIL_IMAGES_MISSING")
            check_platform(page)
            save_tasks(tasks, url)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        # Browser exceptions can contain signed URLs. Never print raw exception text.
        code = str(error)
        if not re.fullmatch(r"[A-Z][A-Z0-9_:]{1,120}", code):
            code = "INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "COLLECTION_FAILED"
        print(json.dumps({"ok": False, "error": code}), file=sys.stderr)
        return 1
