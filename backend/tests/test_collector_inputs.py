import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.integrations.input_routes import create_input_router
from app.integrations.input_service import CollectorInputService, source_url, validate_workbook
from app.integrations.excel_mapping import mapped_workbook, worksheet_info


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=")
LEGACY_ROOT = Path("/Users/xiaoyao/TikTok_Auto")


def workbook():
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="产品" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        rows = []
        for index, values in enumerate((("国家", "keyword", "产品图1"), ("美国", "lipstick", ""), ("法国", "lipstick", "")), 1):
            cells = ''.join(f'<c r="{column}{index}" t="inlineStr"><is><t>{value}</t></is></c>' for column, value in zip("ABC", values))
            rows.append(f'<row r="{index}">{cells}</row>')
        archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheetData>' + ''.join(rows) + '</sheetData><drawing r:id="drawing1"/></worksheet>')
        archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="drawing1" Target="../drawings/drawing1.xml"/></Relationships>')
        anchors = ''.join(f'<xdr:oneCellAnchor><xdr:from><xdr:col>2</xdr:col><xdr:row>{index}</xdr:row></xdr:from><xdr:pic><a:blip r:embed="img1"/></xdr:pic></xdr:oneCellAnchor>' for index in (1, 2))
        archive.writestr("xl/drawings/drawing1.xml", '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' + anchors + '</xdr:wsDr>')
        archive.writestr("xl/drawings/_rels/drawing1.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="img1" Target="../media/image1.png"/></Relationships>')
        archive.writestr("xl/media/image1.png", PNG)
    return target.getvalue()


class CollectorInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(storage_root=Path(self.temp.name), legacy_tiktok_auto_root=LEGACY_ROOT,
                                        legacy_tiktok_auto_python=Path(sys.executable))
        self.collector = SimpleNamespace(create=AsyncMock(return_value={"task_id": "created-id", "rule_id": "rule-id"}))
        self.service = CollectorInputService(self.collector, self.settings)

    def tearDown(self):
        self.temp.cleanup()

    def test_read_only_url_and_draft_boundaries(self):
        self.assertEqual(source_url("https://docs.qq.com/sheet/ABCD?tab=sheet2&access_token=private#secret"), "https://docs.qq.com/sheet/ABCD?tab=sheet2")
        for url in ("http://docs.qq.com/sheet/ABCD", "https://docs.qq.com.attacker/sheet/ABCD", "https://user:password@docs.qq.com/sheet/ABCD"):
            with self.assertRaises(HTTPException):
                source_url(url)
        draft = self.service.draft({"min_views": 500, "filter_watermark": True, "person_integrity": None})
        self.assertFalse(draft["applied"])
        self.assertEqual(self.service.draft()["rules"]["min_views"], 500)
        with self.assertRaises(HTTPException):
            self.service.draft({"api_key": "never-save"})
        self.assertNotIn("never-save", (self.service.root / "rules-draft.json").read_text())

    def test_upload_routes_bound_size_type_and_containment(self):
        app = FastAPI()
        app.include_router(create_input_router(self.collector, self.settings))
        with TestClient(app) as client:
            response = client.post("/api/collector/images", content=PNG, headers={"X-Filename": "product.png"})
            self.assertEqual(response.status_code, 201)
            image = response.json()
            self.assertEqual(client.get(image["url"]).content, PNG)
            self.assertEqual(image["sha256"], hashlib.sha256(PNG).hexdigest())
            self.assertEqual(client.post("/api/collector/images", content=PNG, headers={"X-Filename": "..%2Fproduct.png"}).status_code, 422)
            self.assertEqual(client.post("/api/collector/images", content=b"not-an-image", headers={"X-Filename": "product.png"}).status_code, 422)
            self.assertEqual(client.post("/api/collector/import/excel", content=PNG, headers={"X-Filename": "sheet.csv"}).status_code, 422)
        with self.assertRaises(HTTPException):
            self.service.file("input-" + "a" * 32, "../../.env")

    def test_corrupt_and_oversized_workbook_rejected(self):
        with self.assertRaises(HTTPException):
            validate_workbook(b"not a workbook")
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("xl/workbook.xml", "bad")
        with self.assertRaises(HTTPException):
            validate_workbook(payload.getvalue())

    @unittest.skipUnless((LEGACY_ROOT / "src/excel_input.py").is_file(), "legacy reader unavailable")
    def test_real_legacy_excel_reader_keeps_country_row_images_and_original(self):
        original = workbook()
        config_hash = hashlib.sha256((LEGACY_ROOT / "config.yaml").read_bytes()).hexdigest()
        job = self.service.prepare_import("excel", filename="products.xlsx", payload=original)
        self.service.run_import(job["import_id"], threading.Event())
        result = self.service.read_status(job["import_id"])
        self.assertEqual(result["status"], "READY", result)
        self.assertEqual([row["country"] for row in result["rows"]], ["美国", "法国"])
        self.assertEqual([row["source_row"] for row in result["rows"]], [2, 3])
        self.assertNotEqual(result["rows"][0]["context_id"], result["rows"][1]["context_id"])
        for row in result["rows"]:
            self.assertEqual(row["status"], "READY")
            path = Path(row["product_images"][0])
            self.assertTrue(path.is_relative_to(self.service.root))
            self.assertEqual(path.read_bytes(), PNG)
            self.service.validate_source_context(row)
        self.assertEqual(Path(result["source_document"]).read_bytes(), original)
        self.assertEqual(hashlib.sha256((LEGACY_ROOT / "config.yaml").read_bytes()).hexdigest(), config_hash)
        changed = {**result["rows"][0], "country": "错误归属"}
        with self.assertRaises(HTTPException):
            self.service.validate_source_context(changed)
        self.collector.create.assert_not_called()

    def test_create_batch_validates_before_create_and_never_starts(self):
        image = self.service.save_image(PNG, "product.png")
        output = asyncio.run(self.service.create_batch([
            {"country": "美国", "keyword": "lipstick", "product_images": [image["path"]]},
            {"country": "", "keyword": "lipstick", "product_images": [image["path"]]},
            {"country": "法国", "keyword": "lipstick", "product_images": [str(LEGACY_ROOT / ".env")]},
        ]))
        self.assertEqual(len(output["created"]), 1)
        self.assertEqual(len(output["rejected"]), 2)
        self.assertFalse(output["started"])
        self.collector.create.assert_awaited_once()

    def test_missing_images_has_actionable_validation_and_no_task(self):
        output = asyncio.run(self.service.create_batch([
            {"country": "墨西哥", "keyword": "TEST_IMAGE_REQUIRED", "product_images": []},
        ]))
        self.assertFalse(output["created"])
        self.assertIn("上传真实产品图", output["rejected"][0]["error"])
        self.collector.create.assert_not_called()
        self.assertFalse(output["started"])

    def test_real_excel_metadata_and_mapping_keep_original_file_immutable(self):
        original = workbook()
        job = self.service.prepare_import("excel", filename="original.xlsx", payload=original)
        self.service.run_import(job["import_id"], threading.Event())
        result = self.service.read_status(job["import_id"])
        self.assertEqual(result["worksheet"], "产品")
        self.assertEqual(result["worksheets"], ["产品"])
        self.assertEqual(result["column_mapping"], {"country": "A", "keyword": "B", "product_images": ["C"]})
        self.assertEqual(result["file_size"], len(original))
        self.assertEqual(result["file_sha256"], hashlib.sha256(original).hexdigest())
        mapped = self.service.remap(job["import_id"], "产品", {"country": "A", "keyword": "B", "product_images": ["C"]})
        self.service.run_import(mapped["import_id"], threading.Event())
        second = self.service.read_status(mapped["import_id"])
        self.assertEqual(second["status"], "READY", second)
        self.assertEqual(second["parent_import_id"], job["import_id"])
        self.assertEqual(Path(result["source_document"]).read_bytes(), original)
        self.assertEqual(Path(second["source_document"]).read_bytes(), original)
        self.assertEqual([row["country"] for row in second["rows"]], ["美国", "法国"])
        for row in second["rows"]:
            self.service.validate_source_context(row)

    def test_incompatible_product_description_sheet_is_not_silently_a_valid_collector_input(self):
        job = self.service.prepare_import("excel", filename="descriptions.xlsx", payload=workbook())
        source = self.service.directory(job["import_id"]) / "source.xlsx"
        output = self.service.directory(job["import_id"]) / "description-sheet.xlsx"
        metadata = mapped_workbook(source, output, mapping={"country": "A", "keyword": "B", "product_images": []})
        self.assertTrue(metadata["compatibility_errors"])
        self.assertEqual(metadata["column_mapping"]["product_images"], [])
        info = worksheet_info(output)
        self.assertEqual(info["column_mapping"]["product_images"], [])
        with self.assertRaises(HTTPException):
            self.service.remap(job["import_id"], "缺失工作表", {"country": "A", "keyword": "B", "product_images": []})

    def test_absolute_openxml_relationships_are_normalized_for_existing_legacy_reader(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(workbook())) as original, zipfile.ZipFile(payload, "w") as target:
            for item in original.infolist():
                content = original.read(item.filename)
                if item.filename == "xl/_rels/workbook.xml.rels":
                    content = content.replace(b'Target="worksheets/sheet1.xml"', b'Target="/xl/worksheets/sheet1.xml"')
                elif item.filename == "xl/worksheets/_rels/sheet1.xml.rels":
                    content = content.replace(b'Target="../drawings/drawing1.xml"', b'Target="/xl/drawings/drawing1.xml"')
                target.writestr(item, content)
        job = self.service.prepare_import("excel", filename="absolute-path.xlsx", payload=payload.getvalue())
        self.service.run_import(job["import_id"], threading.Event())
        result = self.service.read_status(job["import_id"])
        self.assertEqual(result["status"], "READY", result)
        self.assertTrue(all(row["status"] == "READY" for row in result["rows"]))
        self.assertEqual(Path(result["source_document"]).read_bytes(), payload.getvalue())

    def test_empty_shared_string_cell_in_tencent_export_is_a_blank_not_an_error(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(workbook())) as original, zipfile.ZipFile(payload, "w") as target:
            for item in original.infolist():
                content = original.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    content = content.replace(b'<c r="C2" t="inlineStr"><is><t></t></is></c>', b'<c r="C2" t="s"/>')
                target.writestr(item, content)
        path = Path(self.temp.name) / "blank-shared.xlsx"
        path.write_bytes(payload.getvalue())
        metadata = worksheet_info(path)
        self.assertEqual(metadata["raw_preview"][0]["cells"]["C"], "")

    def test_tencent_prepare_does_not_read_browser_and_is_serialized(self):
        job = self.service.prepare_import("tencent", url="https://docs.qq.com/sheet/ABCD?tab=one")
        self.assertEqual(self.service.read_status(job["import_id"])["status"], "QUEUED")
        self.assertFalse((self.service.directory(job["import_id"]) / "logs").exists())
        with self.assertRaises(HTTPException):
            self.service.prepare_import("tencent", url="https://docs.qq.com/sheet/ABCD?tab=two")
        restarted = CollectorInputService(self.collector, self.settings)
        self.assertEqual(restarted.read_status(job["import_id"])["status"], "QUEUED")
        restarted.recover()
        self.assertEqual(restarted.read_status(job["import_id"])["status"], "QUEUED")

    def test_second_app_does_not_interrupt_live_import(self):
        job = self.service.prepare_import("tencent", url="https://docs.qq.com/sheet/ABCD?tab=one")
        directory = self.service.directory(job["import_id"])
        entered, release = threading.Event(), threading.Event()
        def run(*args, **kwargs):
            kwargs["on_event"]({"type": "PROCESS_STARTED", "pid": os.getpid()})
            entered.set()
            self.assertTrue(release.wait(3))
            (directory / "result.json").write_text(json.dumps({"status": "READY", "rows": [], "message": "读取完成"}))
            return SimpleNamespace(cancelled=False, timed_out=False)
        with patch.object(self.service.runner, "run", side_effect=run):
            worker = threading.Thread(target=self.service.run_import, args=(job["import_id"], threading.Event()))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                before = (directory / "status.json").read_bytes()
                app = FastAPI()
                app.include_router(create_input_router(self.collector, self.settings))
                with TestClient(app) as client:
                    result = client.get("/api/collector/imports/" + job["import_id"]).json()
                    self.assertEqual(result["status"], "READING")
                    self.assertEqual(result["owner_pid"], os.getpid())
                    self.assertEqual((directory / "status.json").read_bytes(), before)
            finally:
                release.set()
                worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.service.read_status(job["import_id"])["status"], "READY")

    def test_startup_recovery_requires_dead_owner_and_dead_worker(self):
        job = self.service.prepare_import("tencent", url="https://docs.qq.com/sheet/ABCD?tab=one")
        directory = self.service.directory(job["import_id"])
        path = directory / "status.json"
        saved = json.loads(path.read_text())
        saved.update(status="READING", owner_pid=991001, worker_pid=991002)
        path.write_text(json.dumps(saved))
        restarted = CollectorInputService(self.collector, self.settings)
        self.assertEqual(json.loads(path.read_text())["status"], "READING")
        with patch("app.integrations.input_service.process_alive", side_effect=lambda pid: pid == 991002):
            restarted.recover()
        self.assertEqual(json.loads(path.read_text())["status"], "READING")
        with patch("app.integrations.input_service.process_alive", return_value=False):
            restarted.recover()
        self.assertEqual(json.loads(path.read_text())["error_code"], "INPUT_INTERRUPTED")
        path.write_text(json.dumps(saved))
        (directory / "result.json").write_text(json.dumps({"status": "READY", "rows": [], "message": "已完整导出"}))
        with patch("app.integrations.input_service.process_alive", return_value=False):
            restarted.recover()
        self.assertEqual(json.loads(path.read_text())["status"], "READY")

    def test_recent_imports_and_config_restore_history_without_execution(self):
        first = self.service.prepare_import("excel", filename="first.xlsx", payload=workbook())
        latest = self.service.prepare_import("excel", filename="latest.xlsx", payload=workbook())
        directory = self.service.directory(latest["import_id"])
        saved = self.service.read_status(latest["import_id"])
        saved.update(status="READY", rows=[{"country": "法国", "keyword": "unit", "product_images": []}])
        (directory / "status.json").write_text(json.dumps(saved))
        before = (directory / "status.json").read_bytes()
        app = FastAPI()
        router = create_input_router(self.collector, self.settings)
        app.include_router(router)
        with patch.object(router.input_service.runner, "run", side_effect=AssertionError("read-only endpoint ran a process")):
            with TestClient(app) as client:
                recent = client.get("/api/collector/imports?limit=1").json()
                self.assertEqual(recent["recent_import_id"], latest["import_id"])
                self.assertEqual(len(recent["imports"]), 1)
                self.assertEqual(recent["imports"][0]["row_count"], 1)
                self.assertEqual(client.get("/api/collector/inputs/config").json()["recent_import_id"], latest["import_id"])
                self.assertEqual(client.get("/api/collector/imports/" + latest["import_id"]).json()["rows"], saved["rows"])
                self.assertEqual(client.get("/api/collector/imports?limit=0").status_code, 422)
        self.assertEqual((directory / "status.json").read_bytes(), before)
        self.assertEqual(self.service.read_status(first["import_id"])["status"], "QUEUED")


if __name__ == "__main__":
    unittest.main()
