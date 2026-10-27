from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import urllib.error


from jd_images import downloader as DOWNLOADER

(Path(__file__).resolve().parents[1] / ".local").mkdir(exist_ok=True)

class MaterialDownloaderTest(unittest.TestCase):
    def test_downloads_all_categories_and_preserves_hash_zip_and_content_type(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="jd-material-downloader-", dir=Path(__file__).resolve().parents[1] / ".local"))
        try:
            def fake_download(task: dict, target: Path, referer: str | None) -> dict:
                data = {
                    "MAIN_IMAGE": b"main-image",
                    "MAIN_VIDEO": b"main-video",
                    "DETAIL_IMAGE": b"detail-image",
                }[task["category"]]
                output = target / "files" / task["filename"]
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                import hashlib

                return {
                    "category": task["category"],
                    "contentType": "image/jpeg" if task["category"] != "MAIN_VIDEO" else "video/mp4",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "byteLength": len(data),
                    "temporaryPath": task["relative_file"],
                    "sourcePath": task["sourcePath"],
                }

            with patch.dict(DOWNLOADER.os.environ, {"CAMOUFOX_WORK_ROOT": str(root)}):
                with patch.object(DOWNLOADER, "_download_one", side_effect=fake_download):
                    result = DOWNLOADER.download_materials(
                        {
                            "contractVersion": 1,
                            "sessionRelative": "sessions/item-1",
                            "referer": "https://item.jd.com/123456789012.html",
                            "tasks": [
                                {"category": "MAIN_IMAGE", "url": "https://img10.360buyimg.com/main.jpg"},
                                {"category": "MAIN_VIDEO", "url": "https://video.jd.com/video.mp4"},
                                {"category": "DETAIL_IMAGE", "url": "https://img30.360buyimg.com/detail.png"},
                            ],
                        }
                    )
            self.assertEqual([file["category"] for file in result["files"]], [
                "MAIN_IMAGE", "MAIN_VIDEO", "DETAIL_IMAGE"
            ])
            self.assertEqual(result["files"][0]["contentType"], "image/jpeg")
            zip_path = root / result["rawZipTemporaryPath"]
            self.assertTrue(zip_path.is_file())
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(archive.namelist(), ["主图/0001.jpg", "主图视频/0002.mp4", "详情图/0003.png"])
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_download_one_records_content_type_and_referer(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="jd-material-downloader-", dir=Path(__file__).resolve().parents[1] / ".local"))
        try:
            task = {
                "category": "MAIN_IMAGE",
                "url": "https://img10.360buyimg.com/main.jpg",
                "filename": "0001-main_image.jpg",
                "relative_file": "sessions/item-1/files/0001-main_image.jpg",
                "sourcePath": "主图/0001.jpg",
            }
            class FakeResponse:
                status = 200
                headers = {"Content-Type": "image/jpeg"}

                def __enter__(self):
                    return self

                def __exit__(self, *_: object) -> None:
                    return None

                def read(self, _: int) -> bytes:
                    if hasattr(self, "done"):
                        return b""
                    self.done = True
                    return b"\xff\xd8\xff\x00main-image"

            requests: list[object] = []
            with patch.object(DOWNLOADER, "open_media", side_effect=lambda request, timeout: requests.append(request) or FakeResponse()):
                result = DOWNLOADER._download_one(
                    task,
                    root / "sessions" / "item-1",
                    "https://item.jd.com/123456789012.html",
                )
            self.assertEqual(result["contentType"], "image/jpeg")
            self.assertEqual(result["byteLength"], len(b"\xff\xd8\xff\x00main-image"))
            self.assertEqual(requests[0].get_header("Referer"), "https://item.jd.com/123456789012.html")
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_platform_status_is_not_downgraded_to_partial_material(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="jd-material-downloader-", dir=Path(__file__).resolve().parents[1] / ".local"))
        try:
            task = {
                "category": "MAIN_IMAGE",
                "url": "https://img10.360buyimg.com/forbidden",
                "filename": "0001-main_image.jpg",
                "relative_file": "sessions/item-1/files/0001-main_image.jpg",
                "sourcePath": "主图/0001.jpg",
            }
            error = urllib.error.HTTPError(task["url"], 403, "Forbidden", {}, None)
            with patch.object(DOWNLOADER, "open_media", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, "MATERIAL_PLATFORM_BLOCKED:HTTP_403"):
                    DOWNLOADER._download_one(task, root / "sessions" / "item-1", None)
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_rejects_non_cdn_urls_and_html_challenge_responses(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="jd-material-downloader-", dir=Path(__file__).resolve().parents[1] / ".local"))
        task = {
            "category": "MAIN_IMAGE",
            "url": "https://evil.example/image.jpg",
            "filename": "0001-main_image.jpg",
            "relative_file": "sessions/item-1/files/0001-main_image.jpg",
            "sourcePath": "主图/0001.jpg",
        }
        try:
            with self.assertRaisesRegex(RuntimeError, "MATERIAL_DOWNLOAD_URL_INVALID"):
                DOWNLOADER._download_one(task, root / "sessions" / "item-1", None)

            task["url"] = "https://img10.360buyimg.com/image.jpg"

            class HtmlResponse:
                status = 200
                headers = {"Content-Type": "text/html"}

                def __enter__(self):
                    return self

                def __exit__(self, *_: object) -> None:
                    return None

                def read(self, _: int) -> bytes:
                    return b"<html>verify</html>"

            with patch.object(DOWNLOADER, "open_media", return_value=HtmlResponse()):
                with self.assertRaisesRegex(RuntimeError, "MATERIAL_PLATFORM_BLOCKED:HTML_RESPONSE"):
                    DOWNLOADER._download_one(task, root / "sessions" / "item-1", None)
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
