import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable, Literal

from sqlalchemy import DateTime, create_engine, delete, event, inspect, or_, select
from sqlalchemy.orm import Session

from app import models as m
from app.schemas import (
    AccountView, AssetView, AuditView, CollectorView, CollectionIdentityView, ErrorView, FactoryState,
    LegacyBindingView, ProductView, ProjectView, PromptView, PublishingView, ReviewView, RunView, ProductionBatchView, QualityAssessmentView,
    SettingsView, StageView, TaskView, VideoMetadata, WorkflowView,
)

COLLECTIONS = {
    "projects": (m.Project, ProjectView), "tasks": (m.Task, TaskView),
    "stages": (m.TaskStage, StageView), "products": (m.Product, ProductView),
    "assets": (m.VideoAsset, AssetView), "collector": (m.CollectorRule, CollectorView),
    "accounts": (m.PublishingAccount, AccountView), "publishing": (m.PublishingTask, PublishingView),
    "prompts": (m.Prompt, PromptView), "reviews": (m.Review, ReviewView),
    "runs": (m.ProcessingRun, RunView), "errors": (m.ErrorLog, ErrorView),
    "audits": (m.AuditLog, AuditView),
    "legacy_bindings": (m.LegacyBinding, LegacyBindingView),
    "batches": (m.ProductionBatch, ProductionBatchView),
    "quality_assessments": (m.QualityAssessment, QualityAssessmentView),
    "collection_identities": (m.CollectionIdentity, CollectionIdentityView),
}


def context_id(prefix: str, value: str) -> str:
    return prefix + "-" + hashlib.sha256(value.encode()).hexdigest()[:20]


def iso(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    return value


def record(row) -> dict:
    return {column.name: iso(getattr(row, column.name)) for column in row.__table__.columns}


class StateRepository:
    def __init__(self, database_url: str, seed_path: Path, *, app_mode: Literal["mock", "integration"] = "mock"):
        self.seed_path = seed_path
        self.app_mode = app_mode
        seed = self.seed()
        self.mock_product_ids = {row["id"] for row in seed["products"]}
        self.mock_account_ids = {row["id"] for row in seed["accounts"]}
        self.lock = RLock()
        self.engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite:") else {})

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _):
            if self.engine.dialect.name == "sqlite":
                connection.execute("PRAGMA foreign_keys=ON")

        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names()) - {"alembic_version"}
        if not existing_tables:
            m.Base.metadata.create_all(self.engine)
        else:
            missing = []
            for table in m.Base.metadata.tables.values():
                columns = {item["name"] for item in inspector.get_columns(table.name)} if table.name in existing_tables else set()
                missing.extend(table.name + "." + column.name for column in table.columns if column.name not in columns)
            if missing:
                self.engine.dispose()
                raise RuntimeError("Factory 数据库需显式迁移：运行 backend/.venv/bin/alembic upgrade head；缺少 " + ", ".join(missing[:6]))
        with self.lock, Session(self.engine) as session:
            if session.scalar(select(m.Project.id).limit(1)) is None:
                initial = seed if app_mode == "mock" else {
                    **{key: [] for key in COLLECTIONS},
                    "projects": [{"id": "proj-local", "name": "默认项目"}],
                    "settings": seed["settings"],
                }
                self._write(initial, session)
                session.commit()

    def seed(self) -> dict:
        return FactoryState.model_validate(json.loads(self.seed_path.read_text(encoding="utf-8"))).model_dump(mode="json")

    def _upsert(self, session: Session, model, values: dict):
        if model is m.VideoAsset:
            identity = session.get(m.AssetIdentity, values["id"])
            if identity is None:
                identity = m.AssetIdentity(id=values["id"], task_id=values["task_id"], deletion_snapshot={})
                session.add(identity)
                session.flush()
            elif identity.deleted_at is not None:
                raise ValueError("ASSET_ID_PREVIOUSLY_DELETED")
        row = session.get(model, values["id"])
        if row is None:
            row = model()
            session.add(row)
        elif model is m.CollectionIdentity and row.canonical_task_id != values["canonical_task_id"]:
            raise ValueError("COLLECTION_CANONICAL_OWNER_IMMUTABLE")
        for key, value in values.items():
            if key not in model.__table__.columns:
                continue
            if isinstance(model.__table__.columns[key].type, DateTime) and isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            setattr(row, key, value)
        return row

    def _read(self, session: Session) -> dict:
        state = {}
        for key, (model, schema) in COLLECTIONS.items():
            rows = []
            order = (model.created_at, model.id) if hasattr(model, "created_at") else (model.id,)
            for row in session.scalars(select(model).order_by(*order)):
                values = {field: iso(getattr(row, field)) for field in schema.model_fields if hasattr(model, field)}
                if key == "accounts":
                    values["product_ids"] = list(session.scalars(select(m.AccountProduct.product_id).where(m.AccountProduct.account_id == row.id)))
                if key in ("publishing", "collector"):
                    values["product_id"] = values.get("product_id") or ""
                if key == "publishing":
                    values["account_id"] = values.get("account_id") or ""
                rows.append(schema.model_validate(values).model_dump(mode="json"))
            state[key] = rows
        row = session.get(m.FactorySettings, "settings")
        state["settings"] = SettingsView.model_validate({field: getattr(row, field) for field in SettingsView.model_fields}).model_dump(mode="json")
        return FactoryState.model_validate(state).model_dump(mode="json")

    def state(self, *, scope: Literal["raw", "production", "history", "technical"] = "raw") -> dict:
        with self.lock, Session(self.engine) as session:
            state = self._read(session)
        return self._production_state(state) if scope == "production" and self.app_mode == "integration" else state

    @staticmethod
    def _read_rows(session, key, statement):
        model, schema = COLLECTIONS[key]
        return [schema.model_validate({field: iso(getattr(row, field)) for field in schema.model_fields
                if hasattr(model, field)}).model_dump(mode="json") for row in session.scalars(statement)]

    def read_task(self, task_id: str) -> dict | None:
        with self.lock, Session(self.engine) as session:
            rows = self._read_rows(session, "tasks", select(m.Task).where(m.Task.id == task_id))
            return rows[0] if rows else None

    def read_asset(self, asset_id: str) -> dict | None:
        with self.lock, Session(self.engine) as session:
            rows = self._read_rows(session, "assets", select(m.VideoAsset).where(m.VideoAsset.id == asset_id))
            return rows[0] if rows else None

    def next_asset_version(self, task_id: str, kind: str) -> int:
        with self.lock, Session(self.engine) as session:
            versions = [reference.get("version", 0) for reference in session.scalars(
                select(m.VideoAsset.legacy_reference).where(m.VideoAsset.task_id == task_id, m.VideoAsset.type == kind))]
            for snapshot in session.scalars(select(m.AssetIdentity.deletion_snapshot).where(
                    m.AssetIdentity.task_id == task_id, m.AssetIdentity.deleted_at.is_not(None))):
                asset = snapshot.get("asset", {})
                if asset.get("type") == kind:
                    versions.append(asset.get("legacy_reference", {}).get("version", 0))
            return 1 + max(versions, default=0)

    def read_run(self, run_id: str) -> dict | None:
        with self.lock, Session(self.engine) as session:
            rows = self._read_rows(session, "runs", select(m.ProcessingRun).where(m.ProcessingRun.id == run_id))
            return rows[0] if rows else None

    def read_reviews(self, task_id: str) -> list[dict]:
        with self.lock, Session(self.engine) as session:
            return self._read_rows(session, "reviews", select(m.Review).where(m.Review.task_id == task_id).order_by(m.Review.id))

    def read_assets(self, *, task_id: str | None = None, scope="raw") -> list[dict]:
        statement = select(m.VideoAsset).order_by(m.VideoAsset.created_at, m.VideoAsset.id)
        if task_id is not None:
            statement = statement.where(m.VideoAsset.task_id == task_id)
        if scope == "production" and self.app_mode == "integration":
            execution_scope = m.VideoAsset.legacy_reference["execution_scope"].as_string()
            statement = statement.join(m.Task, m.Task.id == m.VideoAsset.task_id).where(
                m.Task.execution_mode == "integration", m.VideoAsset.registry_status != "mock",
                or_(execution_scope.is_(None), execution_scope != "TECHNICAL_VALIDATION"))
        with self.lock, Session(self.engine) as session:
            return self._read_rows(session, "assets", statement)

    def read_asset_context(self, asset_id: str, *, scope="production") -> dict:
        state = {key: [] for key in COLLECTIONS}
        production = scope == "production" and self.app_mode == "integration"
        with self.lock, Session(self.engine) as session:
            asset = session.get(m.VideoAsset, asset_id)
            if asset is None:
                return state
            task_id = asset.task_id
            state["tasks"] = self._read_rows(session, "tasks", select(m.Task).where(m.Task.id == task_id))
            assets = select(m.VideoAsset).where(m.VideoAsset.task_id == task_id)
            if production:
                assets = select(m.VideoAsset).where(or_(m.VideoAsset.task_id == task_id,
                    m.VideoAsset.legacy_reference["execution_scope"].as_string() == "TECHNICAL_VALIDATION"))
            state["assets"] = self._read_rows(session, "assets", assets.order_by(m.VideoAsset.created_at, m.VideoAsset.id))
            technical_ids = [row["id"] for row in state["assets"]
                if row["legacy_reference"].get("execution_scope") == "TECHNICAL_VALIDATION"]
            for key in ("runs", "reviews", "quality_assessments"):
                model = COLLECTIONS[key][0]
                condition = model.task_id == task_id
                if key == "quality_assessments" and production and technical_ids:
                    condition = or_(condition, model.asset_id.in_(technical_ids))
                order = (model.created_at, model.id) if hasattr(model, "created_at") else (model.id,)
                state[key] = self._read_rows(session, key, select(model).where(condition).order_by(*order))
        return self._production_state(state) if production else state

    def read_latest_batch(self, active_statuses) -> dict | None:
        statement = select(m.ProductionBatch).order_by(m.ProductionBatch.created_at.desc(), m.ProductionBatch.id.desc())
        with self.lock, Session(self.engine) as session:
            rows = self._read_rows(session, "batches", statement.where(m.ProductionBatch.status.in_(active_statuses)).limit(1))
            if not rows:
                rows = self._read_rows(session, "batches", statement.limit(1))
            return rows[0] if rows else None

    def _production_state(self, state: dict) -> dict:
        state["tasks"] = [row for row in state["tasks"] if row["execution_mode"] == "integration"]
        task_ids = {row["id"] for row in state["tasks"]}
        technical_assets = [row for row in state["assets"] if row["legacy_reference"].get("execution_scope") == "TECHNICAL_VALIDATION"]
        technical_asset_ids = {row["id"] for row in technical_assets}
        technical_run_ids = {row["legacy_reference"].get("processing_run_id") for row in technical_assets}
        technical_quality = [row for row in state["quality_assessments"] if row["asset_id"] in technical_asset_ids]
        technical_run_ids.update(row["processing_run_id"] for row in technical_quality)
        technical_entity_ids = technical_asset_ids | {row["id"] for row in technical_quality}
        for task in state["tasks"]:
            context = task["context"]
            context.pop("technical_validation", None)
            repairs = context.get("matte_repairs", [])
            technical_repairs = [row for row in repairs if row.get("execution_scope") == "TECHNICAL_VALIDATION" or row.get("person_asset_id") in technical_asset_ids]
            technical_run_ids.update(row.get("run_id") for row in technical_repairs)
            technical_entity_ids.update(row["id"] for row in technical_repairs)
            if "matte_repairs" in context:
                context["matte_repairs"] = [row for row in repairs if row not in technical_repairs]
        state["assets"] = [row for row in state["assets"] if row["task_id"] in task_ids and row["registry_status"] != "mock" and row["id"] not in technical_asset_ids]
        asset_ids = {row["id"] for row in state["assets"]}
        for key in ("reviews", "quality_assessments"):
            state[key] = [row for row in state[key] if row["task_id"] in task_ids and row["asset_id"] in asset_ids]
        state["prompts"] = [row for row in state["prompts"] if row["task_id"] in task_ids and row["status"] != "MOCK"]
        state["runs"] = [row for row in state["runs"] if row["task_id"] in task_ids and row["provider"] != "mock" and not row["provider"].startswith("FactoryTechnical") and row["id"] not in technical_run_ids]
        technical_entity_ids.update(technical_run_ids)
        for key in ("stages", "legacy_bindings", "errors"):
            state[key] = [row for row in state[key] if row["task_id"] in task_ids]
        product_ids = {row["product_id"] for row in state["tasks"]}
        state["products"] = [row for row in state["products"] if row["id"] in product_ids or row["id"] not in self.mock_product_ids and not row["image"].startswith("/mock/")]
        state["accounts"] = [row for row in state["accounts"] if row["id"] not in self.mock_account_ids]
        catalog_ids = {row["id"] for key in ("products", "accounts") for row in state[key]} | {"settings"}
        state["collector"] = [row for row in state["collector"] if row.get("last_task_id") in task_ids]
        state["audits"] = [row for row in state["audits"] if not row["action"].startswith("technical.") and row["entity_id"] not in technical_entity_ids and (row["task_id"] in task_ids or row["task_id"] is None and row["entity_id"] in catalog_ids)]
        state["batches"] = [row for row in state["batches"] if row["task_ids"] and all(task_id in task_ids for task_id in row["task_ids"])]
        state["publishing"] = []
        return state

    def mutate(self, change: Callable[[dict], object], asset_links: dict | None = None):
        with self.lock, Session(self.engine) as session:
            if self.engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            else:
                session.execute(select(m.FactorySettings.id).where(m.FactorySettings.id == "settings").with_for_update())
            state = self._read(session)
            result = change(state)
            self._write(FactoryState.model_validate(state).model_dump(mode="json"), session, asset_links)
            session.commit()
            return result

    def update_processing_run_event(self, task_id: str, run_id: str, values: dict, *, log_message: str | None = None,
                                    task_progress: float | None = None, live_metrics: dict | None = None) -> None:
        allowed = {"last_heartbeat", "progress", "command", "cwd", "pid", "stdout_log_path", "stderr_log_path",
                   "termination_signal", "timed_out", "resource_info"}
        if values.keys() - allowed:
            raise ValueError("PROCESSING_RUN_EVENT_FIELD_INVALID")
        with self.lock, Session(self.engine) as session:
            row = session.get(m.ProcessingRun, run_id)
            if row is None:
                raise ValueError("PROCESSING_RUN_NOT_FOUND")
            if row.task_id != task_id:
                raise ValueError("PROCESSING_RUN_TASK_MISMATCH")
            current = {**record(row), **values}
            if "resource_info" in values and isinstance(values["resource_info"], dict):
                current["resource_info"] = {**(row.resource_info or {}), **values["resource_info"]}
            if log_message is not None:
                current["logs"] = [*row.logs, log_message]
            checked = RunView.model_validate(current).model_dump(mode="json")
            fields = set(values) | ({"logs"} if log_message is not None else set())
            task_values = {}
            if task_progress is not None or live_metrics is not None:
                task = session.get(m.Task, task_id)
                if task is None:
                    raise ValueError("PROCESSING_RUN_TASK_NOT_FOUND")
                current_task = {key: value for key, value in record(task).items() if key in TaskView.model_fields}
                if task_progress is not None:
                    current_task["progress"] = task_progress
                    task_values["progress"] = task_progress
                if live_metrics is not None:
                    current_task["context"] = {**task.context, "live_metrics": live_metrics}
                    task_values["context"] = current_task["context"]
                checked_task = TaskView.model_validate(current_task).model_dump(mode="json")
                task_values = {key: checked_task[key] for key in task_values}
            self._upsert(session, m.ProcessingRun, {"id": run_id, **{key: checked[key] for key in fields}})
            if task_values:
                self._upsert(session, m.Task, {"id": task_id, **task_values})
            session.commit()

    def reset(self) -> None:
        with self.lock, Session(self.engine) as session:
            for task in session.scalars(select(m.Task)):
                task.source_video_id = None
            session.flush()
            # Task/source is the one intentional FK cycle. Unlink it before resetting the mock database.
            tables = list(m.Base.metadata.sorted_tables)
            for table in reversed(tables):
                session.execute(delete(table))
            session.expunge_all()
            self._write(self.seed(), session)
            session.commit()

    def _write(self, state: dict, session: Session, asset_links: dict | None = None) -> None:
        self._upsert(session, m.User, {"id": "user-local", "name": "本地用户"})
        countries = {item["country"] for key in ("tasks", "products", "accounts", "collector") for item in state[key]}
        for country in countries:
            self._upsert(session, m.Country, {"id": context_id("country", country), "name": country})
        session.flush()
        for project in state["projects"]:
            self._upsert(session, m.Project, {**project, "user_id": "user-local"})
        for product in state["products"]:
            self._upsert(session, m.Product, {**product, "country_id": context_id("country", product["country"])})
        session.flush()
        for item in state["tasks"] + state["collector"]:
            self._upsert(session, m.Keyword, {
                "id": context_id("keyword", item["country"] + "|" + item["keyword"] + "|" + item["product_id"]),
                "country_id": context_id("country", item["country"]),
                "product_id": item["product_id"] or None, "text": item["keyword"],
            })
        session.flush()
        for task in state["tasks"]:
            self._upsert(session, m.Task, {
                **task, "source_video_id": None,
                "country_id": context_id("country", task["country"]),
                "keyword_id": context_id("keyword", task["country"] + "|" + task["keyword"] + "|" + task["product_id"]),
            })
        session.flush()
        for task in state["tasks"]:
            source = next((a for a in reversed(state["assets"]) if a["task_id"] == task["id"] and a["type"] == "source"), None)
            if source and task["source_video_id"]:
                rule = next((r for r in state["collector"] if r.get("last_task_id") == task["id"]), {})
                link = (asset_links or {}).get(source["id"], {})
                previous = session.get(m.SourceVideo, task["source_video_id"])
                previous_values = record(previous) if previous else {}
                source_fields = ("external_path", "sha256", "size", "mime_type", "source_system", "legacy_video_id", "legacy_context_id", "legacy_reference", "probe_metadata", "source_url")
                registry = {key: source[key] for key in source_fields if source.get(key) is not None and (source.get(key) != {} or not previous_values.get(key))}
                self._upsert(session, m.SourceVideo, {
                    **previous_values,
                    "id": task["source_video_id"], "task_id": task["id"],
                    "original_filename": source["name"],
                    **{key: source[key] for key in ("fps", "width", "height", "duration", "codec")},
                    "views": previous.views if previous else link.get("views", rule.get("views", 0)),
                    "likes": previous.likes if previous else link.get("likes", rule.get("likes", 0)),
                    **registry, **link.get("source", {}),
                    **link.get("analysis", {}),
                })
        session.flush()
        for task in state["tasks"]:
            if task["source_video_id"]:
                session.get(m.Task, task["id"]).source_video_id = task["source_video_id"]
        for key in ("stages", "assets", "prompts", "reviews", "runs", "errors", "audits"):
            model = COLLECTIONS[key][0]
            for item in state[key]:
                values = item.copy()
                if key == "prompts" and values.get("created_at") is None:
                    values.pop("created_at", None)
                self._upsert(session, model, values)
            session.flush()
        for item in state["accounts"]:
            self._upsert(session, m.PublishingAccount, {**item, "country_id": context_id("country", item["country"])})
        session.flush()
        session.execute(delete(m.AccountProduct))
        for account in state["accounts"]:
            for product_id in account["product_ids"]:
                session.add(m.AccountProduct(account_id=account["id"], product_id=product_id))
        for item in state["publishing"]:
            self._upsert(session, m.PublishingTask, {**item, "account_id": item["account_id"] or None})
        for item in state["collector"]:
            self._upsert(session, m.CollectorRule, {
                **item, "product_id": item["product_id"] or None,
                "country_id": context_id("country", item["country"]),
                "keyword_id": context_id("keyword", item["country"] + "|" + item["keyword"] + "|" + item["product_id"]),
            })
        session.flush()
        for item in state.get("legacy_bindings", []):
            self._upsert(session, m.LegacyBinding, item)
        for key in ("batches", "quality_assessments", "collection_identities"):
            for item in state[key]:
                self._upsert(session, COLLECTIONS[key][0], item)
        rule_ids = [item["id"] for item in state["collector"]]
        session.execute(delete(m.CollectorRule).where(m.CollectorRule.id.not_in(rule_ids)))
        self._upsert(session, m.FactorySettings, {"id": "settings", **state["settings"]})
        session.flush()
        self._asset_details(state, session, asset_links or {})

    def _asset_details(self, state: dict, session: Session, asset_links: dict) -> None:
        for task in state["tasks"]:
            if task.get("execution_mode", "mock") != "mock":
                continue
            workflow_id = "workflow-" + task["id"]
            if session.get(m.WorkflowSnapshot, workflow_id) is None:
                self._upsert(session, m.WorkflowSnapshot, {
                    "id": workflow_id, "task_id": task["id"], "version": "mock-workflow-v1",
                    "workflow": {"app_mode": "mock", "task_id": task["id"], "nodes": [], "provider": "MockComfyUIProvider"},
                })
            session.flush()
            prompt = next((p for p in reversed(state["prompts"]) if p["task_id"] == task["id"]), None)
            assets = [a for a in state["assets"] if a["task_id"] == task["id"]]
            for asset in assets:
                if asset["type"] == "person" and session.get(m.CutoutAsset, "cutout-" + asset["id"]) is None:
                    details = asset_links.get(asset["id"], {}).get("cutout", {})
                    issues = next((r["issues"] for r in state["reviews"] if r["task_id"] == task["id"]), [])
                    self._upsert(session, m.CutoutAsset, {
                        "id": "cutout-" + asset["id"], "task_id": task["id"], "asset_id": asset["id"],
                        "workflow_id": workflow_id, "model": details.get("model", "Mock BiRefNet + MatAnyone2"), "model_version": details.get("model_version", "mock-v1"),
                        "alpha_quality": details.get("alpha_quality", 0.97), "temporal_stability": details.get("temporal_stability", 0.93),
                        "missing_body_score": details.get("missing_body_score", 0.15 if "Missing Body" in issues else 0),
                        "flicker_score": details.get("flicker_score", 0.2 if "Mask Flicker" in issues else 0),
                        "intervals": details.get("intervals", [{"start": 13, "end": 16, "issue": "人物局部缺失（Mock）"}] if issues else []),
                    })
                if asset["type"] == "background" and session.get(m.BackgroundAsset, "background-" + asset["id"]) is None:
                    link = asset_links.get(asset["id"], {})
                    self._upsert(session, m.BackgroundAsset, {
                        "id": "background-" + asset["id"], "task_id": task["id"], "asset_id": asset["id"],
                        "prompt_id": link.get("prompt_id", prompt["id"] if prompt else None),
                        "provider": link.get("provider", prompt["provider"] if prompt else "manual"),
                    })
            session.flush()
            background = next((a for a in reversed(assets) if a["type"] == "background"), None)
            for asset in assets:
                if asset["type"] in ("compose", "final") and session.get(m.CompositionAsset, "composition-" + asset["id"]) is None:
                    link = asset_links.get(asset["id"], {})
                    person = next((a for a in reversed(assets) if a["type"] == "person"), None)
                    self._upsert(session, m.CompositionAsset, {
                        "id": "composition-" + asset["id"], "task_id": task["id"], "asset_id": asset["id"],
                        "source_video_id": task["source_video_id"], "background_id": link.get("background_id", "background-" + background["id"] if background else None),
                        "person_asset_id": link.get("person_asset_id", person["id"] if person else None),
                        "parameters": link.get("parameters", {"exposure": 0, "contrast": 1, "saturation": 1, "crop": "center", "width": asset["width"], "height": asset["height"], "fps": asset["fps"]}),
                    })
        session.flush()

    def workflow(self, task_id: str) -> dict | None:
        with self.lock, Session(self.engine) as session:
            row = session.scalar(select(m.WorkflowSnapshot).where(m.WorkflowSnapshot.task_id == task_id).order_by(m.WorkflowSnapshot.created_at.desc()))
            return WorkflowView.model_validate(record(row)).model_dump(mode="json") if row else None

    def save_workflow(self, value: dict) -> None:
        with self.lock, Session(self.engine) as session:
            if session.get(m.WorkflowSnapshot, value["id"]):
                raise ValueError("Workflow snapshot is immutable")
            self._upsert(session, m.WorkflowSnapshot, value)
            session.commit()

    def workflows(self, task_id: str) -> list[dict]:
        with self.lock, Session(self.engine) as session:
            return [WorkflowView.model_validate(record(row)).model_dump(mode="json") for row in session.scalars(
                select(m.WorkflowSnapshot).where(m.WorkflowSnapshot.task_id == task_id).order_by(m.WorkflowSnapshot.created_at))]

    def register_real_asset_links(self, asset: dict, task: dict) -> None:
        with self.lock, Session(self.engine) as session:
            self._register_real_asset_links(session, asset, task)
            session.commit()

    def _register_real_asset_links(self, session, asset, task):
        reference = asset["legacy_reference"]
        if asset["type"] == "background":
            self._upsert(session, m.BackgroundAsset, {"id": "background-" + asset["id"], "task_id": task["id"],
                "asset_id": asset["id"], "prompt_id": reference.get("prompt_id"), "provider": asset["source_system"]})
        elif asset["type"] in {"compose", "final"}:
            self._upsert(session, m.CompositionAsset, {"id": "composition-" + asset["id"], "task_id": task["id"],
                "asset_id": asset["id"], "source_video_id": task["source_video_id"],
                "background_id": "background-" + reference["background_asset_id"] if reference.get("background_asset_id") else None,
                "person_asset_id": reference.get("person_asset_id"), "parameters": asset["probe_metadata"]})

    def register_real_asset(self, asset: dict, task: dict) -> dict:
        with self.lock, Session(self.engine) as session:
            state = self._read(session)
            reference = asset["legacy_reference"]
            previous = next((item for item in state["assets"] if item["task_id"] == asset["task_id"] and item["type"] == asset["type"]
                and item.get("sha256") == asset["sha256"] and all(item.get("legacy_reference", {}).get(key) == reference.get(key)
                for key in ("alpha_sha256", "factory_fingerprint", "prompt_id", "person_asset_id", "background_asset_id", "manifest_sha256"))), None)
            if previous:
                return previous
            reference["version"] = self.next_asset_version(asset["task_id"], asset["type"])
            state["assets"].append(asset)
            self._write(FactoryState.model_validate(state).model_dump(mode="json"), session)
            self._register_real_asset_links(session, asset, task)
            session.commit()
            return asset

    def cutout_details(self, task_id: str) -> list[dict]:
        with self.lock, Session(self.engine) as session:
            return [record(row) for row in session.scalars(select(m.CutoutAsset).where(m.CutoutAsset.task_id == task_id).order_by(m.CutoutAsset.created_at.desc(), m.CutoutAsset.id.desc()))]

    def secret_reference(self, account_id: str) -> str | None:
        with self.lock, Session(self.engine) as session:
            row = session.get(m.PublishingAccount, account_id)
            return row.secret_ref if row else None

    def metadata(self, task_id: str, *, scope: Literal["production", "history"] = "production") -> dict | None:
        state = self.state(scope=scope)
        task = next((item for item in state["tasks"] if item["id"] == task_id), None)
        if task is None:
            return None
        assets = [item for item in state["assets"] if item["task_id"] == task_id]
        background = next((item for item in reversed(assets) if item["type"] == "background"), {})
        final = next((item for item in reversed(assets) if item["type"] == "final"), {})
        prompt = next((item for item in reversed(state["prompts"]) if item["task_id"] == task_id), {})
        publishing = next((item for item in reversed(state["publishing"]) if item["task_id"] == task_id), {})
        production = scope == "production" and task["execution_mode"] == "integration"
        if production:
            selected = task["context"].get("production", {}).get("current_person_asset_id")
            person = next((item for item in reversed(assets) if item["type"] == "person" and (not selected or item["id"] == selected)), {})
            run_ids = {item["id"] for item in state["runs"] if item["task_id"] == task_id}
            workflows = [item for item in self.workflows(task_id) if item.get("processing_run_id") in run_ids]
            workflow_ids = {item["id"] for item in workflows}
            cutout = next((item for item in self.cutout_details(task_id) if item["asset_id"] == person.get("id") and item["workflow_id"] in workflow_ids), {})
            workflow = next((item for item in reversed(workflows) if item["id"] == cutout.get("workflow_id")
                or not cutout and person and item.get("processing_run_id") == person["legacy_reference"].get("processing_run_id")), {})
        else:
            workflow = self.workflow(task_id) or {}
            cutout = next(iter(self.cutout_details(task_id)), {})
        with self.lock, Session(self.engine) as session:
            source = session.get(m.SourceVideo, task["source_video_id"]) if task["source_video_id"] else None
            source_values = record(source) if source else {}
            if production:
                registered_source = next((item for item in reversed(assets) if item["type"] == "source" and item["registry_status"] == "registered"), None)
                if (not registered_source or source_values.get("sha256") != registered_source.get("sha256")
                        or source_values.get("legacy_reference", {}).get("execution_scope") == "TECHNICAL_VALIDATION"):
                    source_values = {}
            background_row = session.get(m.BackgroundAsset, "background-" + background["id"]) if background else None
            if background_row:
                prompt_row = session.get(m.Prompt, background_row.prompt_id) if background_row.prompt_id else None
                prompt = record(prompt_row) if prompt_row else {}
                prompt["provider"] = background_row.provider
        return VideoMetadata.model_validate({
            "task_id": task_id, **{key: task[key] for key in ("country", "keyword", "product_id")},
            "source_video": {key: source_values.get(key, {} if key in ("legacy_reference", "probe_metadata") else None) for key in ("original_filename", "fps", "width", "height", "duration", "codec", "source_url", "external_path", "sha256", "size", "mime_type", "source_system", "legacy_video_id", "legacy_context_id", "download_task_id", "r_frame_rate", "avg_frame_rate", "legacy_reference", "probe_metadata")},
            "cutout": {"model": cutout.get("model"), "model_version": cutout.get("model_version"), "workflow_version": workflow.get("version"), "created_at": cutout.get("created_at")},
            "background": {"provider": prompt.get("provider"), "prompt": prompt.get("prompt"), "negative_prompt": prompt.get("negative_prompt"), "image_filename": background.get("name")},
            "composition": {key: final.get(key) for key in ("fps", "width", "height", "codec")},
            "quality": {**{key: source_values.get(key) for key in ("ai_match_score", "product_match_score", "overlay_detected", "watermark_detected")}, "mask_flicker_score": cutout.get("flicker_score"), "missing_body_score": cutout.get("missing_body_score")},
            "publishing": {key: publishing.get(key) for key in ("account_id", "caption", "product_binding", "status")},
        }).model_dump(mode="json")
