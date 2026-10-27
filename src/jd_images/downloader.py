"""Small standalone media downloader for Camoufox material tasks.

The process accepts one versioned JSON task on stdin and writes one JSON result
to stdout. URLs are used only while the process is running; they are never
written to the result, logs, manifest, or evidence store.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any


CONTRACT_VERSION = 1
PLATFORM_STATUSES = {401, 403, 429}
CHUNK_SIZE = 1024 * 1024
MAX_FILE_BYTES = 512 * CHUNK_SIZE
MAX_TASKS = 200


class DownloadError(RuntimeError):
    """Only controlled codes may cross the process boundary."""


def safe_error(error: Exception) -> str:
    return str(error) if isinstance(error, DownloadError) else "MATERIAL_DOWNLOADER_INTERNAL_ERROR"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def ident(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _safe_relative_path(value: str) -> Path:
    if not re.fullmatch(r"sessions/[A-Za-z0-9_-]{1,160}", value):
        raise DownloadError("MATERIAL_DOWNLOAD_PATH_INVALID")
    return Path(value)


def validate_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        if host in ("passport.jd.com", "plogin.m.jd.com") or re.search(
            r"/(login|captcha|verify)(?:/|\.|$)", parsed.path, re.I
        ):
            raise DownloadError("MATERIAL_PLATFORM_BLOCKED:LOGIN_OR_VERIFY")
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username or parsed.password
                or not any(host.endswith("." + domain) for domain in
                           ("360buyimg.com", "jd.com", "300hu.com"))):
            raise DownloadError("MATERIAL_DOWNLOAD_URL_INVALID")
    except ValueError:
        raise DownloadError("MATERIAL_DOWNLOAD_URL_INVALID") from None


class MediaRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_media(request, timeout):
    # Explicit direct transport: never inherit environment/system proxies or cookies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), MediaRedirect())
    opener.addheaders = []
    return opener.open(request, timeout=timeout)


def media_kind(prefix: bytes) -> str | None:
    if (prefix.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM"))
            or (prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP")):
        return "image"
    if prefix[4:8] == b"ftyp" or prefix.startswith(b"\x1a\x45\xdf\xa3"):
        return "video"
    return None


def _download_suffix(url: str, content_type: str, category: str) -> str:
    path_suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if category == "MAIN_VIDEO":
        return path_suffix if path_suffix in (".mp4", ".webm", ".mov") else ".mp4"
    if path_suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return ".jpg" if path_suffix == ".jpeg" else path_suffix
    mime_suffixes = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }
    lowered = content_type.lower()
    return next(
        (suffix for mime, suffix in mime_suffixes.items() if mime in lowered), ".jpg"
    )


def _headers(referer: str | None) -> dict[str, str]:
    # Keep the request surface small. No Cookie, token, proxy, or random UA is
    # added. Referer is supplied explicitly by the page that discovered the URL.
    headers = {"Accept": "*/*", "Accept-Encoding": "identity"}
    if referer:
        if not re.fullmatch(r"https://item\.jd\.com/[0-9]+\.html", referer):
            raise DownloadError("MATERIAL_DOWNLOAD_REFERER_INVALID")
        headers["Referer"] = referer
    return headers


def _download_one(task: dict[str, Any], target: Path, referer: str | None) -> dict[str, Any]:
    category = str(task.get("category", ""))
    url = str(task.get("url", ""))
    if category not in {"MAIN_IMAGE", "MAIN_VIDEO", "DETAIL_IMAGE"}:
        raise DownloadError("MATERIAL_DOWNLOAD_TASK_INVALID")
    validate_url(url)

    request = urllib.request.Request(url, headers=_headers(referer), method="GET")
    temporary_path = target / "files" / task["filename"]
    temporary_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    byte_length = 0
    content_type = ""
    deadline = time.monotonic() + 120
    try:
        with open_media(request, timeout=30) as response:
            status_value = getattr(response, "status", None)
            status = int(response.getcode() if status_value is None else status_value)
            if status in PLATFORM_STATUSES:
                raise DownloadError(f"MATERIAL_PLATFORM_BLOCKED:HTTP_{status}")
            if status < 200 or status >= 300:
                raise DownloadError(f"MATERIAL_DOWNLOAD_HTTP_{status}")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            expected = "video" if category == "MAIN_VIDEO" else "image"
            if content_type in ("text/html", "application/xhtml+xml"):
                raise DownloadError("MATERIAL_PLATFORM_BLOCKED:HTML_RESPONSE")
            if not content_type.startswith(expected + "/") and content_type != "application/octet-stream":
                raise DownloadError("MATERIAL_DOWNLOAD_CONTENT_TYPE_INVALID")
            length_header = response.headers.get("Content-Length")
            expected_length = int(length_header) if length_header and length_header.isdigit() else None
            if expected_length is not None and expected_length > MAX_FILE_BYTES:
                raise DownloadError("MATERIAL_DOWNLOAD_TOO_LARGE")
            if response.headers.get("Content-Encoding", "identity") not in ("", "identity"):
                raise DownloadError("MATERIAL_DOWNLOAD_ENCODING_INVALID")
            with temporary_path.open("wb") as output:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    if byte_length == 0 and media_kind(chunk) != expected:
                        if chunk.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                            raise DownloadError("MATERIAL_PLATFORM_BLOCKED:HTML_RESPONSE")
                        raise DownloadError("MATERIAL_DOWNLOAD_CONTENT_INVALID")
                    if byte_length + len(chunk) > MAX_FILE_BYTES or time.monotonic() > deadline:
                        raise DownloadError("MATERIAL_DOWNLOAD_LIMIT_EXCEEDED")
                    output.write(chunk)
                    digest.update(chunk)
                    byte_length += len(chunk)
    except urllib.error.HTTPError as error:
        if error.code in PLATFORM_STATUSES:
            raise DownloadError(f"MATERIAL_PLATFORM_BLOCKED:HTTP_{error.code}") from error
        raise DownloadError(f"MATERIAL_DOWNLOAD_HTTP_{error.code}") from error
    except urllib.error.URLError as error:
        raise DownloadError("MATERIAL_DOWNLOAD_NETWORK_ERROR") from error
    except TimeoutError as error:
        raise DownloadError("MATERIAL_DOWNLOAD_TIMEOUT") from error
    if expected_length is not None and byte_length != expected_length:
        raise DownloadError("MATERIAL_DOWNLOAD_LENGTH_MISMATCH")
    if byte_length == 0:
        raise DownloadError("MATERIAL_DOWNLOAD_EMPTY")

    suffix = _download_suffix(url, content_type, category)
    filename = Path(task["filename"]).with_suffix(suffix).name
    if filename != task["filename"]:
        temporary_path.replace(target / "files" / filename)
    source_path = Path(task["sourcePath"]).with_suffix(suffix).as_posix()
    relative_file = (Path(task["relative_file"]).with_suffix(suffix)).as_posix()
    return {
        "category": category,
        "contentType": content_type,
        "sha256": digest.hexdigest(),
        "byteLength": byte_length,
        "temporaryPath": relative_file,
        "sourcePath": source_path,
    }


def download_materials(command: dict[str, Any]) -> dict[str, Any]:
    if command.get("contractVersion") != CONTRACT_VERSION:
        raise DownloadError("MATERIAL_DOWNLOAD_CONTRACT_UNSUPPORTED")
    work_root_value = os.environ.get("CAMOUFOX_WORK_ROOT")
    if not work_root_value:
        raise DownloadError("CAMOUFOX_WORK_ROOT_NOT_CONFIGURED")
    work_root = Path(work_root_value).resolve()
    session_relative = _safe_relative_path(str(command.get("sessionRelative", "")))
    target = (work_root / session_relative).resolve()
    if not target.is_relative_to(work_root):
        raise DownloadError("MATERIAL_DOWNLOAD_PATH_INVALID")
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise DownloadError("MATERIAL_DOWNLOAD_SESSION_EXISTS") from None
    raw_tasks = command.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks or len(raw_tasks) > MAX_TASKS:
        raise DownloadError("NO_RETAINED_MATERIALS")

    tasks: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_tasks, 1):
        if not isinstance(raw, dict):
            raise DownloadError("MATERIAL_DOWNLOAD_TASK_INVALID")
        validate_url(str(raw.get("url", "")))
        category = str(raw.get("category", ""))
        suffix = _download_suffix(str(raw.get("url", "")), "", category)
        filename = f"{index:04d}-{category.lower()}{suffix}"
        source_folder = {
            "MAIN_IMAGE": "主图",
            "MAIN_VIDEO": "主图视频",
            "DETAIL_IMAGE": "详情图",
        }.get(category)
        if source_folder is None:
            raise DownloadError("MATERIAL_DOWNLOAD_TASK_INVALID")
        tasks.append(
            {
                "category": category,
                "url": raw.get("url"),
                "filename": filename,
                "relative_file": (session_relative / "files" / filename).as_posix(),
                "sourcePath": f"{source_folder}/{index:04d}{suffix}",
            }
        )

    files: list[dict[str, Any]] = []
    for task in tasks:
        files.append(_download_one(task, target, command.get("referer")))

    zip_relative = session_relative / "raw-materials.zip"
    zip_path = work_root / zip_relative
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for file in files:
            archive.write(work_root / Path(file["temporaryPath"]), arcname=file["sourcePath"])
    with zip_path.open("rb") as stream:
        zip_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "contractVersion": CONTRACT_VERSION,
        "collectionId": ident("collection"),
        "imageRunId": ident("image-run"),
        "capturedAt": now(),
        "rawZipSha256": zip_digest,
        "downloadCounts": {category: sum(file["category"] == category for file in files)
                           for category in ("MAIN_IMAGE", "MAIN_VIDEO", "DETAIL_IMAGE")},
        "rawZipTemporaryPath": zip_relative.as_posix(),
        "files": files,
    }


def main() -> None:
    try:
        command = json.loads(sys.stdin.read())
        result = download_materials(command)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)
    except Exception as exc:
        print(json.dumps({"error": safe_error(exc)}, separators=(",", ":")), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
