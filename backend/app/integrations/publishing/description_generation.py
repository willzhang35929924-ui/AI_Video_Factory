import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from fastapi import APIRouter, HTTPException
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app import models as m
from app.domain import new_id
from app.repository import record
from app.integrations.input_service import write_json
from app.integrations.process_runner import ProcessRunner
from app.integrations.publishing.config import DescriptionAIConfig, load_publishing_config
from app.integrations.publishing.description_selector import description_pool_hash, description_review_ready


class Caption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ordinal: int = Field(ge=1, le=20)
    text: str = Field(min_length=1, max_length=2200)
    fact_ids: list[int] = Field(default_factory=list)


class CaptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    descriptions: list[Caption] = Field(min_length=20, max_length=20)


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_external_request: bool = False
    retry_generation_id: str | None = None


class ContentReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_facts: bool = False
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


CLAIM_PATTERN = re.compile(
    r"(?i)\b(?:cure|treat|heal|guarantee|certified|clinically|FDA|discount|price|peso[s]?|"
    r"cura|curar|trata|alivia|garantiz\w*|certific\w*|clínic\w*|descuento|precio|"
    r"emagre\w*|rejuvenes\w*|clinica\w*|promet\w*)\b|治愈|治疗|认证|保证|临床|折扣|减肥|降价|\d+\s*[%$€¥]"
)


def validate_captions(value, config, verified_facts):
    parsed = CaptionResponse.model_validate(value)
    rows = sorted(parsed.descriptions, key=lambda row: row.ordinal)
    if [row.ordinal for row in rows] != list(range(1, 21)):
        raise ValueError("DESCRIPTION_ORDINALS_MUST_BE_01_TO_20")
    texts = [" ".join(row.text.split()) for row in rows]
    if len(set(text.casefold() for text in texts)) != 20:
        raise ValueError("DESCRIPTION_DUPLICATES")
    output = []
    for row, text in zip(rows, texts):
        if len(text) > config.max_characters or any(index < 1 or index > len(verified_facts) for index in row.fact_ids):
            raise ValueError("DESCRIPTION_INVALID_LENGTH_OR_FACT_REFERENCE")
        # A conservative claim gate, not a promise of semantic truth. Ambiguous claims wait for review.
        if CLAIM_PATTERN.search(text):
            raise ValueError("DESCRIPTION_CLAIM_REQUIRES_REVIEW")
        output.append({"id": str(row.ordinal).zfill(2), "text": text, "fact_ids": row.fact_ids,
                       "weight": 1, "enabled": True, "source": "AI_GENERATED", "status": "VALIDATED"})
    return output


class DescriptionGenerationService:
    def __init__(self, publishing, source_service, settings):
        self.publishing, self.sources, self.settings = publishing, source_service, settings
        self.config = publishing.config.description_ai
        self.root = publishing.config.output_root / "descriptions"
        self.jobs, self.cancellations = {}, {}

    def get(self, generation_id):
        with self.publishing.session() as session:
            row = self.publishing.get(session, m.DescriptionGeneration, generation_id)
            result = record(row)
            product = self.publishing.get(session, m.Product, row.product_id)
            current = self.product_snapshot(product)
            result["current_product"] = current
            if row.status == "READY":
                result["evidence"] = {**result["evidence"], "content_review": {"status": current["status"], "sha256": current["sha256"]}}
            return result

    @staticmethod
    def product_snapshot(product):
        context = product.publishing_context or {}
        required = bool(context.get("description_review_required") or context.get("description_generation_id"))
        status = "EMPTY" if not product.descriptions else "APPROVED" if required and description_review_ready(record(product)) else "REQUIRED" if required else "NOT_REQUIRED"
        return {"product_id": product.id, "descriptions": product.descriptions, "sha256": description_pool_hash(product.descriptions),
                "status": status, "required": required, "generation_id": context.get("description_generation_id")}

    def product_review(self, product_id):
        with self.publishing.session() as session:
            return self.product_snapshot(self.publishing.get(session, m.Product, product_id))

    def refresh_config(self):
        self.config = load_publishing_config().description_ai
        return self.config

    def list(self, product_id):
        with self.publishing.session() as session:
            ids = list(session.scalars(select(m.DescriptionGeneration.id).where(
                m.DescriptionGeneration.product_id == product_id).order_by(m.DescriptionGeneration.created_at.desc())))
        return [self.get(generation_id) for generation_id in ids]

    def content_review(self, generation_id, request):
        state = self.get(generation_id)
        self.review_product_content(state["product_id"], request, generation_id=generation_id)
        return self.get(generation_id)

    def review_product_content(self, product_id, request, *, generation_id=None):
        if not request.confirm_facts:
            raise HTTPException(422, "CONFIRM_DESCRIPTION_FACTS_REQUIRED")
        with self.publishing.session() as session:
            product = self.publishing.get(session, m.Product, product_id)
            current_generation_id = product.publishing_context.get("description_generation_id")
            generation = self.publishing.get(session, m.DescriptionGeneration, current_generation_id) if current_generation_id else None
            digest = description_pool_hash(product.descriptions)
            if not product.descriptions:
                raise HTTPException(409, "DESCRIPTION_POOL_EMPTY")
            if (generation_id and current_generation_id != generation_id) or (generation and (generation.product_id != product.id or generation.status != "READY")) or digest != request.sha256:
                raise HTTPException(409, "DESCRIPTION_CHANGED_REVIEW_CURRENT_TEXT")
            review = {"sha256": digest, "product_id": product.id, "generation_id": current_generation_id, "confirmed_at": m.utc_now().isoformat(), "source": "USER_CONFIRMED"}
            product.publishing_context = {**product.publishing_context, "description_review": review}
            if generation:
                generation.evidence = {**generation.evidence, "content_review": review}
            session.add(m.AuditLog(id=new_id("audit"), action="publishing.description_content_confirmed", entity_id=generation_id or product.id,
                message=json.dumps(review), created_at=m.utc_now()))
        return self.product_review(product_id)

    def update(self, generation_id, **values):
        with self.publishing.session() as session:
            row = self.publishing.get(session, m.DescriptionGeneration, generation_id)
            for key, value in values.items():
                setattr(row, key, value)

    async def create(self, product_id, request):
        if not request.confirm_external_request:
            raise HTTPException(422, "CONFIRM_DESCRIPTION_EXTERNAL_REQUEST：会向已配置服务发送产品资料并可能计费")
        readiness = self.config.readiness()
        if readiness["status"] != "READY":
            raise HTTPException(409, readiness)
        with self.publishing.session() as session:
            product = self.publishing.get(session, m.Product, product_id)
            keyword = product.publishing_context.get("keyword") or (product.keywords[0] if len(product.keywords) == 1 else "")
            if not keyword:
                raise HTTPException(409, "PRODUCT_KEYWORD_AMBIGUOUS_OR_MISSING")
            context = dict(product.publishing_context)
            facts = context.get("verified_facts", [])
            if not isinstance(facts, list) or any(not isinstance(fact, str) for fact in facts):
                raise HTTPException(422, "VERIFIED_FACTS_MUST_BE_TEXT_LIST")
            data = {"product_id": product.id, "product_name": product.name, "country": product.country, "keyword": keyword,
                    "product_link_id": product.platform_product_id, "verified_facts": facts,
                    "source_context": context, "existing_descriptions": product.descriptions,
                    "ai_config": self.config.model_dump(mode="json")}
            if request.retry_generation_id:
                previous = self.publishing.get(session, m.DescriptionGeneration, request.retry_generation_id)
                if previous.product_id != product_id or previous.error_code not in {"DESCRIPTION_INPUT_OR_RESPONSE_INVALID", "DESCRIPTION_API_HTTP_400", "DESCRIPTION_API_HTTP_401", "DESCRIPTION_API_HTTP_403", "DESCRIPTION_API_HTTP_422", "DESCRIPTION_API_HTTP_429"}:
                    raise HTTPException(409, "DESCRIPTION_RETRY_NOT_SAFE：未知请求不能重新提交")
                data["retry_generation_id"] = previous.id
            fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            old = session.scalar(select(m.DescriptionGeneration).where(m.DescriptionGeneration.input_fingerprint == fingerprint))
            if old:
                return record(old)
            row = m.DescriptionGeneration(id=new_id("dgen"), product_id=product_id, country=product.country, keyword=keyword,
                model=self.config.model, prompt_version=self.config.prompt_version, input_fingerprint=fingerprint,
                status="QUEUED", request=data, descriptions=[], evidence={})
            session.add(row)
            session.flush()
            result = record(row)
        generation_id = result["id"]
        self.cancellations[generation_id] = threading.Event()
        self.jobs[generation_id] = asyncio.create_task(self.generate(generation_id))
        return result

    async def source_row(self, data):
        context = data["source_context"]
        if not context.get("source_document"):
            if data["ai_config"]["sync_to_tencent"]:
                raise ValueError("PRODUCT_SOURCE_MAPPING_MISSING：先从腾讯文档导入实际行，不能按keyword猜国家/账号")
            return None
        job = await self.sources.create(context["source_document"])
        await self.sources.jobs[job["id"]]
        source = self.sources.status(job["id"])
        if source["status"] != "READY":
            raise ValueError("TENCENT_SOURCE_NOT_READY:" + source.get("error_code", source["status"]))
        matches = [row for row in source["rows"] if row["source_row"] == context.get("source_row")
            and row["country"] == data["country"] and row["keyword"] == data["keyword"]
            and row["account_name"] == context.get("account_name", "")
            and row["telephone_number"] == context.get("telephone_number", "")]
        if len(matches) != 1:
            raise ValueError("SOURCE_ROW_IDENTITY_CHANGED")
        row = {**matches[0], "headers": source["headers"]}
        if set(row["description_columns"]) != {str(i).zfill(2) for i in range(1, 21)}:
            raise ValueError("TWENTY_DESCRIPTION_HEADERS_REQUIRED：实际表头不足20项，不新增/覆盖云文档列")
        return row

    async def generate(self, generation_id):
        data = self.get(generation_id)["request"]
        config = DescriptionAIConfig.model_validate(data["ai_config"])
        directory = self.root / generation_id
        directory.mkdir(parents=True, exist_ok=True)
        try:
            self.update(generation_id, status="READING_SOURCE")
            source = await self.source_row(data)
            existing = {row["id"]: row for row in data["existing_descriptions"]}
            if source:
                existing.update({row["id"]: row for row in source["descriptions"]})
                protected = source.get("protected_description_ordinals", [])
                if any(ordinal not in existing for ordinal in protected):
                    raise ValueError("DESCRIPTION_FORMULA_PROTECTED")
            else:
                protected = []
            if len(existing) == 20:
                self.update(generation_id, status="GENERATED", descriptions=list(existing.values()), evidence={"source": source, "model_called": False})
            else:
                inputs = {key: data[key] for key in ("product_name", "country", "keyword", "product_link_id", "verified_facts")}
                inputs.update(language=config.country_languages.get(data["country"], "local market language") if config.language == "auto" else config.language,
                    country_rules=config.country_rules.get(data["country"], ""), max_characters=config.max_characters)
                payload = {"model": config.model, "temperature": config.temperature, "max_tokens": config.max_tokens,
                    "messages": [{"role": "system", "content": config.prompt_template + "\nReturn only JSON conforming to this schema: " + json.dumps(CaptionResponse.model_json_schema())},
                                 {"role": "user", "content": json.dumps(inputs, ensure_ascii=False)}]}
                write_json(directory / "request.json", {"generation_id": generation_id, "payload": payload,
                    "input_fingerprint": self.get(generation_id)["input_fingerprint"], "source": source})
                self.update(generation_id, status="SUBMITTED", submitted_at=m.utc_now(), evidence={"source": source, "model_called": True, "pid": os.getpid()})
                async with httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=False) as client:
                    response = await client.post(config.base_url + "/chat/completions", json=payload,
                        headers={"Authorization": "Bearer " + os.environ[config.api_key_env]})
                if not response.is_success:
                    self.update(generation_id, status="FAILED", error_code="DESCRIPTION_API_HTTP_" + str(response.status_code),
                        error_message="服务返回非成功状态；响应正文未写入日志，未自动重试", finished_at=m.utc_now())
                    return
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                key = os.environ[config.api_key_env]
                usage = body.get("usage", {})
                usage = {name: value for name, value in usage.items() if name in {"prompt_tokens", "completion_tokens", "total_tokens"}
                         and type(value) is int and value >= 0} if isinstance(usage, dict) else {}
                request_id = body.get("id", "")
                request_id = request_id.replace(key, "[REDACTED]")[:200] if isinstance(request_id, str) else ""
                finish_reason = body["choices"][0].get("finish_reason")
                finish_reason = finish_reason if isinstance(finish_reason, str) and finish_reason in {"stop", "length", "content_filter", "tool_calls", "function_call"} else None
                response_evidence = {"source": source, "model_called": True, "pid": os.getpid(),
                    "request_id": request_id, "usage": usage,
                    "http_status": response.status_code, "finish_reason": finish_reason}
                stored_content = content.replace(key, "[REDACTED]") if isinstance(content, str) else None
                write_json(directory / "response.json", {"content": stored_content, "evidence": response_evidence})
                self.update(generation_id, evidence=response_evidence)
                if not isinstance(content, str) or key in content:
                    raise ValueError("DESCRIPTION_RESPONSE_INVALID_OR_CONTAINS_CREDENTIAL")
                # Providers may wrap JSON in a fenced code block; nothing is executed.
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
                generated = validate_captions(json.loads(content), config, data["verified_facts"])
                descriptions = [existing.get(row["id"], row) for row in generated]
                evidence = {**response_evidence,
                            "generated_sha256": hashlib.sha256(json.dumps(generated, ensure_ascii=False).encode()).hexdigest()}
                write_json(directory / "validated-response.json", {"descriptions": generated, "evidence": evidence})
                self.update(generation_id, status="GENERATED", descriptions=descriptions, evidence=evidence)
            await self.sync(generation_id)
        except (httpx.TimeoutException, httpx.NetworkError, asyncio.CancelledError):
            state = self.get(generation_id)
            submitted = state["status"] == "SUBMITTED"
            self.update(generation_id, status="WAITING_MANUAL", error_code="UNKNOWN_EXTERNAL_REQUEST" if submitted else "DESCRIPTION_PROCESS_INTERRUPTED",
                error_message="请求结果未知，不自动重复付费；保留本次请求和已完成阶段", finished_at=m.utc_now())
        except (OSError, RuntimeError, ValueError, ValidationError, KeyError, TypeError) as exc:
            message = str(exc)[:800]
            if isinstance(exc, ValidationError):
                fields = ", ".join(".".join(str(part) for part in item["loc"]) + ":" + item["type"] for item in exc.errors(include_input=False, include_url=False))
                message = "模型响应结构未通过验证：" + fields[:650] + "；未写入文档，不自动重试"
            message = message.replace(os.environ.get(config.api_key_env, "\0"), "[REDACTED]")
            self.update(generation_id, status="WAITING_MANUAL", error_code="DESCRIPTION_INPUT_OR_RESPONSE_INVALID",
                error_message=message, finished_at=m.utc_now())

    async def sync_job(self, generation_id):
        try:
            await self.sync(generation_id)
        except (OSError, RuntimeError, ValueError) as exc:
            self.update(generation_id, status="WAITING_MANUAL", error_code="TENCENT_SYNC_NOT_VERIFIED",
                        error_message=str(exc)[:800], finished_at=m.utc_now())

    async def sync(self, generation_id):
        state = self.get(generation_id)
        if not state["descriptions"] or state["status"] == "SUBMITTED":
            raise HTTPException(409, "NO_VALIDATED_DESCRIPTIONS_TO_SYNC")
        if state["status"] == "READY":
            return state
        data, source = state["request"], state["evidence"].get("source")
        config = DescriptionAIConfig.model_validate(data["ai_config"])
        directory = self.root / generation_id
        descriptions = state["descriptions"]
        evidence = dict(state["evidence"])
        if config.sync_to_tencent:
            if not source:
                raise ValueError("PRODUCT_SOURCE_MAPPING_MISSING")
            self.update(generation_id, status="SYNCING")
            write_json(directory / "sync-request.json", {"source": source, "descriptions": descriptions,
                       "tiktok_auto_root": str(self.settings.legacy_tiktok_auto_root), "tencent_export_timeout_s": config.tencent_export_timeout_seconds})
            def event(item):
                if item.get("type") == "PROCESS_STARTED":
                    self.update(generation_id, evidence={**evidence, "sync_pid": item["pid"]})
            process = await asyncio.to_thread(ProcessRunner().run,
                [str(self.settings.legacy_tiktok_auto_python), str(Path(__file__).with_name("description_sync_worker.py")),
                 "--job-dir", str(directory), "--video-auto-root", str(self.settings.legacy_video_auto_root)],
                cwd=directory, log_dir=directory / "sync-logs", timeout_s=300, on_event=event,
                cancel_event=self.cancellations.setdefault(generation_id, threading.Event()))
            path = directory / "sync-result.json"
            result = json.loads(path.read_text()) if path.is_file() else {"error_code": "SYNC_OUTPUT_NOT_FOUND"}
            evidence.update(sync_pid=process.pid, sync_exit_code=process.exit_code, sync_log_path=process.log_path, readback=result)
            if process.exit_code or result.get("status") != "READY":
                self.update(generation_id, status="WAITING_MANUAL", evidence=evidence,
                    error_code=result.get("error_code", "TENCENT_SYNC_NOT_VERIFIED"), error_message=result.get("error_message", "写回未确认，已保存生成文案；仅重核云文档，不重调AI"), finished_at=m.utc_now())
                return self.get(generation_id)
            descriptions = result["descriptions"]
        with self.publishing.session() as session:
            product = self.publishing.get(session, m.Product, data["product_id"])
            # Preserve edits made while the external request was running.
            existing = {row["id"]: row for row in product.descriptions}
            merged = {row["id"]: row for row in descriptions}
            for ordinal, row in existing.items():
                if ordinal in merged and row["text"] != merged[ordinal]["text"]:
                    raise ValueError("LOCAL_DESCRIPTION_CHANGED_DURING_GENERATION")
                merged[ordinal] = row
            product.descriptions = list(merged.values())
            product.publishing_context = {**product.publishing_context, "description_generation_id": generation_id,
                "description_model": config.model, "description_prompt_version": config.prompt_version,
                "description_status": "READY", "description_review_required": True, "description_source": "TENCENT_READBACK" if config.sync_to_tencent else "AI_LOCAL"}
            generation = self.publishing.get(session, m.DescriptionGeneration, generation_id)
            generation.status, generation.descriptions, generation.evidence = "READY", descriptions, evidence
            generation.evidence = {**evidence, "content_review": {"status": "REQUIRED", "sha256": description_pool_hash(product.descriptions),
                "reason": "结构校验与云表读回不代表事实正确；核实使用场景、材质、功效等未经证实的表述后才能发布"}}
            generation.error_code, generation.error_message, generation.finished_at = None, None, m.utc_now()
        return self.get(generation_id)

    async def recover(self):
        with self.publishing.session() as session:
            for row in session.scalars(select(m.DescriptionGeneration).where(m.DescriptionGeneration.status.in_(["QUEUED", "READING_SOURCE", "SUBMITTED", "SYNCING"]))):
                row.error_code = "UNKNOWN_EXTERNAL_REQUEST" if row.status == "SUBMITTED" else "DESCRIPTION_PROCESS_INTERRUPTED"
                row.error_message = "进程中断；保留请求/已生成文案，等待明确核实，不自动重复调用模型"
                row.status = "WAITING_MANUAL"

    async def shutdown(self):
        for event in self.cancellations.values():
            event.set()
        for job in self.jobs.values():
            if not job.done():
                job.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)


def create_description_router(service):
    router = APIRouter(prefix="/api/publishing", tags=["description-generation"])

    @router.get("/descriptions/config")
    def config():
        service.refresh_config()
        return {**service.config.model_dump(mode="json"), "readiness": service.config.readiness()}

    @router.post("/products/{product_id}/descriptions/generate", status_code=202)
    async def generate(product_id: str, request: GenerationRequest):
        service.refresh_config()
        return await service.create(product_id, request)

    @router.get("/products/{product_id}/descriptions/generations")
    def generations(product_id: str):
        return service.list(product_id)

    @router.get("/products/{product_id}/descriptions/content-review")
    def product_review(product_id: str):
        return service.product_review(product_id)

    @router.post("/products/{product_id}/descriptions/content-review")
    def review_product_content(product_id: str, request: ContentReviewRequest):
        return service.review_product_content(product_id, request)

    @router.get("/descriptions/{generation_id}")
    def status(generation_id: str):
        return service.get(generation_id)

    @router.post("/descriptions/{generation_id}/content-review")
    def content_review(generation_id: str, request: ContentReviewRequest):
        return service.content_review(generation_id, request)

    @router.post("/descriptions/{generation_id}/sync", status_code=202)
    async def sync(generation_id: str):
        state = service.get(generation_id)
        if state["status"] in {"QUEUED", "READING_SOURCE", "SUBMITTED", "SYNCING"}:
            raise HTTPException(409, "DESCRIPTION_ALREADY_RUNNING")
        if not state["descriptions"]:
            raise HTTPException(409, "NO_VALIDATED_DESCRIPTIONS_TO_SYNC")
        if state["status"] == "READY":
            return state
        service.jobs[generation_id] = asyncio.create_task(service.sync_job(generation_id))
        return {**state, "status": "SYNCING"}

    return router
