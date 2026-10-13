"""Synthetic workbooks/temp SQLite only; these tests are not production evidence."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import xml.etree.ElementTree as ET
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app import models as m
from app.domain import now
from app.integrations.input_service import write_json
from app.integrations.publishing.product_source import ProductSourceService, create_product_source_router
from app.integrations.publishing.product_source_worker import parse_workbook
from app.integrations.publishing.description_generation import ContentReviewRequest, DescriptionGenerationService, create_description_router
from app.integrations.publishing.description_selector import description_pool_hash, description_review_ready
from app.integrations.publishing.schemas import AccountInput, ProductInput, TaskInput
from app.repository import record
from test_real_publishing_service import SQLitePublishingFixture


HEADERS = ["国家", "手机序号", "账号名称", "keyword", "产品链接ID", *["产品描述" + str(i) for i in range(1, 21)]]
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
SOURCE = "https://docs.qq.com/sheet/UNIT_SOURCE?tab=77phlw"


def column_number(reference):
    number = 0
    for char in re.match(r"[A-Z]+", reference)[0]:
        number = number * 26 + ord(char) - 64
    return number


def column_name(number):
    value = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        value = chr(65 + remainder) + value
    return value


def workbook(path, headers, rows, *, formulas=None, leading_rows=()):
    sheet = ET.Element(NS + "worksheet")
    data = ET.SubElement(sheet, NS + "sheetData")
    for number, values in enumerate([*leading_rows, headers, *rows], start=1):
        row = ET.SubElement(data, NS + "row", r=str(number))
        for column, value in enumerate(values, start=1):
            formula = (formulas or {}).get((number, column))
            cell = ET.SubElement(row, NS + "c", r=column_name(column) + str(number), t="str" if formula else "inlineStr")
            if formula:
                ET.SubElement(cell, NS + "f").text = formula
                ET.SubElement(cell, NS + "v").text = value
            else:
                ET.SubElement(ET.SubElement(cell, NS + "is"), NS + "t").text = value
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="产品描述" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", ET.tostring(sheet, encoding="utf-8"))


def parse(path, **options):
    def cell_text(cell, shared):
        cached = cell.find(NS + "v")
        return (cached.text or "") if cached is not None else "".join(node.text or "" for node in cell.iter(NS + "t"))
    return parse_workbook(path, SOURCE, lambda archive: [], cell_text, column_number, **options)


def source_row(context="unit-context", account="UNIT_IMPORTED_ACCOUNT", descriptions=None):
    return {"country": "US", "telephone_number": "UNIT_NOT_A_PHONE", "account_name": account, "keyword": "UNIT_SOURCE_KEYWORD",
            "product_link_id": "UNIT_PLATFORM_ID", "descriptions": descriptions or [], "source_row": 2,
            "source_document": SOURCE, "worksheet": "产品描述", "context_id": context,
            "status": "READY" if descriptions else "DATA_INCOMPLETE", "errors": [] if descriptions else ["产品描述文案缺失"]}


def source_service(root):
    config = SimpleNamespace(output_root=root, product_source_url=SOURCE, product_selection_mode="runtime_match")
    settings = SimpleNamespace(legacy_tiktok_auto_python=root / "UNIT_NO_EXECUTION", legacy_tiktok_auto_root=root)
    return ProductSourceService(settings, config)


def job(service, rows, *, number=1, status="READY", exit_code=0):
    job_id = "source-" + str(number).zfill(32)
    directory = service.directory(job_id)
    directory.mkdir(parents=True)
    write_json(directory / "request.json", {"source_document": SOURCE})
    write_json(directory / "status.json", {"id": job_id, "status": status, "pid": 999999, "exit_code": exit_code, "finished_at": now()})
    write_json(directory / "result.json", {"status": "READY", "source_document": SOURCE, "rows": rows, "unit_only": True})
    return job_id


class ProductSourceLifecycleTests(unittest.TestCase):
    def test_constructor_does_not_create_output_directories(self):
        with tempfile.TemporaryDirectory(prefix="source-lifecycle-unit-") as temporary:
            root = Path(temporary)
            output_root = root / "not-created" / "unit-output"
            before = list(root.rglob("*"))
            service = source_service(output_root)
            self.assertEqual(service.root, output_root / "product_sources")
            self.assertEqual(list(root.rglob("*")), before)
            self.assertFalse(output_root.exists())
            self.assertEqual(service.jobs, {})


class ProductSourceWorkbookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-workbook-unit-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "unit.xlsx"

    def test_actual_header_names_map_all_twenty_without_truncation(self):
        texts = ["UNIT_ONLY_%02d" % number for number in range(1, 21)]
        workbook(self.path, HEADERS + ["产品描述21"], [["墨西哥", "18", "UNIT_ACCOUNT", "UNIT_KEYWORD", "1731972085362886468", *texts, "UNIT_EXTRA_NOT_IMPORTED"]])
        result = parse(self.path)
        self.assertEqual(result["status"], "READY")
        row = result["rows"][0]
        self.assertEqual(row["status"], "READY")
        self.assertEqual(row["product_link_id"], "1731972085362886468")
        self.assertEqual([item["id"] for item in row["descriptions"]], [str(i).zfill(2) for i in range(1, 21)])
        self.assertEqual([item["text"] for item in row["descriptions"]], texts)
        self.assertTrue(row["extra_description_column"])
        self.assertNotIn("UNIT_EXTRA_NOT_IMPORTED", str(row["descriptions"]))
        self.assertEqual(result["source_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_reordered_columns_are_mapped_by_header_not_position(self):
        values = {"国家": "墨西哥", "手机序号": "18", "账号名称": "UNIT_ACCOUNT", "keyword": "UNIT_KEYWORD", "产品链接ID": "UNIT_ID",
                  "产品描述1": "UNIT_FIRST", "产品描述20": "UNIT_LAST"}
        headers = list(reversed(HEADERS))
        workbook(self.path, headers, [[values.get(header, "") for header in headers]])
        row = parse(self.path)["rows"][0]
        self.assertEqual(row["country"], "墨西哥")
        self.assertEqual(row["account_name"], "UNIT_ACCOUNT")
        self.assertEqual([(item["id"], item["text"]) for item in row["descriptions"]], [("01", "UNIT_FIRST"), ("20", "UNIT_LAST")])

    def test_empty_actual_twenty_columns_are_data_incomplete_and_context_stable_on_description_refresh(self):
        values = ["墨西哥", "18", "UNIT_ACCOUNT", "UNIT_KEYWORD", "UNIT_ID", *[""] * 20]
        workbook(self.path, HEADERS, [values])
        old = parse(self.path)["rows"][0]
        self.assertEqual(old["status"], "DATA_INCOMPLETE")
        self.assertEqual(old["descriptions"], [])
        self.assertIn("产品描述文案缺失", old["errors"])
        expected = hashlib.sha256(json.dumps([SOURCE, "产品描述", 2, *values[:5]], ensure_ascii=False).encode()).hexdigest()[:24]
        self.assertEqual(old["context_id"], expected)
        values[5] = "UNIT_TEXT_REFRESH"
        workbook(self.path, HEADERS, [values])
        refreshed = parse(self.path)["rows"][0]
        self.assertEqual(refreshed["context_id"], old["context_id"])
        self.assertEqual(refreshed["source_row"], 2)

    def test_missing_or_duplicate_identifying_headers_never_guess_columns(self):
        for headers in (HEADERS[1:], HEADERS + ["账号名称"]):
            with self.subTest(headers=headers):
                workbook(self.path, headers, [["UNIT_ONLY"] * len(headers)])
                if len(headers) > len(HEADERS):
                    with self.assertRaisesRegex(ValueError, "PUBLISHING_COLUMNS_AMBIGUOUS"):
                        parse(self.path)
                else:
                    self.assertEqual(parse(self.path)["error_code"], "PUBLISHING_COLUMNS_NOT_FOUND")

    def test_actual_labels_allow_optional_account_and_reordered_partial_descriptions(self):
        headers = ["description_20", "关键词", "Country", "产品描述01"]
        workbook(self.path, headers, [["Twenty", "ActualKeyword", "MX", "One"]])
        row = parse(self.path)["rows"][0]
        self.assertEqual(row["country"], "MX")
        self.assertEqual(row["account_name"], "")
        self.assertEqual(row["column_map"], {"country": 3, "keyword": 2})
        self.assertEqual(row["description_columns"], {"20": 1, "01": 4})
        self.assertEqual(row["header_row"], 1)

    def test_reordered_all_twenty_aliases_preserve_identity_and_actual_column_maps(self):
        values = ["MX", "18", "UNIT_ACCOUNT", "UNIT_KEYWORD", "", *["UNIT_%02d" % i for i in range(1, 21)]]
        workbook(self.path, HEADERS, [values], leading_rows=[["UNIT_TITLE"]])
        original = parse(self.path)["rows"][0]
        indexes = list(range(24, -1, -1))
        aliases = [" Country ", "telephone_number", "account_name", "关键词", "product_link_id",
                   *["description_%02d" % i for i in range(1, 21)]]
        workbook(self.path, [aliases[i] for i in indexes], [[values[i] for i in indexes]], leading_rows=[["UNIT_TITLE"]])
        before = self.path.read_bytes()
        row = parse(self.path)["rows"][0]
        self.assertEqual(row["context_id"], original["context_id"])
        self.assertEqual(row["descriptions"], original["descriptions"])
        self.assertEqual(row["header_row"], 2)
        self.assertEqual(row["source_row"], 3)
        self.assertEqual(row["column_map"], {"product_link_id": 21, "keyword": 22, "account_name": 23, "telephone_number": 24, "country": 25})
        self.assertEqual(row["description_columns"], {str(i).zfill(2): 21 - i for i in range(1, 21)})
        self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_semantic_aliases_and_description_ordinals_are_ambiguous(self):
        for duplicate in ("Country", "关键词", "account_name", "产品描述01", "description_1", "产品描述文案20"):
            with self.subTest(duplicate=duplicate):
                workbook(self.path, [*HEADERS, duplicate], [["UNIT_ONLY"] * (len(HEADERS) + 1)])
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "PUBLISHING_COLUMNS_AMBIGUOUS"):
                    parse(self.path)
                self.assertEqual(self.path.read_bytes(), before)

    def test_formula_protection_uses_cells_not_cached_text_or_column_position(self):
        headers = ["description_20", "Country", "产品描述文案02", "关键词", "description03"]
        workbook(self.path, headers, [["UNIT_CACHED", "MX", "", "UNIT_KEYWORD", "=UNIT_LITERAL_NOT_FORMULA"]],
                 formulas={(2, 1): '"UNIT_CACHED"', (2, 3): 'IF(1=1,"","")'})
        before = self.path.read_bytes()
        result = parse(self.path)
        row = result["rows"][0]
        self.assertEqual(set(row["protected_description_ordinals"]), {"02", "20"})
        self.assertEqual([(item["id"], item["text"]) for item in row["descriptions"]],
                         [("03", "=UNIT_LITERAL_NOT_FORMULA"), ("20", "UNIT_CACHED")])
        self.assertEqual(row["description_columns"], {"20": 1, "02": 3, "03": 5})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(result["source_sha256"], hashlib.sha256(before).hexdigest())

    def test_empty_cached_formula_stays_protected_and_data_incomplete(self):
        workbook(self.path, ["国家", "keyword", "产品描述01"], [["US", "UNIT_KEYWORD", ""]],
                 formulas={(2, 3): 'IF(1=1,"","")'})
        row = parse(self.path)["rows"][0]
        self.assertEqual(row["protected_description_ordinals"], ["01"])
        self.assertEqual(row["descriptions"], [])
        self.assertEqual(row["status"], "DATA_INCOMPLETE")
        self.assertIn("产品描述文案缺失", row["errors"])

    def test_runtime_match_empty_product_id_is_not_missing_data_but_exact_id_is(self):
        workbook(self.path, HEADERS, [["墨西哥", "18", "UNIT_ACCOUNT", "UNIT_KEYWORD", "", "UNIT_TEXT", *[""] * 19]])
        row = parse(self.path)["rows"][0]
        self.assertEqual(row["status"], "READY")
        self.assertEqual(row["product_link_id"], "")
        exact = parse(self.path, product_selection_mode="exact_id")["rows"][0]
        self.assertEqual(exact["status"], "DATA_INCOMPLETE")
        self.assertIn("产品链接ID 缺失", exact["errors"])


class ProductSourceImportTests(unittest.TestCase):
    def setUp(self):
        self.f = SQLitePublishingFixture()
        self.addCleanup(self.f.close)
        self.source = source_service(self.f.root / "unit-source-output")
        app = FastAPI()
        app.include_router(create_product_source_router(self.source, self.f.service))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def import_rows(self, job_id, indexes=None, *, import_accounts=True, product_mappings=None):
        return self.client.post("/api/publishing/product-source/" + job_id + "/import", json={"row_indexes": indexes or [0], "import_accounts": import_accounts, "product_mappings": product_mappings or {}})

    def test_refresh_latest_uses_new_request_not_old_completed_status_mtime(self):
        old, new = job(self.source, [], number=1), job(self.source, [], number=2)
        os.utime(self.source.directory(old) / "request.json", ns=(1, 1))
        os.utime(self.source.directory(new) / "request.json", ns=(2, 2))
        os.utime(self.source.directory(old) / "status.json", ns=(100, 100))
        self.assertEqual(self.client.get("/api/publishing/product-source/latest").json()["id"], new)

    def test_reimport_is_idempotent_preserves_user_account_product_controls_and_context(self):
        row = source_row(descriptions=[{"id": "01", "text": "UNIT_TEXT"}])
        first = self.import_rows(job(self.source, [row])).json()["rows"][0]
        account_id, product_id = first["account_id"], first["product_id"]
        with self.f.service.session() as session:
            account = session.get(m.PublishingAccount, account_id)
            account.enabled, account.status, account.cooldown_seconds = False, "DISABLED", 360
            account.cdp_endpoint, account.hubstudio_env_id, account.expected_username = "http://127.0.0.1:9988", "unit-custom-env", "unit_custom_user"
            account.publishing_context = {**account.publishing_context, "user_note": "UNIT_KEEP_ACCOUNT"}
            product = session.get(m.Product, product_id)
            product.status = "disabled"
            product.description_selection = {"mode": "weighted", "exclude_last_n": 7}
            product.publishing_context = {**product.publishing_context, "user_note": "UNIT_KEEP_PRODUCT"}
        refreshed = {**row, "descriptions": [{"id": "20", "text": "UNIT_LATEST_SOURCE_TEXT"}]}
        response = self.import_rows(job(self.source, [refreshed], number=2), [0, 0])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["rows"], [first])
        with self.f.service.session() as session:
            account = session.get(m.PublishingAccount, account_id)
            self.assertEqual((account.enabled, account.status, account.cooldown_seconds), (False, "DISABLED", 360))
            self.assertEqual((account.cdp_endpoint, account.hubstudio_env_id, account.expected_username), ("http://127.0.0.1:9988", "unit-custom-env", "unit_custom_user"))
            self.assertEqual(account.publishing_context["user_note"], "UNIT_KEEP_ACCOUNT")
            product = session.get(m.Product, product_id)
            self.assertEqual(product.status, "disabled")
            self.assertEqual(product.description_selection, {"mode": "weighted", "exclude_last_n": 7})
            self.assertEqual(product.publishing_context["user_note"], "UNIT_KEEP_PRODUCT")
            self.assertEqual(next(item for item in product.descriptions if item["id"] == "20")["text"], "UNIT_LATEST_SOURCE_TEXT")
            self.assertTrue(any(item["text"] == "UNIT_TEXT" for item in product.descriptions))
            self.assertEqual(session.scalar(select(func.count()).select_from(m.AccountProduct).where(m.AccountProduct.account_id == account_id)), 1)
        self.assertEqual(len(self.f.service.products()), 2)
        self.assertEqual(len(self.f.service.accounts()), 2)

    def test_late_account_mapping_failure_rolls_back_entire_selected_import(self):
        for _ in range(2):
            self.f.service.save_account(AccountInput(name="UNIT_DUPLICATE", country="US"))
        before = self.f.repo.state()
        rows = [source_row("unit-first"), source_row("unit-second", account="UNIT_DUPLICATE")]
        response = self.import_rows(job(self.source, rows), [0, 1])
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "ACCOUNT_MAPPING_AMBIGUOUS")
        self.assertEqual(self.f.repo.state(), before)

    def test_late_schema_validation_rolls_back_all_selected_rows(self):
        before = self.f.repo.state()
        rows = [source_row("unit-first"), source_row("unit-second", descriptions=[{"id": "01", "text": "X" * 2201}])]
        response = self.import_rows(job(self.source, rows), [0, 1])
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], "INVALID_PRODUCT_SOURCE_ROW")
        self.assertEqual(self.f.repo.state(), before)

    def test_active_account_guard_failure_does_not_leave_product_saved(self):
        task = self.f.task()
        with self.f.service.session() as session:
            session.get(m.PublishingTask, task["id"]).status = "QUEUED"
        before = self.f.repo.state()
        response = self.import_rows(job(self.source, [source_row(account=self.f.account["name"])]))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "ACCOUNT_IN_ACTIVE_QUEUE")
        self.assertEqual(self.f.repo.state(), before)

    def test_import_accounts_false_preserves_held_account_bindings_and_checkpoint(self):
        task = self.f.task()
        with self.f.service.session() as session:
            account = session.get(m.PublishingAccount, self.f.account["id"])
            account.enabled, account.status, account.cooldown_seconds = False, "DISABLED", 360
            account.publishing_context = {"unit_only": True, "user_note": "UNIT_KEEP_HELD_ACCOUNT"}
            account.identity_verification = {"unit_only": True, "observed_username": "UNIT_KEEP_IDENTITY"}
            account.vpn_verification = {"unit_only": True, "country": "US"}
            row = session.get(m.PublishingTask, task["id"])
            row.status, row.submission_state = "WAITING_MANUAL", "UNKNOWN"
            row.checkpoint = {"unit_only": True, "UPLOAD_VIDEO": {"status": "UNKNOWN"}}
            session.add(m.ResourceLock(resource_key="account:" + account.id, publishing_task_id=row.id))

        def held_state():
            with self.f.service.session() as session:
                return ([record(row) for row in session.scalars(select(m.PublishingAccount).order_by(m.PublishingAccount.id))],
                        [record(row) for row in session.scalars(select(m.AccountProduct).order_by(m.AccountProduct.account_id, m.AccountProduct.product_id))],
                        [record(row) for row in session.scalars(select(m.ResourceLock).order_by(m.ResourceLock.resource_key))],
                        record(session.get(m.PublishingTask, task["id"])))

        before = held_state()
        statements = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.lower())
        event.listen(self.f.repo.engine, "before_cursor_execute", capture)
        self.addCleanup(event.remove, self.f.repo.engine, "before_cursor_execute", capture)
        imported_row = source_row("unit-held-product-only", account=self.f.account["name"], descriptions=[{"id": "01", "text": "UNIT_NEW_TEXT"}])
        job_id = job(self.source, [imported_row])
        response = self.import_rows(job_id, import_accounts=False)
        self.assertEqual(response.status_code, 200, response.text)
        mapping = response.json()["rows"][0]
        self.assertIsNone(mapping["account_id"])
        self.assertNotEqual(mapping["product_id"], self.f.product["id"])
        repeated = self.import_rows(job_id, import_accounts=False)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["rows"], [mapping])
        self.assertEqual(held_state(), before)
        account_writes = [sql for sql in statements if sql.lstrip().startswith(("insert ", "update ", "delete "))
                          and any(table in sql for table in ("publishing_accounts", "account_products", "publishing_resource_locks", "publishing_tasks"))]
        self.assertEqual(account_writes, [])
        state_before_account_import = self.f.repo.state()
        response = self.import_rows(job_id)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "ACCOUNT_ENVIRONMENT_HAS_UNRESOLVED_CHECKPOINT")
        self.assertEqual(self.f.repo.state(), state_before_account_import)
        self.assertEqual(held_state(), before)

    def test_import_keeps_formula_protection_and_optional_account_mapping(self):
        path = self.f.root / "unit-protected-source.xlsx"
        workbook(path, ["产品描述01", "关键词", "Country", "description_20"], [["", "UNIT_KEYWORD", "US", "UNIT_CACHED"]],
                 formulas={(2, 1): 'IF(1=1,"","")', (2, 4): '"UNIT_CACHED"'})
        before_bytes = path.read_bytes()
        row = parse(path)["rows"][0]
        with self.f.service.session() as session:
            before_accounts = [record(account) for account in session.scalars(select(m.PublishingAccount).order_by(m.PublishingAccount.id))]
        response = self.import_rows(job(self.source, [row]))
        self.assertEqual(response.status_code, 200, response.text)
        mapping = response.json()["rows"][0]
        self.assertIsNone(mapping["account_id"])
        with self.f.service.session() as session:
            product = session.get(m.Product, mapping["product_id"])
            self.assertEqual(product.descriptions, row["descriptions"])
            for key in ("worksheet", "header_row", "column_map", "description_columns", "protected_description_ordinals"):
                self.assertEqual(product.publishing_context[key], row[key])
            self.assertEqual([record(account) for account in session.scalars(select(m.PublishingAccount).order_by(m.PublishingAccount.id))], before_accounts)
            self.assertFalse(session.scalar(select(m.AccountProduct).where(m.AccountProduct.product_id == product.id)))
        self.assertEqual(path.read_bytes(), before_bytes)

    def test_imported_cloud_pool_requires_current_product_fact_review_without_generation(self):
        texts = [{"id": str(i).zfill(2), "text": "UNIT_IMPORTED_TEXT_%02d" % i} for i in range(1, 21)]
        response = self.import_rows(job(self.source, [source_row(descriptions=texts)]), import_accounts=False)
        self.assertEqual(response.status_code, 200, response.text)
        product_id = response.json()["rows"][0]["product_id"]
        descriptions = DescriptionGenerationService(self.f.service, self.source, SimpleNamespace())
        app = FastAPI()
        app.include_router(create_description_router(descriptions))
        with TestClient(app) as client:
            path = "/api/publishing/products/" + product_id + "/descriptions/content-review"
            current = client.get(path).json()
            self.assertEqual(current["status"], "REQUIRED")
            self.assertTrue(current["required"])
            self.assertIsNone(current["generation_id"])
            self.assertEqual(len(current["descriptions"]), 20)
            self.assertEqual(current["sha256"], description_pool_hash(current["descriptions"]))
            self.assertEqual(client.get("/api/publishing/products/" + product_id + "/descriptions/generations").json(), [])
            before = self.f.repo.state()
            self.assertEqual(client.post(path, json={"sha256": current["sha256"]}).status_code, 422)
            self.assertEqual(client.post(path, json={"confirm_facts": True, "sha256": "0" * 64}).status_code, 409)
            self.assertEqual(self.f.repo.state(), before)
            approved = client.post(path, json={"confirm_facts": True, "sha256": current["sha256"]})
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.json()["status"], "APPROVED")
        saved = next(product for product in self.f.service.products() if product["id"] == product_id)
        self.assertTrue(description_review_ready(saved))
        saved["descriptions"][0]["text"] = "UNIT_CHANGED_AFTER_REVIEW"
        forged = {"description_review_required": False, "description_review": {"source": "USER_CONFIRMED", "sha256": description_pool_hash(saved["descriptions"])}}
        edited = self.f.service.save_product(ProductInput(name=saved["name"], country=saved["country"], descriptions=saved["descriptions"], publishing_context=forged), product_id)
        self.assertTrue(edited["publishing_context"]["description_review_required"])
        self.assertFalse(description_review_ready(edited))
        removed = self.f.service.save_product(ProductInput(name=edited["name"], country=edited["country"], descriptions=edited["descriptions"]), product_id)
        self.assertTrue(removed["publishing_context"]["description_review_required"])
        self.assertFalse(description_review_ready(removed))
        self.assertEqual(descriptions.product_review(product_id)["descriptions"][0]["text"], "UNIT_CHANGED_AFTER_REVIEW")

    def test_explicit_source_mapping_preserves_canonical_task_held_account_and_generation_history(self):
        with self.f.service.session() as session:
            canonical = session.get(m.Product, self.f.product["id"])
            canonical.publishing_context, canonical.keywords, canonical.image = {}, [], "UNIT_KEEP_IMAGE"
            session.get(m.Task, "unit-source-task").keyword = "UNIT_SOURCE_KEYWORD"
        task = self.f.task()
        texts = [{"id": str(i).zfill(2), "text": "UNIT_SOURCE_%02d" % i} for i in range(1, 21)]
        row = source_row("unit-explicit-canonical", account=self.f.account["name"], descriptions=texts)
        first = self.import_rows(job(self.source, [row]), import_accounts=False)
        self.assertEqual(first.status_code, 200, first.text)
        independent_id = first.json()["rows"][0]["product_id"]
        with self.f.service.session() as session:
            pending = session.get(m.PublishingTask, task["id"])
            pending.status, pending.submission_state = "WAITING_MANUAL", "UNKNOWN"
            session.add(m.ResourceLock(resource_key="account:" + self.f.account["id"], publishing_task_id=pending.id))
            session.add(m.DescriptionGeneration(id="unit-historical-source-generation", product_id=independent_id,
                country="US", keyword="UNIT_SOURCE_KEYWORD", model="UNIT_ONLY_NO_MODEL_CALL", prompt_version="unit-v1",
                input_fingerprint="1" * 64, status="READY", request={"unit_only": True}, descriptions=texts, evidence={"unit_only": True}))
        def protected():
            with self.f.service.session() as session:
                return (record(session.get(m.Task, "unit-source-task")), record(session.get(m.PublishingTask, task["id"])),
                    record(session.get(m.PublishingAccount, self.f.account["id"])),
                    [record(item) for item in session.scalars(select(m.AccountProduct).order_by(m.AccountProduct.account_id, m.AccountProduct.product_id))],
                    [record(item) for item in session.scalars(select(m.ResourceLock))],
                    record(session.get(m.DescriptionGeneration, "unit-historical-source-generation")), record(session.get(m.Product, independent_id)))
        before = protected()
        refreshed = {**row, "descriptions": [{**item, "text": item["text"] + "_REFRESHED"} for item in texts]}
        job_id = job(self.source, [refreshed], number=2)
        response = self.import_rows(job_id, import_accounts=False, product_mappings={0: self.f.product["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["rows"][0]["product_id"], self.f.product["id"])
        self.assertIsNone(response.json()["rows"][0]["account_id"])
        self.assertEqual(protected(), before)
        canonical = next(product for product in self.f.service.products() if product["id"] == self.f.product["id"])
        self.assertEqual((canonical["name"], canonical["image"]), (self.f.product["name"], "UNIT_KEEP_IMAGE"))
        self.assertEqual(canonical["integration_task_count"], 1)
        self.assertEqual(canonical["publishing_context"]["context_id"], row["context_id"])
        self.assertTrue(canonical["publishing_context"]["description_review_required"])
        self.assertNotIn("description_generation_id", canonical["publishing_context"])
        self.assertFalse(description_review_ready(canonical))
        self.assertEqual([item["text"] for item in canonical["descriptions"]], [item["text"] for item in refreshed["descriptions"]])
        before_default = self.f.repo.state()
        ambiguous = self.import_rows(job_id, import_accounts=False)
        self.assertEqual(ambiguous.status_code, 409, ambiguous.text)
        self.assertEqual(ambiguous.json()["detail"], "PRODUCT_MAPPING_AMBIGUOUS")
        self.assertEqual(self.f.repo.state(), before_default)

    def test_explicit_mapping_country_keyword_and_existing_source_scope_guards_rollback(self):
        cases = [("country", "SOURCE_PRODUCT_COUNTRY_KEYWORD_MISMATCH"), ("keyword", "SOURCE_PRODUCT_COUNTRY_KEYWORD_MISMATCH"),
                 ("account", "SOURCE_PRODUCT_CONTEXT_CONFLICT"), ("row", "SOURCE_PRODUCT_CONTEXT_CONFLICT"),
                 ("document", "SOURCE_PRODUCT_CONTEXT_CONFLICT")]
        for number, (kind, error) in enumerate(cases, start=1):
            with self.subTest(kind=kind):
                row = source_row(descriptions=[{"id": "01", "text": "UNIT_ONLY_TEXT"}])
                with self.f.service.session() as session:
                    target = session.get(m.Product, self.f.product["id"])
                    target.keywords = [row["keyword"]]
                    target.publishing_context = {}
                    if kind in {"account", "row", "document"}:
                        target.publishing_context = {key: row[key] for key in ("source_document", "source_row", "worksheet", "account_name", "telephone_number")}
                        key = {"account": "account_name", "row": "source_row", "document": "source_document"}[kind]
                        target.publishing_context = {**target.publishing_context, key: 100 if kind == "row" else "UNIT_OTHER_SOURCE"}
                if kind == "country":
                    row["country"] = "MX"
                if kind == "keyword":
                    row["keyword"] = "UNIT_UNRELATED_KEYWORD"
                before = self.f.repo.state()
                response = self.import_rows(job(self.source, [row], number=number), import_accounts=False, product_mappings={0: self.f.product["id"]})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"], error)
                self.assertEqual(self.f.repo.state(), before)

    def test_mapping_cannot_apply_to_unselected_or_missing_target_product(self):
        job_id = job(self.source, [source_row()])
        before = self.f.repo.state()
        for mapping, status in (({1: self.f.product["id"]}, 422), ({0: "UNIT_MISSING_PRODUCT"}, 404)):
            with self.subTest(mapping=mapping):
                response = self.import_rows(job_id, import_accounts=False, product_mappings=mapping)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(self.f.repo.state(), before)

    def test_ready_result_without_successful_process_exit_is_not_importable(self):
        job_id = job(self.source, [source_row()], status="FAILED", exit_code=2)
        self.assertEqual(self.source.status(job_id)["status"], "FAILED")
        self.assertEqual(self.import_rows(job_id).status_code, 409)

    def test_empty_product_id_can_import_runtime_name_but_exact_id_rejects(self):
        row = {**source_row(), "product_link_id": ""}
        job_id = job(self.source, [row])
        response = self.import_rows(job_id)
        self.assertEqual(response.status_code, 200, response.text)
        product_id = response.json()["rows"][0]["product_id"]
        with self.f.service.session() as session:
            product = session.get(m.Product, product_id)
            self.assertIsNone(product.platform_product_id)
            self.assertEqual(product.binding_reference, "")
            self.assertEqual(product.publishing_context["data_status"], "DATA_INCOMPLETE")
        self.f.service.config = self.f.config.model_copy(update={"product_selection_mode": "exact_id"})
        before = self.f.repo.state()
        response = self.import_rows(job_id)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.f.repo.state(), before)


class ProductSourceExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = SQLitePublishingFixture()
        self.source = source_service(self.f.root / "unit-source-output")

    async def asyncTearDown(self):
        await self.source.shutdown()
        await self.f.service.shutdown()
        self.f.close()

    async def test_missing_descriptions_imports_but_full_publish_stays_manual_no_upload(self):
        app = FastAPI()
        app.include_router(create_product_source_router(self.source, self.f.service))
        job_id = job(self.source, [source_row()])
        with TestClient(app) as client:
            response = client.post("/api/publishing/product-source/" + job_id + "/import", json={"row_indexes": [0]})
        self.assertEqual(response.status_code, 200, response.text)
        mapping = response.json()["rows"][0]
        account = self.f.service.save_account(AccountInput(name="UNIT_IMPORTED_ACCOUNT", country="US", product_ids=[mapping["product_id"]],
            cdp_endpoint="http://127.0.0.1:9999", hubstudio_env_id="unit-env", expected_username="unit_username"), mapping["account_id"])
        with self.f.service.session() as session:
            session.get(m.Task, "unit-source-task").product_id = mapping["product_id"]
            self.assertEqual(session.get(m.Product, mapping["product_id"]).publishing_context["data_status"], "DATA_INCOMPLETE")
        task = self.f.service.create_task(TaskInput(task_id="unit-source-task", video_id="unit-final", product_id=mapping["product_id"], account_id=account["id"]))
        result = await self.f.service.preflight(task["id"])
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(result["error_code"], "PUBLISHING_DATA_INCOMPLETE")
        await self.f.service.create_batch([task["id"]])
        await self.f.service.worker
        self.assertEqual(self.f.adapter.steps, [])
        self.assertEqual(self.f.service.task(task["id"])["submission_state"], "NOT_SUBMITTED")

    async def test_cloud_text_without_fact_review_blocks_full_publish_and_select_until_unit_confirmation(self):
        app = FastAPI()
        app.include_router(create_product_source_router(self.source, self.f.service))
        texts = [{"id": str(i).zfill(2), "text": "UNIT_IMPORTED_%02d" % i} for i in range(1, 21)]
        job_id = job(self.source, [source_row(descriptions=texts)])
        with TestClient(app) as client:
            response = client.post("/api/publishing/product-source/" + job_id + "/import", json={"row_indexes": [0]})
        self.assertEqual(response.status_code, 200, response.text)
        mapping = response.json()["rows"][0]
        account = self.f.service.save_account(AccountInput(name="UNIT_IMPORTED_ACCOUNT", country="US", product_ids=[mapping["product_id"]],
            cdp_endpoint="http://127.0.0.1:9999", hubstudio_env_id="unit-env", expected_username="unit_username"), mapping["account_id"])
        with self.f.service.session() as session:
            session.get(m.Task, "unit-source-task").product_id = mapping["product_id"]
        task = self.f.service.create_task(TaskInput(task_id="unit-source-task", video_id="unit-final", product_id=mapping["product_id"], account_id=account["id"]))
        result = await self.f.service.preflight(task["id"])
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(next(item for item in result["checks"] if item["name"] == "description")["reason"], "DESCRIPTION_CONTENT_REVIEW_REQUIRED")
        self.assertEqual(self.f.service.select_description(task["id"])["error_code"], "DESCRIPTION_CONTENT_REVIEW_REQUIRED")
        await self.f.service.create_batch([task["id"]])
        await self.f.service.worker
        self.assertEqual(self.f.adapter.steps, [])
        self.assertEqual(self.f.service.task(task["id"])["submission_state"], "NOT_SUBMITTED")
        descriptions = DescriptionGenerationService(self.f.service, self.source, SimpleNamespace())
        current = descriptions.product_review(mapping["product_id"])
        descriptions.review_product_content(mapping["product_id"], ContentReviewRequest(confirm_facts=True, sha256=current["sha256"]))
        self.assertEqual(self.f.service.select_description(task["id"])["status"], "READY")
        self.assertEqual(self.f.adapter.steps, [])

    async def test_refresh_create_schedules_new_read_without_starting_external_process_in_unit(self):
        with patch.object(self.source, "read", new_callable=AsyncMock) as reader:
            first = await self.source.create(SOURCE)
            second = await self.source.create(SOURCE)
            await self.source.shutdown()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(reader.await_count, 2)
        self.assertEqual(first["status"], "QUEUED")
        self.assertEqual(second["status"], "QUEUED")

    async def test_exported_result_requires_successful_finished_process_before_ready(self):
        for number, exit_code in ((1, 0), (2, 2)):
            with self.subTest(exit_code=exit_code):
                job_id = job(self.source, [source_row()], number=number, status="READING", exit_code=None)
                self.source.cancellations[job_id] = Mock()
                self.assertEqual(self.source.status(job_id)["status"], "READING")
                result = SimpleNamespace(pid=999999, exit_code=exit_code, started_at=now(), finished_at=now(), log_path="UNIT_ONLY_LOG")
                runner = Mock(run=Mock(return_value=result))
                with patch("app.integrations.publishing.product_source.ProcessRunner", return_value=runner):
                    await self.source.read(job_id)
                self.assertEqual(self.source.status(job_id)["status"], "READY" if exit_code == 0 else "FAILED")
                self.assertEqual(runner.run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
