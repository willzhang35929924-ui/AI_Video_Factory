from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import HTTPException

from app.integrations.contracts import CollectorInput, FactoryCollectorRules
from app.integrations.process_runner import ProcessRunner
from app.integrations.process_probe import probe_process
from app.integrations.excel_mapping import worksheet_info

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_ROWS = 1000
DRAFT_KEYS = {"min_views", "min_likes", "min_like_view_ratio", "product_match_min_confidence",
              "reference_product_match_required", "accepted_product_match", "accepted_ai_origin",
              "accepted_cutout_suitability", "accepted_require_cutout_suitability", "manual_review_on_uncertain",
              "filter_overlay", "filter_watermark", "filter_subtitle", "require_person_integrity", "person_integrity"}
ROW_KEYS = ("country", "keyword", "product_images", "source_document", "source_row", "context_id")


def process_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        probe_process(pid)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".part")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def source_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.netloc != "docs.qq.com" or not re.fullmatch(r"/sheet/[A-Za-z0-9_-]+", parsed.path):
        raise HTTPException(422, "请输入 https://docs.qq.com/sheet/ 开头的腾讯表格链接")
    return urlunsplit(("https", "docs.qq.com", parsed.path,
                       urlencode([(key, value) for key, value in parse_qsl(parsed.query) if key == "tab"]), ""))


def validate_workbook(payload: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
                raise ValueError("missing workbook")
            if len(names) > 10000 or sum(item.file_size for item in archive.infolist()) > 250 * 1024 * 1024:
                raise HTTPException(413, "Excel 解压后超过允许大小")
            if any(item.flag_bits & 1 for item in archive.infolist()):
                raise ValueError("encrypted workbook")
            if archive.testzip() is not None:
                raise ValueError("invalid workbook")
    except (zipfile.BadZipFile, ValueError, RuntimeError):
        raise HTTPException(422, "Excel 文件损坏或格式不支持，请上传有效的 .xlsx 文件") from None


class CollectorInputService:
    def __init__(self, collector_service, settings, runner=None):
        self.collector = collector_service
        self.settings = settings
        self.root = (settings.storage_root / "collector_inputs").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.runner = runner or ProcessRunner()
        self.jobs: dict[str, asyncio.Task] = {}
        self.cancel_events: dict[str, threading.Event] = {}

    def recover(self) -> None:
        for path in self.root.glob("input-*/status.json"):
            saved = json.loads(path.read_text())
            owner = saved.get("owner_pid")
            if saved.get("status") not in {"QUEUED", "READING"} or not owner or process_alive(owner):
                continue
            if (path.parent / "result.json").is_file():
                saved.update(self.completed_result(path.parent))
            elif process_alive(saved.get("worker_pid")):
                continue
            else:
                saved.update(status="FAILED", error_code="INPUT_INTERRUPTED", message="上次读取进程已退出，请重新读取")
            write_json(path, saved)

    def directory(self, import_id: str) -> Path:
        if not re.fullmatch(r"input-[0-9a-f]{32}", import_id):
            raise HTTPException(404, "导入记录不存在")
        target = (self.root / import_id).resolve()
        if not target.is_relative_to(self.root):
            raise HTTPException(404, "导入记录不存在")
        return target

    def new_directory(self) -> tuple[str, Path]:
        import_id = "input-" + uuid4().hex
        directory = self.directory(import_id)
        directory.mkdir()
        return import_id, directory

    def defaults(self) -> dict:
        value = ""
        for line in (self.settings.legacy_tiktok_auto_root / "config.yaml").read_text(encoding="utf-8").splitlines():
            key, separator, item = line.partition(":")
            if separator and key.strip() == "tencent_docs_url":
                value = item.strip().strip("\"'")
                break
        try:
            value = source_url(value) if value else ""
        except HTTPException:
            value = ""
        return {"tencent_docs_url": value, "worksheet": parse_qs(urlsplit(value).query).get("tab", ["首工作表"])[0],
                "headers": ["国家", "keyword", *["产品图" + str(index) for index in range(1, 7)]],
                "read_only": True, "max_images": 6, "max_rows": MAX_ROWS,
                "recent_import_id": self.recent_imports(1)["recent_import_id"]}

    def recent_imports(self, limit: int = 10) -> dict:
        statuses = [self.read_status(path.parent.name) for path in self.root.glob("input-*/status.json")]
        statuses.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        summaries = [{**{key: row.get(key) for key in ("import_id", "kind", "status", "source_document", "created_at", "message")},
                      "row_count": len(row.get("rows", []))} for row in statuses[:limit]]
        return {"imports": summaries, "recent_import_id": summaries[0]["import_id"] if summaries else None}

    def save_image(self, payload: bytes, filename: str) -> dict:
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif payload.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            raise HTTPException(422, "产品图仅支持 PNG、JPEG、WebP 图片")
        if not payload or len(payload) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "产品图片不能超过 15 MB")
        digest = hashlib.sha256(payload).hexdigest()
        import_id, directory = self.new_directory()
        target = directory / (digest + suffix)
        target.write_bytes(payload)
        return {"path": str(target), "url": "/api/collector/files/" + import_id + "/" + target.name,
                "filename": filename, "size": len(payload), "sha256": digest}

    def prepare_import(self, kind: str, *, filename: str = "", payload: bytes = b"", url: str = "") -> dict:
        if kind == "excel":
            if Path(filename).suffix.lower() != ".xlsx":
                raise HTTPException(422, "仅接受 .xlsx 文件")
            if not payload or len(payload) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Excel 文件必须小于 25 MB 且不能为空")
            validate_workbook(payload)
        if kind == "tencent":
            for path in self.root.glob("input-*/status.json"):
                saved = self.read_status(path.parent.name)
                if saved.get("kind") == "tencent" and saved.get("status") in {"QUEUED", "READING"}:
                    raise HTTPException(409, "已有腾讯文档读取任务正在运行，请等待结束后刷新")
        source = source_url(url) if kind == "tencent" else filename
        import_id, directory = self.new_directory()
        if kind == "excel":
            (directory / "source.xlsx").write_bytes(payload)
            source = str(directory / "source.xlsx")
        request = {"import_id": import_id, "kind": kind, "source_document": source,
                   "original_filename": filename, "created_at": datetime.now(timezone.utc).isoformat(), "owner_pid": os.getpid()}
        if kind == "excel":
            request.update(file_size=len(payload), file_sha256=hashlib.sha256(payload).hexdigest())
        write_json(directory / "request.json", request)
        write_json(directory / "status.json", {**request, "status": "QUEUED", "rows": [], "message": "等待读取"})
        return {"import_id": import_id, "status": "QUEUED"}

    def remap(self, import_id, worksheet, mapping):
        directory = self.directory(import_id)
        source = directory / "source.xlsx"
        previous = self.read_status(import_id)
        if previous["status"] in {"QUEUED", "READING"} or not source.is_file():
            raise HTTPException(409, "请等待原 Excel 导出或解析结束，再选择工作表/字段映射")
        if not isinstance(worksheet, str) or not isinstance(mapping, dict):
            raise HTTPException(422, "请通过工作表和字段选择器设置映射")
        try:
            info = worksheet_info(source, worksheet)
        except (ValueError, KeyError, zipfile.BadZipFile):
            raise HTTPException(422, "工作表不存在或原 Excel 无法读取") from None
        if set(mapping) != {"country", "keyword", "product_images"} or not isinstance(mapping.get("product_images"), list):
            raise HTTPException(422, "字段映射必须包含国家、keyword 和产品图列")
        columns = [mapping.get("country"), mapping.get("keyword"), *mapping["product_images"]]
        available = {header["column"] for header in info["headers"]}
        if (len(mapping["product_images"]) > 6 or not all(isinstance(column, str) and column in available for column in columns)
                or len(columns) != len(set(columns))):
            raise HTTPException(422, "国家/keyword 需映射不同的已有列；产品图最多六列，不能重复")
        job = self.prepare_import("excel", filename=previous.get("original_filename") or "tencent-export.xlsx", payload=source.read_bytes())
        target = self.directory(job["import_id"])
        request = json.loads((target / "request.json").read_text())
        request.update(worksheet=worksheet, column_mapping=mapping, parent_import_id=import_id)
        if previous.get("kind") == "tencent":
            request["source_document"] = previous["source_document"]
        write_json(target / "request.json", request)
        write_json(target / "status.json", {**request, "status": "QUEUED", "rows": [], "message": "正在按所选工作表与字段重新读取；原文件和旧结果保留"})
        return job

    async def launch(self, import_id: str) -> None:
        event = threading.Event()
        self.cancel_events[import_id] = event
        job = asyncio.create_task(asyncio.to_thread(self.run_import, import_id, event))
        self.jobs[import_id] = job
        job.add_done_callback(lambda _job: (self.jobs.pop(import_id, None), self.cancel_events.pop(import_id, None)))

    def run_import(self, import_id: str, cancel_event: threading.Event) -> None:
        directory = self.directory(import_id)
        request = json.loads((directory / "request.json").read_text())
        write_json(directory / "status.json", {**request, "status": "READING", "rows": [], "message": "正在读取数据源"})
        def process_event(event):
            if event.get("type") == "PROCESS_STARTED":
                status = self.read_status(import_id)
                status["worker_pid"] = event["pid"]
                write_json(directory / "status.json", status)
        try:
            execution = self.runner.run([str(self.settings.legacy_tiktok_auto_python), "-B",
                str(Path(__file__).parent / "legacy/input_worker.py"), "--legacy-root", str(self.settings.legacy_tiktok_auto_root),
                "--job-dir", str(directory), "--kind", request["kind"]], cwd=self.settings.legacy_tiktok_auto_root,
                log_dir=directory / "logs", timeout_s=180, cancel_event=cancel_event, on_event=process_event)
            result_path = directory / "result.json"
            if execution.cancelled or execution.timed_out:
                result = {"status": "FAILED", "rows": [], "error_code": "INPUT_CANCELLED" if execution.cancelled else "INPUT_TIMEOUT",
                          "message": "读取已中止；原数据和浏览器登录保留，可重新读取"}
            elif result_path.is_file():
                result = self.completed_result(directory)
            else:
                result = {"status": "FAILED", "rows": [], "error_code": "INPUT_PROCESS_FAILED", "message": "旧读取器未返回结果，请查看导入记录"}
        except (OSError, ValueError, RuntimeError):
            result = {"status": "FAILED", "rows": [], "error_code": "INPUT_PROCESS_FAILED", "message": "数据源读取进程失败"}
        write_json(directory / "status.json", {**request, **result})

    def completed_result(self, directory: Path) -> dict:
        result = json.loads((directory / "result.json").read_text())
        if len(result.get("rows", [])) > MAX_ROWS:
            return {"status": "FAILED", "rows": [], "error_code": "TOO_MANY_ROWS", "message": "单次最多读取 1000 行"}
        for row in result.get("rows", []):
            row["product_image_urls"] = ["/api/collector/files/" + directory.name + "/" +
                str(Path(value).resolve().relative_to(directory)) for value in row["product_images"]]
        return result

    def read_status(self, import_id: str) -> dict:
        path = self.directory(import_id) / "status.json"
        if not path.is_file():
            raise HTTPException(404, "导入记录不存在")
        status = json.loads(path.read_text())
        if status.get("status") in {"QUEUED", "READING"} and status.get("owner_pid") and not process_alive(status["owner_pid"]) and (path.parent / "result.json").is_file():
            return {**status, **self.completed_result(path.parent)}
        return status

    def file(self, import_id: str, filename: str) -> Path:
        directory = self.directory(import_id)
        path = (directory / filename).resolve()
        if not path.is_relative_to(directory) or not re.fullmatch(r"(?:product_references/)?[0-9a-f]{64}\.(png|jpg|jpeg|webp)", filename) or not path.is_file():
            raise HTTPException(404, "产品图不存在")
        return path

    def validate_images(self, paths: list[str]) -> list[str]:
        result = []
        legacy_refs = (self.settings.legacy_tiktok_auto_root / "data/product_references").resolve()
        for value in paths:
            path = Path(value).expanduser().resolve()
            if not (path.is_relative_to(self.root) or path.is_relative_to(legacy_refs)) or not path.is_file():
                raise HTTPException(422, "产品图必须来自文件选择器或已读取的数据源")
            result.append(str(path))
        return result

    def validate_source_context(self, row: dict) -> None:
        if not row.get("context_id"):
            return
        for status_path in self.root.glob("input-*/status.json"):
            imported = self.read_status(status_path.parent.name)
            if any(all(item.get(key) == row.get(key) for key in ROW_KEYS) for item in imported.get("rows", [])):
                return
        raise HTTPException(422, "数据源上下文不匹配；请重新读取数据，或作为手动输入创建")

    async def create_batch(self, rows: list[dict], rules: dict | None = None) -> dict:
        if not rows or len(rows) > MAX_ROWS:
            raise HTTPException(422, "请提交 1 至 1000 行任务")
        if rules is not None:
            try:
                rules = FactoryCollectorRules.model_validate(rules).model_dump()
            except ValueError:
                raise HTTPException(422, "Factory 筛选规则无效；赞播比由最低 Like/PV 自动计算") from None
        created, duplicates, rejected = [], [], []
        for index, raw in enumerate(rows):
            try:
                row = {key: raw[key] for key in CollectorInput.model_fields if key in raw}
                if rules is not None:
                    row["factory_rules"] = rules
                self.validate_source_context(row)
                if not row.get("product_images"):
                    raise HTTPException(422, "缺少产品图片；请在这一行上传真实产品图后再创建任务")
                data = CollectorInput.model_validate(row)
                row["product_images"] = self.validate_images(data.product_images)
                result = await self.collector.create(row)
                if result.get("status") == "duplicate":
                    duplicates.append({**result, "index": index})
                else:
                    created.append(result)
            except HTTPException as exc:
                rejected.append({"index": index, "error": str(exc.detail)})
            except ValueError:
                rejected.append({"index": index, "error": "国家、关键词或产品图输入无效"})
        return {"created": created, "duplicates": duplicates, "rejected": rejected, "started": False,
                "message": "任务已创建，请点击开始生产" if created else "未创建新任务；历史重复已跳过" if duplicates else "没有有效输入"}

    def draft(self, rules: dict | None = None) -> dict:
        path = self.root / "rules-draft.json"
        if rules is not None:
            if set(rules) - DRAFT_KEYS:
                raise HTTPException(422, "包含不支持的筛选规则")
            if any(not isinstance(value, (str, int, float, bool, type(None))) for value in rules.values()):
                raise HTTPException(422, "规则值必须是数字、文本或开关")
            write_json(path, {"rules": rules, "updated_at": datetime.now(timezone.utc).isoformat(),
                              "applied": False, "message": "Factory Draft；尚未应用，旧生产配置保持只读"})
        return json.loads(path.read_text()) if path.is_file() else {"rules": {}, "applied": False}

    async def close(self) -> None:
        for event in self.cancel_events.values():
            event.set()
        if self.jobs:
            await asyncio.gather(*self.jobs.values(), return_exceptions=True)
