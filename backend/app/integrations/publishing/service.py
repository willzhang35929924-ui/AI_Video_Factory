import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import models as m
from app.domain import new_id
from app.repository import context_id, record
from app.integrations.publishing.description_selector import DescriptionSelector, description_pool_hash, description_review_ready
from app.integrations.publishing.browser_adapter import ExistingHubBrowserAdapter, product_receipt, studio_readback
from app.integrations.publishing.hubstudio import HubStudioAdapter

STEPS = ("PREFLIGHT", "SELECT_DESCRIPTION", "UPLOAD_VIDEO", "FILL_DESCRIPTION", "ATTACH_PRODUCT", "CONTENT_PRECHECK", "FINAL_CONFIRMATION", "SUBMIT", "VERIFY_RESULT")
UNRESOLVED = {"SUBMITTING", "SUBMITTED", "UNKNOWN", "UNKNOWN_EXTERNAL_RESULT"}
FLOW_STATES = {"PREFLIGHT": "UPLOAD_READY", "SELECT_DESCRIPTION": "DESCRIPTION_READY",
    "UPLOAD_VIDEO": "UPLOAD_VERIFIED", "FILL_DESCRIPTION": "DESCRIPTION_READY",
    "ATTACH_PRODUCT": "PRODUCT_CONFIRMED", "CONTENT_PRECHECK": "CONTENT_REVIEW_PASSED",
    "FINAL_CONFIRMATION": "PUBLIC_SUBMISSION_READY", "SUBMIT": "SUBMITTING", "VERIFY_RESULT": "PUBLISHED_VERIFIED"}
SECRET_KEYS = ("password", "secret", "token", "cookie", "authorization", "api_key", "密码", "密钥", "令牌")


class _ClaimBusy(Exception):
    pass


def safe(value):
    if isinstance(value, dict):
        return {key: safe(item) for key, item in value.items() if not any(word in key.lower() for word in SECRET_KEYS)}
    if isinstance(value, list):
        return [safe(item) for item in value]
    return value


def aware(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


class PublishingService:
    def __init__(self, repository, config, adapter=None):
        self.repository = repository
        self.config = config
        self.adapter = adapter or HubStudioAdapter(config)
        self.selector = DescriptionSelector()
        self.worker = None
        self.closing = False

    @contextmanager
    def session(self):
        with self.repository.lock, Session(self.repository.engine) as session, session.begin():
            yield session

    @staticmethod
    def get(session, model, entity_id):
        row = session.get(model, entity_id)
        if row is None:
            raise HTTPException(404, "PUBLISHING_ENTITY_NOT_FOUND")
        return row

    @staticmethod
    def country(session, name):
        row = session.scalar(select(m.Country).where(m.Country.name == name))
        if row is None:
            row = m.Country(id=context_id("country", name), name=name)
            session.add(row)
            session.flush()
        return row.id

    def fresh(self, evidence):
        try:
            timestamp = aware(datetime.fromisoformat(evidence.get("verified_at", "").replace("Z", "+00:00")))
        except (ValueError, TypeError, AttributeError):
            return False
        return 0 <= (m.utc_now() - timestamp).total_seconds() <= self.config.verification_max_age_seconds

    def account_row(self, session, row):
        values = safe(record(row))
        values.pop("country_id", None)
        values["product_ids"] = list(session.scalars(select(m.AccountProduct.product_id).where(m.AccountProduct.account_id == row.id)))
        identity, vpn = row.identity_verification, row.vpn_verification
        identity_ok = identity.get("source") == "OBSERVED_BROWSER" and identity.get("username") == row.expected_username and bool(row.expected_username) and identity.get("environment_id") == row.hubstudio_env_id and self.fresh(identity)
        vpn_ok = vpn.get("source") == "OBSERVED_NETWORK" and vpn.get("country") == row.country and self.fresh(vpn)
        values["identity_status"] = "OBSERVED" if identity_ok else "WAITING_MANUAL"
        values["network_status"] = "OBSERVED" if vpn_ok else "WAITING_MANUAL"
        values["legacy_status"] = row.status
        values["status"] = "DISABLED" if not row.enabled else "EXPIRED" if row.status in {"EXPIRED", "expired"} else "CONNECTED" if identity_ok and vpn_ok else "WAITING_MANUAL" if row.cdp_endpoint else "NOT_CONNECTED"
        values["connection_verified"] = identity_ok and vpn_ok
        return values

    def products(self):
        with self.session() as session:
            real_task_products = select(m.Task.product_id).where(m.Task.execution_mode == "integration")
            counts = dict(session.execute(select(m.Task.product_id, func.count()).where(m.Task.execution_mode == "integration").group_by(m.Task.product_id)).all())
            return [{**safe(record(row)), "integration_task_count": counts.get(row.id, 0)} for row in session.scalars(select(m.Product).where(or_(m.Product.id.not_in(self.repository.mock_product_ids), m.Product.id.in_(real_task_products))).order_by(m.Product.id))]

    def save_product(self, payload, product_id=None):
        values = payload.model_dump(mode="json")
        if safe(values) != values:
            raise HTTPException(422, "CREDENTIALS_NOT_ALLOWED_IN_CATALOG")
        with self.session() as session:
            row = self.get(session, m.Product, product_id) if product_id else m.Product(id=new_id("prd"))
            if product_id and row.country != values["country"] and (session.scalar(select(m.Task.id).where(m.Task.product_id == product_id).limit(1)) or session.scalar(select(m.AccountProduct).where(m.AccountProduct.product_id == product_id).limit(1))):
                raise HTTPException(409, "REFERENCED_PRODUCT_COUNTRY_IMMUTABLE")
            if product_id and session.scalar(select(m.PublishingTask.id).where(m.PublishingTask.product_id == product_id, m.PublishingTask.status.in_(["QUEUED", "RUNNING"])).limit(1)):
                raise HTTPException(409, "PRODUCT_IN_ACTIVE_QUEUE")
            if product_id and session.scalar(select(m.DescriptionGeneration.id).where(m.DescriptionGeneration.product_id == product_id,
                    m.DescriptionGeneration.status.in_(["QUEUED", "READING_SOURCE", "SUBMITTED", "SYNCING"])).limit(1)):
                raise HTTPException(409, "DESCRIPTION_GENERATION_RUNNING：请等待文案写回校验完成后再编辑产品")
            protected_fields = {"description_generation_id", "description_review", "description_review_required", "description_reuse"}
            protected_context = {key: value for key, value in (row.publishing_context or {}).items() if key in protected_fields}
            values["publishing_context"] = {**{key: value for key, value in values["publishing_context"].items() if key not in protected_fields}, **protected_context}
            for key, value in values.items():
                setattr(row, key, value)
            row.country_id = self.country(session, row.country)
            session.add(row)
            session.flush()
            return safe(record(row))

    def description_reuse_snapshot(self, product):
        context = product.publishing_context or {}
        keywords = set(product.keywords)
        if context.get("keyword"):
            keywords.add(context["keyword"])
        if len(keywords) != 1 or not next(iter(keywords)).strip():
            raise HTTPException(409, "DESCRIPTION_REUSE_EXACT_KEYWORD_REQUIRED：产品必须有唯一、明确的关键词")
        image = Path(product.image).expanduser()
        if not image.is_absolute() or not image.is_file() or not any(image.resolve().is_relative_to(root) for root in self.config.source_roots):
            raise HTTPException(409, "DESCRIPTION_REUSE_PRODUCT_IMAGE_REQUIRED：需要可读取的真实产品参考图")
        snapshot = {"product_id": product.id, "name": product.name, "country": product.country,
            "keyword": next(iter(keywords)), "product_image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "pool_sha256": description_pool_hash(product.descriptions), "status": product.status,
            "source_context": safe(context)}
        snapshot["input_sha256"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return snapshot

    def description_reuse_options(self, product_id):
        with self.session() as session:
            target = self.get(session, m.Product, product_id)
            snapshot = self.description_reuse_snapshot(target)
            sources = []
            for product in session.scalars(select(m.Product).where(m.Product.id != product_id, m.Product.country == target.country,
                    m.Product.status == "active").order_by(m.Product.id)):
                if len(product.descriptions) != 20 or {row.get("id") for row in product.descriptions} != {str(i).zfill(2) for i in range(1, 21)} or any(not row.get("text", "").strip() for row in product.descriptions):
                    continue
                try:
                    source = self.description_reuse_snapshot(product)
                except HTTPException:
                    continue
                if source["keyword"] == snapshot["keyword"] and source["product_image_sha256"] == snapshot["product_image_sha256"]:
                    sources.append({**source, "descriptions": safe(product.descriptions),
                        "content_review": "NOT_REQUIRED" if not (product.publishing_context.get("description_review_required")
                            or product.publishing_context.get("description_generation_id")) else "APPROVED" if description_review_ready(record(product)) else "REQUIRED"})
            return {"target": snapshot, "sources": sources, "target_has_descriptions": bool(target.descriptions)}

    def reuse_descriptions(self, product_id, payload):
        if payload.confirmed is not True:
            raise HTTPException(422, "DESCRIPTION_REUSE_CONFIRM_REQUIRED：请明确选择文案来源；不会按关键词自动覆盖")
        with self.session() as session:
            target = self.get(session, m.Product, product_id)
            source = self.get(session, m.Product, payload.source_product_id)
            if source.id == target.id or source.status != "active" or target.status != "active":
                raise HTTPException(409, "DESCRIPTION_REUSE_ACTIVE_DISTINCT_PRODUCTS_REQUIRED")
            source_input, target_input = self.description_reuse_snapshot(source), self.description_reuse_snapshot(target)
            if any(source_input[key] != target_input[key] for key in ("country", "keyword", "product_image_sha256")):
                raise HTTPException(409, "DESCRIPTION_REUSE_PRODUCT_IDENTITY_MISMATCH：国家、精确关键词和实物图必须一致")
            if source_input["pool_sha256"] != payload.source_pool_sha256 or source_input["input_sha256"] != payload.source_input_sha256:
                raise HTTPException(409, "DESCRIPTION_REUSE_SOURCE_CHANGED：重新读取来源后再选择")
            lineage = (target.publishing_context or {}).get("description_reuse", {})
            if (lineage.get("source_product_id") == source.id and lineage.get("source_input_sha256") == payload.source_input_sha256
                    and lineage.get("target_input_sha256") == payload.target_input_sha256
                    and lineage.get("source_pool_sha256") == payload.source_pool_sha256 == target_input["pool_sha256"]):
                return {"reused": True, "product": safe(record(target)), "lineage": safe(lineage), "model_called": False, "cloud_document_modified": False}
            if target_input["input_sha256"] != payload.target_input_sha256:
                raise HTTPException(409, "DESCRIPTION_REUSE_TARGET_CHANGED：先保存产品修改，再重新读取来源")
            if target.descriptions:
                raise HTTPException(409, "DESCRIPTION_REUSE_TARGET_NOT_EMPTY：已有文案不会被覆盖")
            if len(source.descriptions) != 20 or {row.get("id") for row in source.descriptions} != {str(i).zfill(2) for i in range(1, 21)} or any(not row.get("text", "").strip() for row in source.descriptions):
                raise HTTPException(409, "DESCRIPTION_REUSE_TWENTY_SAVED_TEXTS_REQUIRED")
            if safe(source.descriptions) != source.descriptions:
                raise HTTPException(409, "DESCRIPTION_REUSE_CREDENTIALS_NOT_ALLOWED")
            for identity in (target.id, source.id):
                if session.scalar(select(m.DescriptionGeneration.id).where(m.DescriptionGeneration.product_id == identity,
                        or_(m.DescriptionGeneration.status.in_(["QUEUED", "READING_SOURCE", "SUBMITTED", "SYNCING"]),
                            m.DescriptionGeneration.error_code == "UNKNOWN_EXTERNAL_REQUEST")).limit(1)):
                    raise HTTPException(409, "DESCRIPTION_GENERATION_RUNNING：等待当前文案任务完成")
            if session.scalar(select(m.PublishingTask.id).where(m.PublishingTask.product_id == target.id,
                    or_(m.PublishingTask.status.in_(["QUEUED", "RUNNING"]), m.PublishingTask.submission_state.in_(UNRESOLVED))).limit(1)):
                raise HTTPException(409, "PRODUCT_IN_ACTIVE_OR_UNRESOLVED_PUBLISH：保留当前发布输入")
            generations = [{"id": row.id, "input_fingerprint": row.input_fingerprint, "status": row.status,
                "evidence": safe(row.evidence)} for row in session.scalars(select(m.DescriptionGeneration).where(
                    m.DescriptionGeneration.product_id == source.id, m.DescriptionGeneration.status == "READY").order_by(m.DescriptionGeneration.created_at))
                if description_pool_hash(row.descriptions) == payload.source_pool_sha256]
            source_audits = []
            for row in session.scalars(select(m.AuditLog).where(m.AuditLog.entity_id == source.id,
                    m.AuditLog.action.in_(["publishing.description_source_imported", "publishing.description_pool_reused"])).order_by(m.AuditLog.created_at)):
                try:
                    evidence = json.loads(row.message)
                except ValueError:
                    continue
                if isinstance(evidence, dict) and (evidence.get("sha256") or evidence.get("source_pool_sha256")) == payload.source_pool_sha256:
                    source_audits.append({"id": row.id, "action": row.action, "at": row.created_at.isoformat(), "evidence": safe(evidence)})
            context = target.publishing_context or {}
            lineage = {"id": new_id("dreuse"), "source_product_id": source.id, "target_product_id": target.id,
                "country": target.country, "keyword": target_input["keyword"], "product_image_sha256": target_input["product_image_sha256"],
                "source_pool_sha256": payload.source_pool_sha256, "source_input_sha256": payload.source_input_sha256,
                "target_input_sha256": payload.target_input_sha256, "source_context": source_input["source_context"],
                "source_generation_evidence": generations, "source_catalog_evidence": source_audits,
                "previous_target_review": context.get("description_review"),
                "applied_at": m.utc_now().isoformat(), "model_called": False, "cloud_document_modified": False, "content_review": "REQUIRED"}
            target.descriptions = json.loads(json.dumps(source.descriptions))
            target.publishing_context = {**context, "description_reuse": lineage, "description_review_required": True,
                "description_review": {"source": "REUSE_PENDING_FACT_REVIEW", "sha256": payload.source_pool_sha256}}
            session.add(m.AuditLog(id=new_id("audit"), action="publishing.description_pool_reused", entity_id=target.id,
                message=json.dumps(safe(lineage), ensure_ascii=False), created_at=m.utc_now()))
            session.flush()
            return {"reused": False, "product": safe(record(target)), "lineage": safe(lineage), "model_called": False, "cloud_document_modified": False}

    def delete_product(self, product_id):
        with self.session() as session:
            row = self.get(session, m.Product, product_id)
            for model in (m.Task, m.PublishingTask, m.AccountProduct, m.CollectorRule, m.Keyword, m.DescriptionUsage, m.DescriptionGeneration):
                if session.scalar(select(model).where(model.product_id == product_id).limit(1)):
                    raise HTTPException(409, "PRODUCT_REFERENCED")
            session.delete(row)
        return {"id": product_id, "deleted": True}

    def accounts(self):
        with self.session() as session:
            return [self.account_row(session, row) for row in session.scalars(select(m.PublishingAccount).where(m.PublishingAccount.id.not_in(self.repository.mock_account_ids)).order_by(m.PublishingAccount.id))]

    def save_account(self, payload, account_id=None):
        values = payload.model_dump(mode="json")
        if safe(values) != values:
            raise HTTPException(422, "CREDENTIALS_NOT_ALLOWED_IN_ACCOUNT")
        product_ids = values.pop("product_ids")
        with self.session() as session:
            row = self.get(session, m.PublishingAccount, account_id) if account_id else m.PublishingAccount(id=new_id("acc"), authorization="NOT_VERIFIED", queue=0, errors=[])
            if account_id and session.scalar(select(m.PublishingTask.id).where(m.PublishingTask.account_id == account_id, m.PublishingTask.status.in_(["QUEUED", "RUNNING"])).limit(1)):
                raise HTTPException(409, "ACCOUNT_IN_ACTIVE_QUEUE")
            for product_id in product_ids:
                if self.get(session, m.Product, product_id).country != values["country"]:
                    raise HTTPException(409, "ACCOUNT_PRODUCT_COUNTRY_MISMATCH")
            changed = any(getattr(row, key, None) != values[key] for key in ("country", "cdp_endpoint", "hubstudio_env_id", "expected_username"))
            if account_id:
                binding_changed = set(session.scalars(select(m.AccountProduct.product_id).where(m.AccountProduct.account_id == account_id))) != set(product_ids)
                held = session.scalar(select(m.ResourceLock.resource_key).join(m.PublishingTask, m.ResourceLock.publishing_task_id == m.PublishingTask.id).where(m.PublishingTask.account_id == account_id, m.ResourceLock.released_at.is_(None)).limit(1))
                if held and (changed or binding_changed):
                    raise HTTPException(409, "ACCOUNT_ENVIRONMENT_HAS_UNRESOLVED_CHECKPOINT")
            for key, value in values.items():
                setattr(row, key, value)
            row.country_id = self.country(session, row.country)
            if changed:
                row.identity_verification, row.vpn_verification = {}, {}
            session.add(row)
            session.flush()
            session.execute(delete(m.AccountProduct).where(m.AccountProduct.account_id == row.id))
            for product_id in dict.fromkeys(product_ids):
                session.add(m.AccountProduct(account_id=row.id, product_id=product_id))
            session.flush()
            return self.account_row(session, row)

    def control_account(self, account_id, enabled):
        with self.session() as session:
            row = self.get(session, m.PublishingAccount, account_id)
            row.enabled = enabled
            row.status = "WAITING_MANUAL" if enabled else "DISABLED"
            return self.account_row(session, row)

    def confirm_account(self, account_id, payload):
        with self.session() as session:
            row = self.get(session, m.PublishingAccount, account_id)
            if payload.country != row.country or payload.vpn_country != row.country or payload.username != row.expected_username:
                raise HTTPException(409, "ACCOUNT_CONFIRMATION_MISMATCH")
            row.identity_verification = {"source": "USER_ATTESTATION", "username": payload.username, "confirmed_at": m.utc_now().isoformat(), "note": payload.note}
            row.vpn_verification = {"source": "USER_ATTESTATION", "country": payload.vpn_country, "confirmed_at": m.utc_now().isoformat(), "note": payload.note}
            row.status = "WAITING_MANUAL"
            return self.account_row(session, row)

    def delete_account(self, account_id):
        with self.session() as session:
            row = self.get(session, m.PublishingAccount, account_id)
            if session.scalar(select(m.PublishingTask.id).where(m.PublishingTask.account_id == account_id).limit(1)) or session.scalar(select(m.DescriptionUsage.id).where(m.DescriptionUsage.account_id == account_id).limit(1)):
                raise HTTPException(409, "ACCOUNT_REFERENCED")
            session.execute(delete(m.AccountProduct).where(m.AccountProduct.account_id == account_id))
            session.delete(row)
        return {"id": account_id, "deleted": True}

    def asset_file(self, asset):
        if asset.registry_status != "registered" or asset.legacy_reference.get("execution_scope", "PRODUCTION") != "PRODUCTION" or asset.legacy_reference.get("superseded_by_person_asset_id") or asset.status.lower() in {"rejected", "failed", "media_deleted"}:
            raise HTTPException(409, "BUSINESS_ASSET_REQUIRED")
        path = Path(asset.external_path or asset.storage_path).expanduser()
        if not path.is_absolute():
            raise HTTPException(409, "ABSOLUTE_SOURCE_PATH_REQUIRED")
        path = path.resolve()
        if not any(path.is_relative_to(root) for root in self.config.source_roots):
            raise HTTPException(409, "SOURCE_PATH_NOT_ALLOWED")
        if not path.is_file() or not asset.sha256:
            raise HTTPException(409, "SOURCE_FILE_MISSING")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != asset.sha256:
            raise HTTPException(409, "SOURCE_HASH_CHANGED")
        return str(path)

    @staticmethod
    def latest_review(session, task_id, asset_id):
        return session.scalar(select(m.Review).where(m.Review.task_id == task_id, m.Review.asset_id == asset_id).order_by(m.Review.id.desc()).limit(1))

    def input(self, session, row, *, reconcile_submitted=False):
        task = self.get(session, m.Task, row.task_id)
        account = self.get(session, m.PublishingAccount, row.account_id)
        product = self.get(session, m.Product, row.product_id)
        asset = self.get(session, m.VideoAsset, row.video_id)
        readonly_submitted = reconcile_submitted and row.submission_state in {"SUBMITTED", "UNKNOWN"} and bool(row.checkpoint.get("submission_intent"))
        if (task.execution_mode != "integration" or task.status.upper() in {"CANCELLED", "REJECTED", "PAUSED", "SKIPPED"}
                or task.status.upper() == "FAILED" and not readonly_submitted):
            raise HTTPException(409, "SOURCE_TASK_NOT_PUBLISHABLE")
        if not (task.country == product.country == account.country == row.country) or task.product_id != product.id:
            raise HTTPException(409, "PUBLISH_COUNTRY_PRODUCT_MISMATCH")
        upload_only = row.checkpoint.get("execution_scope") == "UPLOAD_ONLY"
        if not upload_only and row.product_binding != (product.platform_product_id or product.binding_reference):
            raise HTTPException(409, "PUBLISH_PRODUCT_BINDING_CHANGED")
        if not upload_only and row.selected_description and row.selected_description.get("source") != "USER_PROVIDED" and not description_review_ready(record(product)):
            raise HTTPException(409, "DESCRIPTION_CONTENT_REVIEW_REQUIRED")
        if not session.get(m.AccountProduct, (account.id, product.id)):
            raise HTTPException(409, "ACCOUNT_PRODUCT_NOT_BOUND")
        if not account.enabled or account.status in {"EXPIRED", "DISABLED", "expired"} or product.status != "active":
            raise HTTPException(409, "ACCOUNT_PRODUCT_DISABLED")
        if asset.task_id != task.id or asset.type != "final" or asset.asset_type != "FINAL_VIDEO":
            raise HTTPException(409, "APPROVED_FINAL_REQUIRED")
        production = task.context.get("production", {})
        person_id = production.get("current_person_asset_id")
        composition_id = asset.legacy_reference.get("composition_asset_id")
        composition = session.get(m.VideoAsset, composition_id) if composition_id else None
        manual = task.context.get("manual_compose", {}).get("selection", {})
        manual_composition = bool(composition and composition.source_system == "FactoryManualCompose")
        if manual_composition:
            person_id = manual.get("person_asset_id")
        person = session.get(m.VideoAsset, person_id) if person_id else None
        if not composition or composition.task_id != task.id or composition.type != "compose" or not person or person.type != "person" or composition.legacy_reference.get("person_asset_id") != person_id:
            raise HTTPException(409, "CURRENT_REVIEW_LINEAGE_REQUIRED")
        donor = self.get(session, m.Task, person.task_id)
        if (donor.context.get("production", {}).get("current_person_asset_id") != person.id
                or donor.status.upper() in {"REJECTED", "CANCELLED", "SKIPPED"}
                or donor.status.upper() == "FAILED" and not readonly_submitted
                or person.task_id != task.id and (not manual_composition
                    or manual.get("person_task_id") != person.task_id
                    or composition.legacy_reference.get("person_task_id") != person.task_id)):
            raise HTTPException(409, "CURRENT_REVIEW_LINEAGE_REQUIRED")
        latest = self.latest_review(session, task.id, composition.id)
        if not latest or latest.status != "approved" or asset.legacy_reference.get("review_id") != latest.id:
            raise HTTPException(409, "CURRENT_COMPOSITION_NOT_APPROVED")
        final_review = self.latest_review(session, task.id, asset.id)
        person_review = self.latest_review(session, donor.id, person.id)
        if (task.status.upper() == "FAILED" or donor.status.upper() == "FAILED") and (not person_review or person_review.status != "approved"):
            raise HTTPException(409, "CURRENT_LINEAGE_REVIEW_BLOCKED")
        if (final_review and final_review.status != "approved") or (person_review and person_review.status != "approved"):
            raise HTTPException(409, "CURRENT_LINEAGE_REVIEW_BLOCKED")
        rejected = production.get("rejected_review", {})
        if rejected.get("asset_id") in {asset.id, composition.id, person.id} or donor.context.get("production", {}).get("rejected_review", {}).get("asset_id") == person.id:
            raise HTTPException(409, "CURRENT_LINEAGE_REJECTED")
        if session.scalar(select(m.ProcessingRun.id).where(m.ProcessingRun.task_id == task.id, or_(m.ProcessingRun.error_code == "UNKNOWN_EXTERNAL_REQUEST", m.ProcessingRun.resource_info["submission_state"].as_string() == "UNKNOWN")).limit(1)):
            raise HTTPException(409, "SOURCE_SUBMISSION_UNKNOWN")
        path = self.asset_file(asset)
        self.asset_file(composition)
        self.asset_file(person)
        if row.input_sha256 and row.input_sha256 != asset.sha256:
            raise HTTPException(409, "PUBLISH_INPUT_CHANGED")
        return {"publishing_task": safe(record(row)), "account": self.account_row(session, account), "product": safe(record(product)),
            "video": {"id": asset.id, "path": path, "sha256": asset.sha256, "duration": asset.duration,
                "composition_asset_id": composition.id, "person_asset_id": person.id},
            "product_match": {"mode": row.checkpoint.get("product_selection_mode", self.config.product_selection_mode),
                "expected_names": self.product_names(record(product)), "requested_id": product.platform_product_id},
            "checkpoint": safe(row.checkpoint), "config": self.config.public()}

    def create_task(self, payload):
        with self.session() as session:
            product = self.get(session, m.Product, payload.product_id)
            if session.scalar(select(m.PublishingTask.id).where(m.PublishingTask.video_id == payload.video_id, m.PublishingTask.account_id == payload.account_id, m.PublishingTask.status.not_in(["CANCELLED", "failed", "FAILED"])).limit(1)):
                raise HTTPException(409, "PUBLISH_TASK_ALREADY_EXISTS")
            values = payload.model_dump()
            upload_only = values.pop("upload_only")
            checkpoint = {"product_selection_mode": self.config.product_selection_mode, "production_state": "ASSET_SELECTED"}
            if upload_only:
                checkpoint["execution_scope"] = "UPLOAD_ONLY"
            row = m.PublishingTask(id=new_id("pub"), **values, country=product.country, product_binding=product.platform_product_id or product.binding_reference,
                status="DRAFT", current_step="PREFLIGHT", checkpoint=checkpoint, submission_state="NOT_SUBMITTED", moderation_status="NOT_CHECKED", product_link_status="NOT_CHECKED")
            self.input(session, row)
            row.checkpoint = {**row.checkpoint, "production_state": "LINEAGE_VALIDATED"}
            row.input_sha256 = self.get(session, m.VideoAsset, row.video_id).sha256
            session.add(row)
            session.flush()
            return safe(record(row))

    def tasks(self):
        with self.session() as session:
            scope = m.VideoAsset.legacy_reference["execution_scope"].as_string()
            rows = select(m.PublishingTask).join(m.Task, m.Task.id == m.PublishingTask.task_id).join(m.VideoAsset, m.VideoAsset.id == m.PublishingTask.video_id).where(
                m.Task.execution_mode == "integration", m.VideoAsset.type == "final", m.VideoAsset.asset_type == "FINAL_VIDEO", m.VideoAsset.registry_status == "registered",
                or_(scope.is_(None), scope == "PRODUCTION"),
                or_(m.Task.context["production"]["current_person_asset_id"].as_string().is_not(None),
                    m.Task.context["manual_compose"]["selection"]["person_asset_id"].as_string().is_not(None)))
            return [safe(record(row)) for row in session.scalars(rows.order_by(m.PublishingTask.created_at, m.PublishingTask.id))]

    def task(self, task_id):
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            return {**safe(record(row)), "runs": [safe(record(run)) for run in session.scalars(select(m.PublishRun).where(m.PublishRun.publishing_task_id == row.id).order_by(m.PublishRun.attempt))],
                "steps": [safe(record(step)) for step in session.scalars(select(m.StepExecution).where(m.StepExecution.publishing_task_id == row.id).order_by(m.StepExecution.started_at, m.StepExecution.id))]}

    def batches(self):
        with self.session() as session:
            return [{**safe(record(row)), "task_ids": list(session.scalars(select(m.PublishingTask.id).where(m.PublishingTask.batch_id == row.id).order_by(m.PublishingTask.created_at, m.PublishingTask.id)))} for row in session.scalars(select(m.PublishingBatch).order_by(m.PublishingBatch.created_at))]

    def queue(self):
        with self.session() as session:
            locks = [safe(record(row)) for row in session.scalars(select(m.ResourceLock).where(m.ResourceLock.released_at.is_(None)))]
        return {"batches": self.batches(), "tasks": self.tasks(), "locks": locks, "effective_concurrency": 1, "closing": self.closing}

    async def preflight(self, task_id):
        with self.session() as session:
            payload = self.input(session, self.get(session, m.PublishingTask, task_id))
        return await self.observe(payload)

    async def observe(self, payload):
        try:
            return self.verified_preflight(payload, await self.adapter.preflight(payload))
        except asyncio.CancelledError:
            raise
        except Exception:
            return {"status": "WAITING_MANUAL", "performed": False, "error_code": "PREFLIGHT_OBSERVATION_FAILED", "checks": [{"name": "browser_observation", "status": "WAITING_MANUAL", "reason": "PREFLIGHT_OBSERVATION_FAILED"}], "evidence": {}}

    @staticmethod
    def result(value):
        if not isinstance(value, dict) or value.get("status") not in ("READY", "WAITING_MANUAL", "FAILED") or not isinstance(value.get("evidence", {}), dict) or not isinstance(value.get("checkpoint", {}), dict):
            return {"status": "WAITING_MANUAL", "error_code": "INVALID_ADAPTER_RESULT", "evidence": {}}
        return safe(value)

    @staticmethod
    def product_names(product):
        values = [product.get("name"), *product.get("keywords", []), product.get("publishing_context", {}).get("keyword")]
        return list(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))

    @staticmethod
    def product_name_matches(selected, expected_names):
        selected = " ".join(selected.casefold().split())
        return any(selected == value or selected.startswith(value + " ") for value in (" ".join(name.casefold().split()) for name in expected_names))

    def product_intent(self, payload):
        account = payload["account"]
        return {"product_id": payload["product"]["id"], "account_id": account["id"], "environment_id": account["hubstudio_env_id"],
            "username": account["expected_username"], "video_sha256": payload["video"]["sha256"], "country": account["country"],
            "mode": payload["product_match"]["mode"], "expected_names": payload["product_match"]["expected_names"],
            "requested_id": payload["product_match"]["requested_id"]}

    def product_observation_valid(self, payload, result):
        evidence = result.get("evidence", {})
        account = payload["account"]
        if evidence.get("source") != "OBSERVED_BROWSER" or evidence.get("read_only") is not True or not self.fresh(evidence):
            return False
        expected = {"account_id": account["id"], "environment_id": account["hubstudio_env_id"], "username": account["expected_username"], "video_sha256": payload["video"]["sha256"]}
        if any(evidence.get(key) != value or not value for key, value in expected.items()):
            return False
        if any(evidence.get(key) is not False for key in ("attached", "confirmed", "publish_clicked")) or evidence.get("scope") != "VISIBLE_CATALOG_PAGE_ONLY":
            return False
        if not isinstance(evidence.get("url"), str) or not isinstance(evidence.get("screenshot"), str) or not evidence["screenshot"]:
            return False
        try:
            url = urlsplit(evidence["url"])
            screenshot = Path(evidence["screenshot"]).resolve()
            screenshot_ok = screenshot.is_relative_to(self.config.output_root) and screenshot.is_file() and screenshot.stat().st_size > 0
        except (ValueError, OSError):
            return False
        if url.scheme != "https" or url.hostname != "www.tiktok.com" or url.path != "/tiktokstudio/upload" or url.username or url.password or not screenshot_ok:
            return False
        candidates, count = evidence.get("candidates"), evidence.get("match_count")
        if not isinstance(candidates, list) or type(count) is not int or count < 0:
            return False
        mode = payload["checkpoint"].get("product_selection_mode", self.config.product_selection_mode)
        requested_id = payload["product"].get("platform_product_id")
        names = self.product_names(payload["product"])
        for candidate in candidates:
            if not isinstance(candidate, dict) or any(not isinstance(candidate.get(key), str) or not candidate[key].strip() for key in ("product_name", "product_id", "status")) or type(candidate.get("matches")) is not bool or type(candidate.get("selectable")) is not bool:
                return False
            matches = bool(requested_id) and candidate["product_id"] == requested_id if mode == "exact_id" else self.product_name_matches(candidate["product_name"], names)
            matches = matches and candidate["selectable"]
            if candidate["matches"] != matches:
                return False
        return count == sum(candidate["matches"] for candidate in candidates)

    def product_observation_input(self, session, row):
        if row.checkpoint.get('inflight_step') == 'ATTACH_PRODUCT' and row.checkpoint.get('external_outcome') == 'UNKNOWN':
            return self.product_reconciliation_input(session, row)
        if row.submission_state != "NOT_SUBMITTED" or row.status in {"QUEUED", "RUNNING", "CANCELLED"} or self.unknown(row):
            raise HTTPException(409, "PRODUCT_OBSERVATION_SAFE_CHECKPOINT_REQUIRED")
        if session.scalar(select(m.PublishRun.id).where(m.PublishRun.publishing_task_id == row.id, m.PublishRun.status == "RUNNING").limit(1)):
            raise HTTPException(409, "PUBLISH_RUN_STILL_ACTIVE")
        payload = self.input(session, row)
        completed = row.checkpoint.get("UPLOAD_VIDEO", {})
        if completed.get("status") != "READY" or not self.upload_completion_valid(session, row, completed):
            raise HTTPException(409, "VERIFIED_UPLOADED_VIDEO_REQUIRED")
        return payload

    async def observe_product(self, task_id):
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            payload = self.product_observation_input(session, row)
            original = {"account": payload["account"], "product": payload["product"], "video": payload["video"],
                "upload_intent": row.checkpoint.get("upload_intent"), "upload_proof": row.checkpoint.get("UPLOAD_VIDEO"),
                "mode": row.checkpoint.get("product_selection_mode", self.config.product_selection_mode), "status": row.status,
                "batch_status": self.get(session, m.PublishingBatch, row.batch_id).status}
        observed = await self.observe(payload)
        if observed["status"] == "READY":
            try:
                observed = self.result(await self.adapter.observe_product(payload))
            except asyncio.CancelledError:
                raise
            except Exception:
                observed = {"status": "WAITING_MANUAL", "error_code": "PRODUCT_OBSERVATION_FAILED", "evidence": {"read_only": True}}
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            current = self.product_observation_input(session, row)
            current_snapshot = {"account": current["account"], "product": current["product"], "video": current["video"],
                "upload_intent": row.checkpoint.get("upload_intent"), "upload_proof": row.checkpoint.get("UPLOAD_VIDEO"),
                "mode": row.checkpoint.get("product_selection_mode", self.config.product_selection_mode), "status": row.status,
                "batch_status": self.get(session, m.PublishingBatch, row.batch_id).status}
            if current_snapshot != original:
                raise HTTPException(409, "PRODUCT_OBSERVATION_INPUT_CHANGED")
            if observed["status"] == "READY" and not self.product_observation_valid(current, observed):
                observed = {"status": "WAITING_MANUAL", "error_code": "PRODUCT_OBSERVATION_EVIDENCE_REQUIRED", "evidence": {"read_only": True, "accepted": False}}
            count = observed.get("evidence", {}).get("match_count", 0)
            observed["match_status"] = ("MATCHED_CANDIDATE" if count == 1 else "MULTIPLE_VISIBLE_CANDIDATES" if count > 1 else "NO_MATCH_VISIBLE") if observed["status"] == "READY" else "WAITING_MANUAL"
            row.checkpoint = {**row.checkpoint, "product_observation": observed}
            row.updated_at = m.utc_now()
            session.add(m.AuditLog(id=new_id("audit"), task_id=row.task_id, action="publishing.product_observed", entity_id=row.id,
                message=f"{observed['status']} {observed['match_status']}; visible catalog only; no attachment, confirmation or publishing", created_at=m.utc_now()))
        evidence = observed.get("evidence", {})
        return {"status": observed["status"], "match_status": observed["match_status"], "candidates": evidence.get("candidates", []),
            "match_count": evidence.get("match_count"), "scope": evidence.get("scope"), "observed": observed,
            "task": self.task(task_id), "public_submission": "NOT_PERFORMED"}

    def verified_preflight(self, payload, result):
        result = self.result(result)
        evidence = result.get("evidence", {})
        account = payload["account"]
        identity, vpn = evidence.get("identity", {}), evidence.get("vpn", {})
        identity = identity if isinstance(identity, dict) else {}
        vpn = vpn if isinstance(vpn, dict) else {}
        checks = list(result.get("checks", []))
        identity_ok = identity.get("source") == "OBSERVED_BROWSER" and identity.get("username") == account.get("expected_username") and bool(identity.get("username")) and identity.get("environment_id") == account.get("hubstudio_env_id")
        vpn_ok = vpn.get("source") == "OBSERVED_NETWORK" and vpn.get("country") == account["country"] and bool(vpn.get("verified_at"))
        for item, valid in ((identity, identity_ok), (vpn, vpn_ok)):
            try:
                timestamp = aware(datetime.fromisoformat(item.get("verified_at", "").replace("Z", "+00:00")))
                valid = valid and 0 <= (m.utc_now() - timestamp).total_seconds() <= self.config.verification_max_age_seconds
            except (ValueError, TypeError):
                valid = False
            if item is identity:
                identity_ok = valid
            else:
                vpn_ok = valid
        if not identity_ok:
            checks.append({"name": "tiktok_identity", "status": "WAITING_MANUAL", "reason": "OBSERVED_CURRENT_USERNAME_REQUIRED"})
        if not vpn_ok:
            checks.append({"name": "vpn", "status": "WAITING_MANUAL", "reason": "OBSERVED_NETWORK_COUNTRY_REQUIRED"})
        upload_only = payload["checkpoint"].get("execution_scope") == "UPLOAD_ONLY"
        data_ready = True
        if upload_only:
            checks.extend([{"name": "product_link", "status": "NOT_REQUIRED", "reason": "UPLOAD_ONLY_NO_PRODUCT_ATTACHMENT"},
                {"name": "description", "status": "NOT_REQUIRED", "reason": "UPLOAD_ONLY_NO_PUBLIC_SUBMISSION"}])
        else:
            selection_mode = payload["checkpoint"].get("product_selection_mode", self.config.product_selection_mode)
            exact_id = selection_mode == "exact_id"
            product_ready = bool((payload["product"].get("platform_product_id") or "").strip()) if exact_id else bool(self.product_names(payload["product"]))
            description_ready = bool(payload["publishing_task"].get("caption", "").strip()) or any(item.get("enabled", True) and item.get("text", "").strip() for item in payload["product"].get("descriptions", []))
            selected = payload["publishing_task"].get("selected_description")
            manual_caption = bool(payload["publishing_task"].get("caption", "").strip()) and (not selected or selected.get("source") == "USER_PROVIDED")
            content_reviewed = manual_caption or description_review_ready(payload["product"])
            description_ready = description_ready and content_reviewed
            product_reason = "PRODUCT_LINK_ID_CONFIGURED" if product_ready else "REAL_PRODUCT_LINK_ID_REQUIRED"
            if not exact_id:
                product_reason = "MATCH_PRODUCT_DURING_SELECTION_NOT_CONFIRMED" if product_ready else "REAL_PRODUCT_NAME_OR_KEYWORD_REQUIRED"
            checks.extend([{"name": "product_link", "status": ("READY" if exact_id else "NOT_CHECKED") if product_ready else "DATA_INCOMPLETE", "reason": product_reason},
                {"name": "description", "status": "READY" if description_ready else "WAITING_MANUAL" if not content_reviewed else "DATA_INCOMPLETE", "reason": "EXISTING_TEXT_CONFIGURED" if description_ready else "DESCRIPTION_CONTENT_REVIEW_REQUIRED" if not content_reviewed else "EXISTING_DESCRIPTION_REQUIRED_NO_GENERATED_FACTS"}])
            data_ready = product_ready and description_ready
            if not data_ready:
                result["error_code"] = "PUBLISHING_DATA_INCOMPLETE"
        result.update(checks=checks, status="READY" if result.get("status") == "READY" and identity_ok and vpn_ok and data_ready else "WAITING_MANUAL")
        return result

    @staticmethod
    def unknown(row):
        return row.submission_state in UNRESOLVED or row.checkpoint.get("external_outcome") == "UNKNOWN"

    def ready_to_queue(self, session, row):
        if self.config.concurrency != 1:
            raise HTTPException(409, "CONCURRENCY_NOT_SUPPORTED")
        if self.unknown(row) or row.status in {"PUBLISHED", "published", "COMPLETED", "RUNNING", "QUEUED", "CANCELLED"}:
            raise HTTPException(409, "PUBLISH_RETRY_NOT_SAFE")
        self.input(session, row)

    @staticmethod
    def evidence_hash(value):
        return hashlib.sha256(json.dumps(safe(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def submission_snapshot(self, session, row):
        if not self.config.allow_publish or self.config.stop_after_upload or row.checkpoint.get("execution_scope") == "UPLOAD_ONLY":
            raise HTTPException(409, "PUBLIC_SUBMISSION_DISABLED")
        payload = self.input(session, row)
        if not payload["account"]["connection_verified"]:
            raise HTTPException(409, "OBSERVED_ACCOUNT_ENVIRONMENT_REQUIRED")
        content = row.checkpoint.get("CONTENT_PRECHECK", {})
        if content.get("status") != "READY" or not self.content_review_valid(session, row, content):
            raise HTTPException(409, "CONTENT_REVIEW_EVIDENCE_REQUIRED")
        attachment = row.checkpoint["ATTACH_PRODUCT"]["checkpoint"]
        snapshot = {"publishing_task_id": row.id, "source_task_id": row.task_id, "account_id": row.account_id,
            "username": payload["account"]["expected_username"], "country": row.country,
            "environment_id": payload["account"]["hubstudio_env_id"], "cdp_endpoint": payload["account"]["cdp_endpoint"],
            "video_id": row.video_id, "video_sha256": row.input_sha256,
            "person_asset_id": payload["video"]["person_asset_id"], "composition_asset_id": payload["video"]["composition_asset_id"],
            "product_id": row.product_id, "selected_product_id": attachment["selected_id"], "selected_product_name": attachment["selected_name"],
            "description_id": row.selected_description["id"], "description_sha256": hashlib.sha256(row.caption.encode()).hexdigest(),
            "content_review_sha256": self.evidence_hash(content)}
        return {"snapshot": snapshot, "fingerprint": self.evidence_hash(snapshot), "caption": row.caption,
            "content_review": safe(content["evidence"]), "status": "READY_FOR_SINGLE_TASK_CONFIRMATION"}

    def submit_confirmation(self, task_id):
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            if row.submission_state != "NOT_SUBMITTED" or self.unknown(row):
                raise HTTPException(409, "SUBMISSION_ALREADY_STARTED_RECONCILE_ONLY")
            return self.submission_snapshot(session, row)

    def confirmation_valid(self, session, row):
        confirmation = row.checkpoint.get("single_task_confirmation", {})
        if confirmation.get("source") != "USER_SINGLE_TASK_CONFIRMATION" or confirmation.get("confirmed") is not True:
            return False
        snapshot = self.submission_snapshot(session, row)
        return confirmation.get("fingerprint") == snapshot["fingerprint"] and confirmation.get("snapshot") == snapshot["snapshot"]

    async def confirm_submit(self, task_id, request):
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            self.ready_to_queue(session, row)
            if session.scalar(select(m.PublishRun.id).where(m.PublishRun.publishing_task_id == row.id, m.PublishRun.status == "RUNNING").limit(1)):
                raise HTTPException(409, "PUBLISH_RUN_STILL_ACTIVE")
            if not request.confirmed:
                raise HTTPException(409, "EXPLICIT_SINGLE_TASK_PUBLISH_CONFIRMATION_REQUIRED")
            if not row.batch_id or self.get(session, m.PublishingBatch, row.batch_id).status == "CANCELLED":
                raise HTTPException(409, "ACTIVE_PUBLISH_BATCH_REQUIRED")
            snapshot = self.submission_snapshot(session, row)
            if request.fingerprint != snapshot["fingerprint"]:
                raise HTTPException(409, "PUBLISH_CONFIRMATION_INPUT_CHANGED")
            row.checkpoint = {**row.checkpoint, "single_task_confirmation": {"source": "USER_SINGLE_TASK_CONFIRMATION",
                "confirmed": True, "confirmed_at": m.utc_now().isoformat(), "fingerprint": snapshot["fingerprint"],
                "snapshot": snapshot["snapshot"], "note": request.note}}
        await self.retry(task_id)
        return {"accepted": True, "task_id": task_id, "status": "QUEUED", "message": "仅此任务已确认；后台复核所有身份、商品、文案和审查证据后才提交"}

    async def create_batch(self, task_ids):
        if self.config.concurrency != 1:
            raise HTTPException(409, "CONCURRENCY_NOT_SUPPORTED")
        if not task_ids or len(set(task_ids)) != len(task_ids):
            raise HTTPException(422, "UNIQUE_PUBLISHING_TASK_IDS_REQUIRED")
        with self.session() as session:
            rows = [self.get(session, m.PublishingTask, task_id) for task_id in task_ids]
            for row in rows:
                self.ready_to_queue(session, row)
                if row.batch_id:
                    raise HTTPException(409, "USE_EXISTING_BATCH_RESUME_OR_TASK_RETRY")
            batch = m.PublishingBatch(id=new_id("pbt"), status="QUEUED", concurrency=1, settings_snapshot=self.config.public())
            session.add(batch)
            session.flush()
            for row in rows:
                row.batch_id, row.status, row.error, row.updated_at = batch.id, "QUEUED", None, m.utc_now()
            result = {**record(batch), "task_ids": task_ids}
        self.launch()
        return result

    def launch(self):
        if not self.closing and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self.work(), name="isolated-publishing-worker")

    async def retry(self, task_id):
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            self.ready_to_queue(session, row)
            if not row.batch_id:
                raise HTTPException(409, "PUBLISH_BATCH_REQUIRED")
            batch = self.get(session, m.PublishingBatch, row.batch_id)
            if batch.status == "CANCELLED":
                raise HTTPException(409, "USER_CANCELLED_BATCH")
            row.status, row.error, row.updated_at = "QUEUED", None, m.utc_now()
            batch.status, batch.updated_at = "QUEUED", m.utc_now()
            result = safe(record(row))
        self.launch()
        return result

    async def control(self, batch_id, action):
        with self.session() as session:
            batch = self.get(session, m.PublishingBatch, batch_id)
            rows = list(session.scalars(select(m.PublishingTask).where(m.PublishingTask.batch_id == batch.id)))
            if action == "resume":
                if self.config.concurrency != 1:
                    raise HTTPException(409, "CONCURRENCY_NOT_SUPPORTED")
                if batch.status == "CANCELLED":
                    raise HTTPException(409, "USER_CANCELLED_BATCH")
                for row in rows:
                    if self.unknown(row):
                        raise HTTPException(409, "UNKNOWN_EXTERNAL_OUTCOME_REQUIRES_MANUAL_RECONCILIATION")
                    if row.status in {"PAUSED", "WAITING_MANUAL", "FAILED"}:
                        self.input(session, row)
                        row.status, row.error, row.updated_at = "QUEUED", None, m.utc_now()
                batch.status = "QUEUED"
            elif action in {"pause", "cancel"}:
                batch.status = "PAUSED" if action == "pause" else "CANCELLED"
                for row in rows:
                    if row.status in {"QUEUED", "DRAFT", "WAITING_MANUAL", "FAILED", "PAUSED"} and not self.unknown(row):
                        row.status, row.updated_at = batch.status, m.utc_now()
                        if action == "cancel":
                            self.release(session, row.id)
            else:
                raise HTTPException(422, "INVALID_PUBLISH_CONTROL")
            batch.updated_at = m.utc_now()
            result = safe(record(batch))
        if action == "resume":
            self.launch()
        return result

    @staticmethod
    def release(session, task_id, *, keep_environment=False):
        for lock in session.scalars(select(m.ResourceLock).where(m.ResourceLock.publishing_task_id == task_id, m.ResourceLock.released_at.is_(None))):
            if not keep_environment or lock.resource_key == "queue:global":
                lock.released_at = m.utc_now()

    def claim(self):
        try:
            return self._claim()
        except (_ClaimBusy, IntegrityError):
            return None
        except OperationalError as exception:
            if isinstance(exception.orig, sqlite3.OperationalError) and exception.orig.sqlite_errorcode & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                return None
            raise

    def _claim(self):
        with self.session() as session:
            if session.scalar(select(m.PublishRun.id).where(m.PublishRun.status == "RUNNING").limit(1)):
                return None
            batches = list(session.scalars(select(m.PublishingBatch).where(m.PublishingBatch.status == "QUEUED").order_by(m.PublishingBatch.created_at)))
            for batch in batches:
                rows = list(session.scalars(select(m.PublishingTask).where(m.PublishingTask.batch_id == batch.id, m.PublishingTask.status == "QUEUED").order_by(m.PublishingTask.created_at, m.PublishingTask.id)))
                for row in rows:
                    if row.schedule and aware(row.schedule) > m.utc_now():
                        row.status, row.error, row.updated_at = "WAITING_MANUAL", "SCHEDULE_REQUIRES_EXPLICIT_RESUME", m.utc_now()
                        continue
                    account = self.get(session, m.PublishingAccount, row.account_id)
                    if account.last_publish and (m.utc_now() - aware(account.last_publish)).total_seconds() < account.cooldown_seconds:
                        row.status, row.error, row.updated_at = "WAITING_MANUAL", "ACCOUNT_COOLDOWN", m.utc_now()
                        continue
                    keys = ["queue:global", "account:" + account.id, "environment:" + hashlib.sha256((account.hubstudio_env_id or account.cdp_endpoint or account.id).encode()).hexdigest()]
                    locks = [session.scalar(select(m.ResourceLock).where(m.ResourceLock.resource_key == key).with_for_update()) for key in keys]
                    if any(lock and lock.released_at is None and lock.publishing_task_id != row.id for lock in locks):
                        row.status, row.error, row.updated_at = "WAITING_MANUAL", "RESOURCE_IN_USE_REQUIRES_REVIEW", m.utc_now()
                        continue
                    session.refresh(row)
                    if row.status != "QUEUED" or session.scalar(select(m.PublishRun.id).where(m.PublishRun.status == "RUNNING").limit(1)):
                        return None
                    if session.execute(update(m.PublishingTask).where(m.PublishingTask.id == row.id, m.PublishingTask.status == "QUEUED").values(status="RUNNING", updated_at=m.utc_now())).rowcount != 1:
                        return None
                    attempt = session.scalar(select(func.count()).select_from(m.PublishRun).where(m.PublishRun.publishing_task_id == row.id)) + 1
                    run = m.PublishRun(id=new_id("prn"), publishing_task_id=row.id, batch_id=batch.id, status="RUNNING", attempt=attempt, started_at=m.utc_now(), last_heartbeat=m.utc_now(), logs=[], evidence={})
                    session.add(run)
                    session.flush()
                    for key in keys:
                        lock = session.get(m.ResourceLock, key)
                        if not lock:
                            lock = m.ResourceLock(resource_key=key, publishing_task_id=row.id, publish_run_id=run.id)
                            session.add(lock)
                            session.flush()
                        else:
                            available = m.ResourceLock.released_at.is_not(None)
                            if key != "queue:global":
                                available = or_(available, m.ResourceLock.publishing_task_id == row.id)
                            claimed = session.execute(update(m.ResourceLock).where(m.ResourceLock.resource_key == key, available).values(
                                publishing_task_id=row.id, publish_run_id=run.id, acquired_at=m.utc_now(), released_at=None))
                            if claimed.rowcount != 1:
                                raise _ClaimBusy()
                    row.status, row.updated_at, batch.active_task_id = "RUNNING", m.utc_now(), row.id
                    return row.id, run.id
                remaining = set(session.scalars(select(m.PublishingTask.status).where(m.PublishingTask.batch_id == batch.id)))
                batch.status = "COMPLETED" if remaining <= {"PUBLISHED", "published"} else "FAILED" if remaining <= {"FAILED", "CANCELLED"} else "PARTIAL" if remaining <= {"PUBLISHED", "published", "FAILED", "CANCELLED"} else "WAITING_MANUAL"
                batch.updated_at = m.utc_now()
        return None

    async def work(self):
        while not self.closing:
            claimed = self.claim()
            if not claimed:
                return
            task_id, run_id = claimed
            try:
                await self.run(task_id, run_id)
            except asyncio.CancelledError:
                self.finish(task_id, run_id, "WAITING_MANUAL", "WORKER_SHUTDOWN_CHECKPOINT")
                raise
            except (HTTPException, OSError, ValueError) as exception:
                self.finish(task_id, run_id, "FAILED", str(exception.detail) if isinstance(exception, HTTPException) else "PUBLISH_STEP_FAILED")

    def finish(self, task_id, run_id, status, error=None):
        with self.session() as session:
            row, run = self.get(session, m.PublishingTask, task_id), self.get(session, m.PublishRun, run_id)
            batch = self.get(session, m.PublishingBatch, row.batch_id)
            if self.unknown(row):
                status = "WAITING_MANUAL"
            elif batch.status in {"CANCELLED", "PAUSED"}:
                status = batch.status
            row.status, row.error, row.updated_at = status, error, m.utc_now()
            run.status, run.error_code, run.finished_at, run.last_heartbeat = status, error, m.utc_now(), m.utc_now()
            for step in session.scalars(select(m.StepExecution).where(m.StepExecution.publish_run_id == run.id, m.StepExecution.status == "RUNNING")):
                step.status, step.error_code, step.finished_at = status, error or "INTERRUPTED_WITHOUT_RESULT", m.utc_now()
            batch.active_task_id, batch.updated_at = None, m.utc_now()
            if not self.unknown(row):
                self.release(session, row.id, keep_environment=status not in {"PUBLISHED", "published"}
                    and row.checkpoint.get("UPLOAD_VIDEO", {}).get("status") == "READY" and batch.status != "CANCELLED")

    def step_start(self, task_id, run_id, name):
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            batch = self.get(session, m.PublishingBatch, row.batch_id)
            if self.closing or batch.status in {"PAUSED", "CANCELLED"}:
                return None
            payload = self.input(session, row)
            step = m.StepExecution(id=new_id("pst"), publish_run_id=run_id, publishing_task_id=row.id, name=name, status="RUNNING", started_at=m.utc_now(), checkpoint={})
            session.add(step)
            row.current_step, row.updated_at = name, m.utc_now()
            if name in {"UPLOAD_VIDEO", "CONTENT_PRECHECK", "SUBMIT"}:
                row.checkpoint = {**row.checkpoint, "production_state": {"UPLOAD_VIDEO": "UPLOADING",
                    "CONTENT_PRECHECK": "CONTENT_REVIEW_PENDING", "SUBMIT": "SUBMITTING"}[name]}
            if name not in {"PREFLIGHT", "SELECT_DESCRIPTION"}:
                row.checkpoint = {**row.checkpoint, "inflight_step": name, "external_outcome": "UNKNOWN"}
            if name == "UPLOAD_VIDEO":
                row.checkpoint = {**row.checkpoint, "upload_intent": {"video_sha256": row.input_sha256, "account_id": row.account_id,
                    "environment_id": payload["account"]["hubstudio_env_id"], "cdp_endpoint": payload["account"]["cdp_endpoint"],
                    "filename": Path(payload["video"]["path"]).name, "started_at": m.utc_now().isoformat()}}
            if name == "FILL_DESCRIPTION":
                if row.checkpoint.get("UPLOAD_VIDEO", {}).get("status") != "READY" or not self.upload_completion_valid(session, row, row.checkpoint["UPLOAD_VIDEO"]):
                    raise HTTPException(409, "VERIFIED_UPLOADED_VIDEO_REQUIRED")
                selected = row.selected_description or {}
                if not row.caption.strip() or selected.get("text") != row.caption or selected.get("sha256") != hashlib.sha256(row.caption.encode()).hexdigest():
                    raise HTTPException(409, "PERSISTED_DESCRIPTION_REQUIRED")
            if name == "ATTACH_PRODUCT":
                if row.checkpoint.get("execution_scope") == "UPLOAD_ONLY":
                    raise HTTPException(409, "UPLOAD_ONLY_PRODUCT_ATTACHMENT_FORBIDDEN")
                if row.checkpoint.get("UPLOAD_VIDEO", {}).get("status") != "READY" or not self.upload_completion_valid(session, row, row.checkpoint["UPLOAD_VIDEO"]):
                    raise HTTPException(409, "VERIFIED_UPLOADED_VIDEO_REQUIRED")
                if row.checkpoint.get("FILL_DESCRIPTION", {}).get("status") != "READY" or not self.caption_completion_valid(session, row, row.checkpoint["FILL_DESCRIPTION"]):
                    raise HTTPException(409, "VERIFIED_CAPTION_REQUIRED")
                row.checkpoint = {**row.checkpoint, "product_intent": self.product_intent(payload),
                    "product_attachment_attempt": {"run_id": run_id, "step_id": step.id, "started_at": step.started_at.isoformat()}}
            if name == "SUBMIT":
                if row.submission_state != "NOT_SUBMITTED" or not self.confirmation_valid(session, row):
                    raise HTTPException(409, "EXPLICIT_SINGLE_TASK_PUBLISH_CONFIRMATION_REQUIRED")
                confirmation = row.checkpoint["single_task_confirmation"]
                row.checkpoint = {**row.checkpoint, "submission_intent": {"id": new_id("submission"),
                    "publishing_task_id": row.id, "publish_run_id": run_id, "started_at": m.utc_now().isoformat(),
                    "fingerprint": confirmation["fingerprint"], **confirmation["snapshot"]}}
                row.submission_state = "SUBMITTING"
            if name == "VERIFY_RESULT" and (row.submission_state not in UNRESOLVED or not row.checkpoint.get("submission_intent")):
                raise HTTPException(409, "PERSISTED_SUBMISSION_INTENT_REQUIRED")
            return step.id

    def upload_completion_valid(self, session, row, result):
        proof, evidence = result.get("checkpoint", {}), result.get("evidence", {})
        account = self.get(session, m.PublishingAccount, row.account_id)
        intent, dom = row.checkpoint.get("upload_intent", {}), evidence.get("dom_evidence", {})
        if not isinstance(dom, dict):
            return False
        expected = {"video_sha256": row.input_sha256, "account_id": row.account_id, "environment_id": account.hubstudio_env_id}
        if not intent or any(proof.get(key) != value or intent.get(key) != value for key, value in expected.items()) or intent.get("cdp_endpoint") != account.cdp_endpoint:
            return False
        if not intent.get("filename") or proof.get("filename") != intent["filename"] or dom.get("filename") != intent["filename"] or dom.get("upload_complete") is not True or not dom.get("completion_text"):
            return False
        if not isinstance(dom.get("url"), str) or not isinstance(dom.get("completion_text"), str):
            return False
        url = urlsplit(dom["url"])
        if url.scheme != "https" or url.hostname != "www.tiktok.com" or url.path != "/tiktokstudio/upload" or url.username or url.password:
            return False
        try:
            timestamp = aware(datetime.fromisoformat(evidence.get("verified_at", "").replace("Z", "+00:00")))
        except (ValueError, TypeError, AttributeError):
            return False
        return evidence.get("uploaded") is True and evidence.get("source") == "OBSERVED_BROWSER" and 0 <= (m.utc_now() - timestamp).total_seconds() <= self.config.verification_max_age_seconds

    def product_confirmation_valid(self, session, row, result):
        if row.checkpoint.get("execution_scope") == "UPLOAD_ONLY":
            return False
        payload = self.input(session, row)
        intent = self.product_intent(payload)
        uploaded = row.checkpoint.get("UPLOAD_VIDEO", {})
        if uploaded.get("status") != "READY" or not self.upload_completion_valid(session, row, uploaded):
            return False
        proof, evidence = result.get("checkpoint", {}), result.get("evidence", {})
        if row.checkpoint.get("product_intent") != intent or evidence.get("source") != "OBSERVED_BROWSER" or not self.fresh(evidence):
            return False
        if any(proof.get(key) != intent[key] for key in ("product_id", "account_id", "environment_id", "video_sha256", "mode")):
            return False
        if any(evidence.get(key) != intent[key] for key in ("account_id", "environment_id", "username", "video_sha256")):
            return False
        if evidence.get("read_only") is True:
            if row.checkpoint.get("product_reconciliation_attempt"):
                payload["checkpoint"]["product_reconciliation_attempt"] = row.checkpoint["product_reconciliation_attempt"]
            else:
                old_step = session.scalar(select(m.StepExecution).where(m.StepExecution.publishing_task_id == row.id,
                    m.StepExecution.name == "ATTACH_PRODUCT").order_by(m.StepExecution.started_at.desc()).limit(1))
                if old_step:
                    payload["checkpoint"]["product_reconciliation_attempt"] = {"run_id": old_step.publish_run_id,
                        "step_id": old_step.id, "started_at": aware(old_step.started_at).isoformat()}
            receipt = product_receipt(self.config, payload, evidence.get("product_receipt")) if isinstance(evidence.get("product_receipt"), dict) else None
            dom = evidence.get("dom_evidence", {})
            if (evidence.get("reconciliation") not in {"BOUND_RECEIPT_AND_CURRENT_CHIP", "PLATFORM_ANCHOR_AND_CURRENT_CATALOG"} or not receipt
                    or not self.draft_evidence_valid(payload, evidence) or result.get("performed") is not False
                    or receipt["receipt"].get("selected_name") != evidence.get("selected_name")
                    or receipt["receipt"].get("selected_id") != evidence.get("selected_id")
                    or receipt["receipt"].get("display_name") != dom.get("confirmation_text")
                    or dom.get("caption") != row.caption or dom.get("upload_complete") is not True
                    or evidence.get("caption_sha256") != hashlib.sha256(row.caption.encode()).hexdigest()):
                return False
            if receipt["receipt"]["source"] == "OBSERVED_ATTACHED_PRODUCT_READBACK":
                if evidence.get("reconciliation") != "PLATFORM_ANCHOR_AND_CURRENT_CATALOG" or not self.product_observation_valid(payload, payload["checkpoint"].get("product_observation", {})):
                    return False
            elif evidence.get("reconciliation") != "BOUND_RECEIPT_AND_CURRENT_CHIP":
                return False
        elif evidence.get("read_only") is not False:
            return False
        if evidence.get("attached") is not True or evidence.get("confirmed") is not True or evidence.get("publish_clicked") is not False:
            return False
        selected_name, selected_id = evidence.get("selected_name"), evidence.get("selected_id")
        if not isinstance(selected_name, str) or not selected_name.strip() or not isinstance(selected_id, str) or not selected_id.strip():
            return False
        if proof.get("selected_name") != selected_name or proof.get("selected_id") != selected_id:
            return False
        if not self.product_name_matches(selected_name, intent["expected_names"]) or (intent["mode"] == "exact_id" and selected_id != intent["requested_id"]):
            return False
        dom = evidence.get("dom_evidence", {})
        if not isinstance(dom, dict) or dom.get("selected_name") != selected_name or dom.get("selected_id") != selected_id or dom.get("attached") is not True:
            return False
        if not isinstance(dom.get("confirmation_text"), str) or not dom["confirmation_text"].strip() or not isinstance(dom.get("url"), str):
            return False
        try:
            url = urlsplit(dom["url"])
        except ValueError:
            return False
        return url.scheme == "https" and url.hostname == "www.tiktok.com" and url.path == "/tiktokstudio/upload" and not url.username and not url.password

    def draft_evidence_valid(self, payload, evidence):
        if evidence.get("source") != "OBSERVED_BROWSER" or not self.fresh(evidence) or evidence.get("publish_clicked") is not False:
            return False
        account = payload["account"]
        expected = {"account_id": account["id"], "environment_id": account["hubstudio_env_id"],
            "username": account["expected_username"], "video_sha256": payload["video"]["sha256"]}
        if any(evidence.get(key) != value or not value for key, value in expected.items()):
            return False
        try:
            url = urlsplit(evidence.get("url", ""))
            screenshot = Path(evidence.get("screenshot", "")).resolve()
            screenshot_ok = screenshot.is_relative_to(self.config.output_root) and screenshot.is_file() and screenshot.stat().st_size > 0
        except (ValueError, TypeError, OSError):
            return False
        return url.scheme == "https" and url.hostname == "www.tiktok.com" and url.path == "/tiktokstudio/upload" and not url.username and not url.password and screenshot_ok

    def caption_completion_valid(self, session, row, result):
        payload = self.input(session, row)
        proof, evidence = result.get("checkpoint", {}), result.get("evidence", {})
        selected = row.selected_description or {}
        digest = hashlib.sha256(row.caption.encode()).hexdigest()
        expected = {"account_id": row.account_id, "environment_id": payload["account"]["hubstudio_env_id"],
            "video_sha256": row.input_sha256, "caption_sha256": digest, "description_id": selected.get("id")}
        dom = evidence.get("dom_evidence", {})
        return (bool(row.caption.strip()) and selected.get("text") == row.caption and selected.get("sha256") == digest
            and all(proof.get(key) == value and value for key, value in expected.items())
            and self.draft_evidence_valid(payload, evidence) and evidence.get("caption_sha256") == digest
            and isinstance(dom, dict) and dom.get("caption") == row.caption and dom.get("url") == evidence.get("url"))

    def content_review_valid(self, session, row, result):
        payload = self.input(session, row)
        filled = row.checkpoint.get("FILL_DESCRIPTION", {})
        attached = row.checkpoint.get("ATTACH_PRODUCT", {})
        evidence = result.get("evidence", {})
        dom = evidence.get("dom_evidence", {})
        return (filled.get("status") == "READY" and self.caption_completion_valid(session, row, filled)
            and attached.get("status") == "READY" and self.product_confirmation_valid(session, row, attached)
            and self.draft_evidence_valid(payload, evidence) and evidence.get("read_only") is True
            and evidence.get("content_review") == "PASSED" and evidence.get("caption_sha256") == hashlib.sha256(row.caption.encode()).hexdigest()
            and evidence.get("selected_id") == attached.get("checkpoint", {}).get("selected_id")
            and isinstance(dom, dict) and dom.get("panel") == "Revisión del contenido simplificada"
            and dom.get("enabled") is True and dom.get("visible") is True and dom.get("data_show") == "true"
            and isinstance(dom.get("classes"), list) and "status-success" in dom["classes"]
            and isinstance(dom.get("text"), str) and dom["text"].startswith("No se han detectado problemas.")
            and dom.get("url") == evidence.get("url"))

    def submitted_identity_valid(self, session, row, evidence):
        payload = self.input(session, row, reconcile_submitted=True)
        intent = row.checkpoint.get("submission_intent", {})
        account = payload["account"]
        expected = {"publishing_task_id": row.id, "account_id": row.account_id, "environment_id": account["hubstudio_env_id"],
            "username": account["expected_username"], "video_sha256": row.input_sha256,
            "description_sha256": hashlib.sha256(row.caption.encode()).hexdigest()}
        if not intent or any(intent.get(key) != value or evidence.get(key) != value or not value for key, value in expected.items()):
            return False
        if evidence.get("submission_intent_id") != intent["id"] or evidence.get("source") != "OBSERVED_BROWSER" or not self.fresh(evidence):
            return False
        try:
            verified = aware(datetime.fromisoformat(evidence["verified_at"].replace("Z", "+00:00")))
            started = aware(datetime.fromisoformat(intent["started_at"].replace("Z", "+00:00")))
            screenshot = Path(evidence.get("screenshot", "")).resolve()
            return (verified >= started and screenshot.is_relative_to(self.config.output_root)
                and screenshot.is_file() and screenshot.stat().st_size > 0)
        except (ValueError, TypeError, KeyError, OSError):
            return False

    def submission_click_valid(self, session, row, result):
        evidence = result.get("evidence", {})
        intent = row.checkpoint.get("submission_intent", {})
        dom = evidence.get("dom_evidence", {})
        if not isinstance(dom, dict) or not self.submitted_identity_valid(session, row, evidence):
            return False
        try:
            url = urlsplit(dom.get("before_url", ""))
        except (ValueError, TypeError):
            return False
        return (result.get("performed") is True and evidence.get("publish_clicked") is True and evidence.get("read_only") is False
            and evidence.get("selected_id") == intent.get("selected_product_id")
            and dom.get("caption") == row.caption and dom.get("selected_id") == intent.get("selected_product_id")
            and dom.get("publish_control_observed") is True and dom.get("visible") is True and dom.get("enabled") is True
            and url.scheme == "https" and url.hostname == "www.tiktok.com" and url.path == "/tiktokstudio/upload"
            and not url.username and not url.password)

    def publication_receipt_valid(self, session, row, result):
        evidence = result.get("evidence", {})
        dom = evidence.get("dom_evidence", {})
        intent = row.checkpoint.get("submission_intent", {})
        if not isinstance(dom, dict) or not self.submitted_identity_valid(session, row, evidence):
            return False
        if evidence.get("studio_receipt"):
            payload = self.input(session, row, reconcile_submitted=True)
            receipt = studio_readback(self.config, payload, evidence["studio_receipt"])
            if not receipt or receipt["receipt"]["item"]["item_id"] != result.get("platform_video_id") or receipt["receipt"]["item"]["href"] != result.get("platform_url"):
                return False
            item = receipt["receipt"]["item"]
            if item.get("in_review") is not False or item.get("status") != 102 or item.get("visibility") != 1 or "Todo el mundo" not in item.get("row_text", ""):
                return False
            if (not ExistingHubBrowserAdapter.public_post_matches(evidence.get("public_metadata"),
                    {"video_id": item["item_id"]}, payload) or int(evidence["public_metadata"]["create_time"]) != int(item["create_time"])):
                return False
            playback = evidence.get("playback", {})
            if (playback.get("visible") is not True or playback.get("ready_state", 0) < 2 or playback.get("error") is not None
                    or playback.get("width", 0) <= 0 or playback.get("height", 0) <= 0
                    or not isinstance(playback.get("duration"), (int, float)) or abs(playback["duration"] - payload["video"]["duration"]) > 1):
                return False
        platform_id, platform_url = result.get("platform_video_id"), result.get("platform_url")
        if not isinstance(platform_id, str) or not platform_id.isdigit() or not isinstance(platform_url, str):
            return False
        try:
            url = urlsplit(platform_url)
        except ValueError:
            return False
        return (evidence.get("read_only") is True and result.get("performed") is False and evidence.get("published") is True
            and url.scheme == "https" and url.hostname == "www.tiktok.com" and not url.username and not url.password
            and url.path == f"/@{intent['username']}/video/{platform_id}" and not url.query and not url.fragment
            and dom.get("video_url") == platform_url and dom.get("video_id") == platform_id and dom.get("published") is True
            and dom.get("username") == intent["username"] and dom.get("caption") == row.caption
            and dom.get("selected_product_id") == intent.get("selected_product_id"))

    def step_finish(self, task_id, run_id, step_id, result):
        result = self.result(result)
        status = result.get("status", "WAITING_MANUAL")
        if status not in {"READY", "WAITING_MANUAL", "FAILED"}:
            status = "WAITING_MANUAL"
        with self.session() as session:
            row, run, step = self.get(session, m.PublishingTask, task_id), self.get(session, m.PublishRun, run_id), self.get(session, m.StepExecution, step_id)
            if step.name == "UPLOAD_VIDEO" and status == "READY":
                if not self.upload_completion_valid(session, row, result):
                    status = "WAITING_MANUAL"
                    result = {**result, "status": status, "error_code": "UPLOAD_COMPLETION_EVIDENCE_REQUIRED"}
            if step.name == "FILL_DESCRIPTION" and status == "READY" and not self.caption_completion_valid(session, row, result):
                status = "WAITING_MANUAL"
                result = {**result, "status": status, "error_code": "CAPTION_READBACK_EVIDENCE_REQUIRED"}
            if step.name == "ATTACH_PRODUCT" and status == "READY":
                if not self.product_confirmation_valid(session, row, result):
                    status = "WAITING_MANUAL"
                    result = {**result, "status": status, "error_code": "PRODUCT_CONFIRMATION_EVIDENCE_REQUIRED"}
                else:
                    row.product_link_status = "PRODUCT_CONFIRMED"
            if step.name == "CONTENT_PRECHECK":
                if status == "READY" and not self.content_review_valid(session, row, result):
                    status = "WAITING_MANUAL"
                    result = {**result, "status": status, "error_code": "CONTENT_REVIEW_EVIDENCE_REQUIRED"}
                observed = result.get("evidence", {}).get("content_review")
                row.moderation_status = "PASSED" if status == "READY" else observed if observed in {"CHECKING", "ERROR", "RESTRICTED"} else "UNKNOWN"
            if step.name == "SUBMIT":
                if result.get("performed") is False:
                    row.submission_state = "NOT_SUBMITTED"
                    if status == "READY":
                        status = "WAITING_MANUAL"
                        result = {**result, "status": status, "error_code": "PUBLIC_SUBMISSION_NOT_PERFORMED"}
                elif status == "READY" and self.submission_click_valid(session, row, result):
                    row.submission_state = "SUBMITTED"
                else:
                    row.submission_state = "UNKNOWN"
                    status = "WAITING_MANUAL"
                    result = {**result, "status": status, "error_code": result.get("error_code") or "SUBMISSION_OUTCOME_UNKNOWN"}
            if step.name == "VERIFY_RESULT":
                if status == "READY" and self.publication_receipt_valid(session, row, result):
                    row.submission_state = "VERIFIED"
                    row.platform_video_id, row.platform_url = result["platform_video_id"], result["platform_url"]
                    self.get(session, m.PublishingAccount, row.account_id).last_publish = m.utc_now()
                else:
                    status = "WAITING_MANUAL"
                    result = {**result, "status": status, "error_code": result.get("error_code") or "PUBLICATION_RECEIPT_NOT_VERIFIED"}
            step.status, step.finished_at, step.checkpoint, step.error_code = status, m.utc_now(), result, result.get("error_code")
            if step.name == "PREFLIGHT" and status == "READY":
                account = self.get(session, m.PublishingAccount, row.account_id)
                account.identity_verification, account.vpn_verification = result["evidence"]["identity"], result["evidence"]["vpn"]
            run.logs = [*run.logs, {"step": step.name, "status": status, "at": m.utc_now().isoformat()}]
            run.evidence = {**run.evidence, step.name: result.get("evidence", {})}
            run.last_heartbeat = m.utc_now()
            checkpoint = dict(row.checkpoint)
            checkpoint[step.name] = result
            checkpoint["production_state"] = FLOW_STATES[step.name] if status == "READY" else (
                "UNKNOWN_EXTERNAL_RESULT" if self.unknown(row) and result.get("performed") is not False else
                "FAILED" if status == "FAILED" else "WAITING_MANUAL")
            if step.name not in {"PREFLIGHT", "SELECT_DESCRIPTION"} and (status == "READY" or result.get("performed") is False):
                checkpoint.pop("inflight_step", None)
                checkpoint.pop("external_outcome", None)
            row.checkpoint, row.updated_at = checkpoint, m.utc_now()
        return status, result.get("error_code")

    async def reconcile_submit(self, task_id):
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            if row.submission_state == "VERIFIED":
                return {"status": "PUBLISHED", "task": self.task(task_id), "read_only": True, "reused": True}
            if row.submission_state not in UNRESOLVED or not row.checkpoint.get("submission_intent"):
                raise HTTPException(409, "PERSISTED_SUBMISSION_INTENT_REQUIRED")
            if session.scalar(select(m.PublishRun.id).where(m.PublishRun.publishing_task_id == row.id, m.PublishRun.status == "RUNNING").limit(1)):
                raise HTTPException(409, "PUBLISH_RUN_STILL_ACTIVE")
            payload = self.input(session, row, reconcile_submitted=True)
            intent = dict(row.checkpoint["submission_intent"])
        try:
            observed = self.result(await self.adapter.execute_step("VERIFY_RESULT", payload))
        except asyncio.CancelledError:
            raise
        except Exception:
            observed = {"status": "WAITING_MANUAL", "performed": False, "error_code": "PUBLICATION_RECONCILIATION_FAILED", "evidence": {"read_only": True}}
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            if row.checkpoint.get("submission_intent") != intent:
                raise HTTPException(409, "SUBMISSION_INTENT_CHANGED")
            run = self.get(session, m.PublishRun, intent["publish_run_id"])
            verified = observed["status"] == "READY" and self.publication_receipt_valid(session, row, observed)
            status = "READY" if verified else "WAITING_MANUAL"
            error = None if verified else observed.get("error_code") or "PUBLICATION_RECEIPT_NOT_VERIFIED"
            session.add(m.StepExecution(id=new_id("pst"), publish_run_id=run.id, publishing_task_id=row.id,
                name="RECONCILE_SUBMIT", status=status, started_at=m.utc_now(), finished_at=m.utc_now(),
                checkpoint=observed, error_code=error))
            run.logs = [*run.logs, {"step": "RECONCILE_SUBMIT", "status": status, "at": m.utc_now().isoformat(), "read_only": True}]
            run.evidence = {**run.evidence, "RECONCILE_SUBMIT": observed.get("evidence", {})}
            row.error, row.updated_at = error, m.utc_now()
            observation = observed.get("checkpoint", {}).get("publication_observation")
            if observation and observation.get("source") == "OBSERVED_STUDIO_POST_READBACK":
                receipt = studio_readback(self.config, self.input(session, row, reconcile_submitted=True), observation.get("studio_receipt", {}))
                if receipt:
                    row.checkpoint = {**row.checkpoint, "publication_observation": observation}
            if verified:
                row.platform_video_id, row.platform_url = observed["platform_video_id"], observed["platform_url"]
                row.submission_state, row.status, row.current_step = "VERIFIED", "PUBLISHED", "VERIFY_RESULT"
                checkpoint = {**row.checkpoint, "VERIFY_RESULT": observed, "production_state": "PUBLISHED_VERIFIED"}
                checkpoint.pop("inflight_step", None)
                checkpoint.pop("external_outcome", None)
                row.checkpoint = checkpoint
                run.status, run.error_code, run.last_heartbeat = "PUBLISHED", None, m.utc_now()
                self.get(session, m.PublishingAccount, row.account_id).last_publish = m.utc_now()
                self.release(session, task_id)
                session.flush()
                batch = self.get(session, m.PublishingBatch, row.batch_id)
                states = set(session.scalars(select(m.PublishingTask.status).where(m.PublishingTask.batch_id == batch.id)))
                if batch.status not in {"PAUSED", "CANCELLED"} and states <= {"PUBLISHED", "published"}:
                    batch.status, batch.updated_at = "COMPLETED", m.utc_now()
            else:
                row.status = "WAITING_MANUAL"
        return {"status": "PUBLISHED" if verified else "WAITING_MANUAL", "task": self.task(task_id), "observed": observed,
            "read_only": True, "public_submission": "NOT_REPEATED"}

    async def reconcile_upload(self, task_id):
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            if row.submission_state != "NOT_SUBMITTED" or row.checkpoint.get("inflight_step") != "UPLOAD_VIDEO" or not self.unknown(row):
                raise HTTPException(409, "UNKNOWN_UPLOAD_CHECKPOINT_REQUIRED")
            if row.status in {"QUEUED", "RUNNING"} or session.scalar(select(m.PublishRun.id).where(m.PublishRun.publishing_task_id == task_id, m.PublishRun.status == "RUNNING").limit(1)):
                raise HTTPException(409, "PUBLISH_RUN_STILL_ACTIVE")
            payload = self.input(session, row)
            intent = dict(row.checkpoint.get("upload_intent", {}))
            if not intent or intent.get("video_sha256") != payload["video"]["sha256"] or intent.get("account_id") != row.account_id or intent.get("environment_id") != payload["account"]["hubstudio_env_id"] or intent.get("cdp_endpoint") != payload["account"]["cdp_endpoint"]:
                raise HTTPException(409, "UPLOAD_INTENT_IDENTITY_CHANGED")
            old_step = session.scalar(select(m.StepExecution).where(m.StepExecution.publishing_task_id == task_id, m.StepExecution.name == "UPLOAD_VIDEO").order_by(m.StepExecution.started_at.desc()).limit(1))
            if old_step is None:
                raise HTTPException(409, "UPLOAD_EXECUTION_RECORD_REQUIRED")
            run_id = old_step.publish_run_id
        observed = await self.observe(payload)
        if observed["status"] == "READY":
            try:
                observed = self.result(await self.adapter.reconcile_upload(payload))
            except asyncio.CancelledError:
                raise
            except Exception:
                observed = {"status": "WAITING_MANUAL", "error_code": "RECONCILIATION_OBSERVATION_FAILED", "evidence": {}}
        with self.session() as session:
            row, run = self.get(session, m.PublishingTask, task_id), self.get(session, m.PublishRun, run_id)
            self.input(session, row)
            if row.checkpoint.get("upload_intent") != intent or not self.unknown(row) or row.status in {"QUEUED", "RUNNING"}:
                raise HTTPException(409, "UPLOAD_CHECKPOINT_CHANGED_DURING_OBSERVATION")
            if observed.get("status") == "READY" and (observed.get("evidence", {}).get("read_only") is not True or not self.upload_completion_valid(session, row, observed)):
                observed = {**observed, "status": "WAITING_MANUAL", "error_code": "RECONCILIATION_COMPLETION_EVIDENCE_REQUIRED"}
            status = observed.get("status", "WAITING_MANUAL")
            session.add(m.StepExecution(id=new_id("pst"), publish_run_id=run.id, publishing_task_id=row.id, name="RECONCILE_UPLOAD", status=status,
                started_at=m.utc_now(), finished_at=m.utc_now(), checkpoint=observed, error_code=observed.get("error_code")))
            run.logs = [*run.logs, {"step": "RECONCILE_UPLOAD", "status": status, "at": m.utc_now().isoformat(), "read_only": True}]
            run.evidence = {**run.evidence, "RECONCILE_UPLOAD": observed.get("evidence", {})}
            if status == "READY":
                checkpoint = {**row.checkpoint, "UPLOAD_VIDEO": observed, "upload_reconciled_at": m.utc_now().isoformat()}
                checkpoint.pop("external_outcome", None)
                checkpoint.pop("inflight_step", None)
                row.checkpoint = checkpoint
                batch = self.get(session, m.PublishingBatch, row.batch_id)
                row.status = "CANCELLED" if batch.status == "CANCELLED" else "PAUSED" if batch.status == "PAUSED" else "WAITING_MANUAL"
                row.error = "UPLOAD_VERIFIED_STOP_BEFORE_PRODUCT_AND_PUBLISH" if self.config.stop_after_upload or row.checkpoint.get("execution_scope") == "UPLOAD_ONLY" else "RECONCILED_UPLOAD_REQUIRES_EXPLICIT_RESUME"
                self.release(session, row.id, keep_environment=batch.status != "CANCELLED")
            else:
                row.error = observed.get("error_code", "UPLOAD_RECONCILIATION_REQUIRES_MANUAL")
            row.updated_at = m.utc_now()
        return {"status": status, "observed": observed, "task": self.task(task_id), "public_submission": "NOT_PERFORMED"}

    def product_reconciliation_input(self, session, row):
        if row.submission_state != "NOT_SUBMITTED" or row.checkpoint.get("inflight_step") != "ATTACH_PRODUCT" or not self.unknown(row):
            raise HTTPException(409, "UNKNOWN_PRODUCT_CHECKPOINT_REQUIRED")
        if row.status in {"QUEUED", "RUNNING"} or session.scalar(select(m.PublishRun.id).where(m.PublishRun.publishing_task_id == row.id, m.PublishRun.status == "RUNNING").limit(1)):
            raise HTTPException(409, "PUBLISH_RUN_STILL_ACTIVE")
        payload = self.input(session, row)
        if row.checkpoint.get("product_intent") != self.product_intent(payload):
            raise HTTPException(409, "PRODUCT_INTENT_IDENTITY_CHANGED")
        if not self.upload_completion_valid(session, row, row.checkpoint.get("UPLOAD_VIDEO", {})) or not self.caption_completion_valid(session, row, row.checkpoint.get("FILL_DESCRIPTION", {})):
            raise HTTPException(409, "VERIFIED_UPLOADED_VIDEO_AND_CAPTION_REQUIRED")
        old_step = session.scalar(select(m.StepExecution).where(m.StepExecution.publishing_task_id == row.id,
            m.StepExecution.name == "ATTACH_PRODUCT").order_by(m.StepExecution.started_at.desc()).limit(1))
        if old_step is None:
            raise HTTPException(409, "PRODUCT_EXECUTION_RECORD_REQUIRED")
        payload["checkpoint"]["product_reconciliation_attempt"] = {"run_id": old_step.publish_run_id,
            "step_id": old_step.id, "started_at": aware(old_step.started_at).isoformat()}
        return payload

    async def reconcile_product(self, task_id):
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            payload = self.product_reconciliation_input(session, row)
            original = {"payload": payload, "status": row.status, "batch_status": self.get(session, m.PublishingBatch, row.batch_id).status}
            old_step = session.scalar(select(m.StepExecution).where(m.StepExecution.publishing_task_id == task_id,
                m.StepExecution.name == "ATTACH_PRODUCT").order_by(m.StepExecution.started_at.desc()).limit(1))
            if old_step is None:
                raise HTTPException(409, "PRODUCT_EXECUTION_RECORD_REQUIRED")
            run_id = old_step.publish_run_id
        observed = await self.observe(payload)
        if observed["status"] == "READY":
            try:
                observed = self.result(await self.adapter.reconcile_product(payload))
            except asyncio.CancelledError:
                raise
            except Exception:
                observed = {"status": "WAITING_MANUAL", "performed": False, "error_code": "PRODUCT_RECONCILIATION_OBSERVATION_FAILED", "evidence": {"read_only": True}}
        with self.session() as session:
            row, run = self.get(session, m.PublishingTask, task_id), self.get(session, m.PublishRun, run_id)
            current = self.product_reconciliation_input(session, row)
            batch = self.get(session, m.PublishingBatch, row.batch_id)
            if {"payload": current, "status": row.status, "batch_status": batch.status} != original:
                raise HTTPException(409, "PRODUCT_CHECKPOINT_CHANGED_DURING_OBSERVATION")
            if observed.get("status") == "READY" and (observed.get("evidence", {}).get("read_only") is not True or not self.product_confirmation_valid(session, row, observed)):
                observed = {**observed, "status": "WAITING_MANUAL", "error_code": "PRODUCT_RECONCILIATION_EVIDENCE_REQUIRED"}
            status = observed.get("status", "WAITING_MANUAL")
            session.add(m.StepExecution(id=new_id("pst"), publish_run_id=run.id, publishing_task_id=row.id,
                name="RECONCILE_PRODUCT", status=status, started_at=m.utc_now(), finished_at=m.utc_now(), checkpoint=observed, error_code=observed.get("error_code")))
            run.logs = [*run.logs, {"step": "RECONCILE_PRODUCT", "status": status, "at": m.utc_now().isoformat(), "read_only": True}]
            run.evidence = {**run.evidence, "RECONCILE_PRODUCT": observed.get("evidence", {})}
            if status == "READY":
                checkpoint = {**row.checkpoint, "ATTACH_PRODUCT": observed, "product_reconciled_at": m.utc_now().isoformat(),
                    "product_reconciliation_attempt": current["checkpoint"]["product_reconciliation_attempt"]}
                checkpoint.pop("external_outcome", None)
                checkpoint.pop("inflight_step", None)
                row.checkpoint, row.product_link_status = checkpoint, "PRODUCT_CONFIRMED"
                row.status = "CANCELLED" if batch.status == "CANCELLED" else "PAUSED" if batch.status == "PAUSED" else "WAITING_MANUAL"
                row.error = "RECONCILED_PRODUCT_REQUIRES_EXPLICIT_RESUME"
                self.release(session, row.id, keep_environment=batch.status != "CANCELLED")
            else:
                row.error = observed.get("error_code", "PRODUCT_RECONCILIATION_REQUIRES_MANUAL")
            row.updated_at = m.utc_now()
            session.add(m.AuditLog(id=new_id("audit"), task_id=row.task_id, action="publishing.product_reconciled", entity_id=row.id,
                message=f"{status}; read-only attached-product reconciliation; no product reattachment or publishing", created_at=m.utc_now()))
        return {"status": status, "observed": observed, "task": self.task(task_id), "read_only": True, "public_submission": "NOT_PERFORMED"}

    def select_description(self, task_id):
        with self.session() as session:
            row = self.get(session, m.PublishingTask, task_id)
            product = self.get(session, m.Product, row.product_id)
            if row.selected_description and row.selected_description.get("source") != "USER_PROVIDED" and not description_review_ready(record(product)):
                return {"status": "WAITING_MANUAL", "error_code": "DESCRIPTION_CONTENT_REVIEW_REQUIRED", "evidence": {"performed": False}}
            if row.selected_description is None:
                if not row.caption.strip() and not description_review_ready(record(product)):
                    return {"status": "WAITING_MANUAL", "error_code": "DESCRIPTION_CONTENT_REVIEW_REQUIRED", "evidence": {"performed": False}}
                recent = [{"description_id": item.description_id, "description_sha256": item.description_sha256} for item in session.scalars(select(m.DescriptionUsage).where(m.DescriptionUsage.product_id == product.id, m.DescriptionUsage.account_id == row.account_id).order_by(m.DescriptionUsage.selected_at.desc(), m.DescriptionUsage.id.desc()).limit(100))]
                if row.caption.strip():
                    selected = {"id": "manual-caption", "text": row.caption, "sha256": hashlib.sha256(row.caption.encode()).hexdigest(), "source": "USER_PROVIDED"}
                else:
                    try:
                        selected = self.selector.select(product.descriptions, product.description_selection, recent)
                    except ValueError:
                        return {"status": "WAITING_MANUAL", "error_code": "DESCRIPTION_POOL_EXHAUSTED", "evidence": {"performed": False}}
                row.selected_description, row.caption = selected, row.caption or selected["text"]
                session.add(m.DescriptionUsage(id=new_id("dsc"), product_id=product.id, account_id=row.account_id, publishing_task_id=row.id, description_id=selected["id"], description_sha256=selected["sha256"]))
            return {"status": "READY", "checkpoint": row.selected_description, "evidence": {"persisted": True, "description_id": row.selected_description["id"]}}

    async def run(self, task_id, run_id):
        for name in STEPS:
            with self.session() as session:
                row = self.get(session, m.PublishingTask, task_id)
                upload_only = row.checkpoint.get("execution_scope") == "UPLOAD_ONLY"
                if upload_only and name == "SELECT_DESCRIPTION":
                    continue
                if (self.config.stop_after_upload or upload_only) and name in STEPS[3:] and row.checkpoint.get("UPLOAD_VIDEO", {}).get("status") == "READY":
                    stop = True
                else:
                    stop = False
                if name != "PREFLIGHT" and row.checkpoint.get(name, {}).get("status") == "READY":
                    continue
                payload = self.input(session, row)
            if stop:
                self.finish(task_id, run_id, "WAITING_MANUAL", "UPLOAD_VERIFIED_STOP_BEFORE_PRODUCT_AND_PUBLISH")
                return
            step_id = self.step_start(task_id, run_id, name)
            if not step_id:
                self.finish(task_id, run_id, "PAUSED", "CONTROL_CHECKPOINT")
                return
            if name == "PREFLIGHT":
                result = await self.observe(payload)
                result["performed"] = False
            elif name == "SELECT_DESCRIPTION":
                result = self.select_description(task_id)
            elif name == "FINAL_CONFIRMATION":
                with self.session() as session:
                    current = self.get(session, m.PublishingTask, task_id)
                    try:
                        confirmed = self.confirmation_valid(session, current)
                        error = "EXPLICIT_SINGLE_TASK_PUBLISH_CONFIRMATION_REQUIRED"
                    except HTTPException as failure:
                        confirmed, error = False, str(failure.detail)
                    result = {"status": "READY" if confirmed else "WAITING_MANUAL", "performed": False,
                        "evidence": {"source": "USER_SINGLE_TASK_CONFIRMATION", "fingerprint": current.checkpoint.get("single_task_confirmation", {}).get("fingerprint")},
                        **({} if confirmed else {"error_code": error})}
            else:
                try:
                    with self.session() as session:
                        payload = self.input(session, self.get(session, m.PublishingTask, task_id))
                    result = await self.adapter.execute_step(name, payload)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    result = {"status": "WAITING_MANUAL", "error_code": "EXTERNAL_STEP_OUTCOME_UNKNOWN", "evidence": {"step": name}}
            result = self.result(result)
            status, step_error = self.step_finish(task_id, run_id, step_id, result)
            if status != "READY":
                self.finish(task_id, run_id, status, step_error or "MANUAL_VERIFICATION_REQUIRED")
                return
            if name == "UPLOAD_VIDEO" and (self.config.stop_after_upload or upload_only):
                self.finish(task_id, run_id, "WAITING_MANUAL", "UPLOAD_VERIFIED_STOP_BEFORE_PRODUCT_AND_PUBLISH")
                return
        with self.session() as session:
            verified = self.get(session, m.PublishingTask, task_id).submission_state == "VERIFIED"
        self.finish(task_id, run_id, "PUBLISHED" if verified else "WAITING_MANUAL", None if verified else "PUBLIC_RESULT_NOT_VERIFIED")

    async def recover(self):
        with self.session() as session:
            for run in session.scalars(select(m.PublishRun).where(m.PublishRun.status == "RUNNING")):
                row = self.get(session, m.PublishingTask, run.publishing_task_id)
                batch = self.get(session, m.PublishingBatch, run.batch_id)
                row.status, row.error = "WAITING_MANUAL", "RESTART_REQUIRES_CHECKPOINT_REVIEW"
                run.status, run.error_code, run.finished_at = "WAITING_MANUAL", row.error, m.utc_now()
                for step in session.scalars(select(m.StepExecution).where(m.StepExecution.publish_run_id == run.id, m.StepExecution.status == "RUNNING")):
                    step.status, step.error_code, step.finished_at = "WAITING_MANUAL", row.error, m.utc_now()
                if batch.status != "CANCELLED":
                    batch.status = "PAUSED"
                batch.active_task_id = None
                if not self.unknown(row):
                    self.release(session, row.id, keep_environment=row.checkpoint.get("UPLOAD_VIDEO", {}).get("status") == "READY" and batch.status != "CANCELLED")
            for batch in session.scalars(select(m.PublishingBatch).where(m.PublishingBatch.status == "QUEUED")):
                batch.status = "PAUSED"
                for row in session.scalars(select(m.PublishingTask).where(m.PublishingTask.batch_id == batch.id, m.PublishingTask.status == "QUEUED")):
                    row.status = "PAUSED"
        return self.queue()

    async def shutdown(self, *, recover=True):
        self.closing = True
        if self.worker and not self.worker.done():
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        if recover:
            await self.recover()
