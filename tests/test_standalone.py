from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from jd_images import cli, discovery, downloader

(Path(__file__).resolve().parents[1] / ".local").mkdir(exist_ok=True)


class StandaloneTest(unittest.TestCase):
    def test_offline_pipeline_validates_bytes_zip_and_redacted_manifest(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".local") as value:
            root = Path(value)
            with patch.object(cli, "data_root", return_value=root), redirect_stdout(StringIO()):
                result = cli.demo()
            manifest_path = next(root.rglob("manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest, result)
            self.assertEqual(result["downloadCounts"], {"MAIN_IMAGE": 1, "MAIN_VIDEO": 0, "DETAIL_IMAGE": 1})
            text = manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("https://", text)
            self.assertNotIn("cookie", text.lower())
            archive_path = root / "downloads" / result["rawZipTemporaryPath"]
            self.assertEqual(hashlib.sha256(archive_path.read_bytes()).hexdigest(), result["rawZipSha256"])
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(len(archive.namelist()), 2)
                for record in result["files"]:
                    data = archive.read(record["sourcePath"])
                    self.assertEqual(hashlib.sha256(data).hexdigest(), record["sha256"])
                    self.assertEqual(len(data), record["byteLength"])
                    self.assertEqual(data, (root / "downloads" / record["temporaryPath"]).read_bytes())

    def test_failure_does_not_publish_manifest(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".local") as value:
            root = Path(value)
            with patch.object(cli, "data_root", return_value=root):
                with patch.object(downloader, "open_media", side_effect=TimeoutError):
                    with self.assertRaisesRegex(RuntimeError, "MATERIAL_DOWNLOAD_TIMEOUT"):
                        cli.save_tasks([{"category": "MAIN_IMAGE", "url": "https://img10.360buyimg.com/jfs/a.jpg"}])
            self.assertEqual(list(root.rglob("manifest.json")), [])

    def test_rejects_path_traversal_and_overwriting_sessions(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".local") as value:
            root = Path(value)
            with patch.dict(os.environ, {"CAMOUFOX_WORK_ROOT": value}):
                command = {"contractVersion": 1, "sessionRelative": "../../outside", "tasks": []}
                with self.assertRaisesRegex(RuntimeError, "PATH_INVALID"):
                    downloader.download_materials(command)
                (root / "sessions" / "same").mkdir(parents=True)
                command["sessionRelative"] = "sessions/same"
                with self.assertRaisesRegex(RuntimeError, "SESSION_EXISTS"):
                    downloader.download_materials(command)

    def test_rejects_non_https_local_hosts_and_untrusted_redirects(self):
        for url in ("http://img10.360buyimg.com/a.jpg", "https://localhost/a.jpg",
                    "https://127.0.0.1/a.jpg", "https://img10.360buyimg.com.evil.example/a.jpg",
                    "https://user:password@img10.360buyimg.com/a.jpg"):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                downloader.validate_url(url)
        from urllib.request import Request
        with self.assertRaises(RuntimeError):
            downloader.MediaRedirect().redirect_request(
                Request("https://img10.360buyimg.com/a.jpg"), None, 302, "Found", {}, "https://evil.example/a.jpg"
            )

    def test_main_order_and_detail_deduplication_are_preserved(self):
        class Page:
            def evaluate(self, script):
                return {"main": ["//img10.360buyimg.com/n5/jfs/a.jpg"] * 2, "video": [], "detail": []}
        with patch.object(discovery, "_detail_image_urls", return_value=["https://img30.360buyimg.com/jfs/b.jpg"]):
            result = discovery._material_tasks(Page(), "123456789012")
        self.assertEqual([r["category"] for r in result], ["MAIN_IMAGE", "MAIN_IMAGE", "DETAIL_IMAGE"])
        self.assertEqual(result[0]["url"], "https://img10.360buyimg.com/pcpubliccms/s800x800_jfs/a.jpg")

    def test_detail_parser_filters_domains_and_deduplicates_queries(self):
        class Page:
            def wait_for_function(self, *args, **kwargs):
                pass
            def evaluate(self, script, sku):
                return ('<img src="//img30.360buyimg.com/jfs/b.jpg?a=1">'
                        '<img src="https://img30.360buyimg.com/jfs/b.jpg?a=2">'
                        '<img src="https://evil.example/jfs/c.jpg">')
        self.assertEqual(discovery._detail_image_urls(Page(), "123456789012"),
                         ["https://img30.360buyimg.com/jfs/b.jpg?a=1"])

    def test_invalid_sku_does_not_open_browser(self):
        from contextlib import redirect_stderr
        with patch.object(cli, "browser") as start, redirect_stderr(StringIO()):
            status = cli.main(["collect", "--runtime-root", "unused", "--sku", "../not-a-sku"])
        self.assertEqual(status, 1)
        start.assert_not_called()

    def test_platform_restrictions_are_detected(self):
        self.assertEqual(discovery._platform_restriction_code("验证码"), "CAPTCHA")
        self.assertEqual(discovery._platform_restriction_code("", "https://passport.jd.com/login"), "LOGIN_REQUIRED")
        self.assertIsNone(discovery._platform_restriction_code("普通商品说明"))

    def test_missing_runtime_does_not_import_or_download_browser(self):
        with self.assertRaisesRegex(RuntimeError, "BROWSER_RUNTIME_NOT_FOUND"):
            with cli.browser(str(Path.cwd() / ".local" / "not-installed")):
                self.fail("Missing runtime must not launch")

    def test_arbitrary_runtime_directory_is_not_cleaned_or_modified(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".local") as value:
            marker = Path(value) / "keep.txt"
            marker.write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "BROWSER_RUNTIME_NOT_INSTALLED"):
                with cli.browser(value):
                    self.fail("Arbitrary directory must not launch")
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
            self.assertEqual([p.name for p in Path(value).iterdir()], ["keep.txt"])


if __name__ == "__main__":
    unittest.main()
