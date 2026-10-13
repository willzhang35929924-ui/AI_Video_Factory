import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models as m
from app.integrations.collector_board import CollectorBoard
from app.integrations.collector_review import CandidateDecision, CollectorCandidateReview
from app.integrations.collection_identity import claim_identity, history_groups, identity_for, lookup_identity
from app.repository import StateRepository


BACKEND = Path(__file__).parents[1]
VIDEO_ID = "7684584667923664150"


class CollectionIdentityAtomicTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="factory-dedupe-atomic-")
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "identity.db"
        self.url = "sqlite:///" + str(self.database)
        self.repository = self.make_repository()
        stamp = datetime.now(timezone.utc).isoformat()

        def inputs(state):
            state["products"].append({"id": "product-a", "name": "Product", "country": "MX", "image": "",
                "url": "", "binding_reference": "", "description": "", "status": "active", "keywords": ["Product"]})
            for task_id, country in (("first", "MX"), ("second", "FR")):
                state["tasks"].append({"id": task_id, "project_id": "proj-local", "country": country,
                    "keyword": "Product", "product_id": "product-a", "source_video_id": None,
                    "current_stage": "collector", "status": "CREATED", "progress": 0, "thumbnail": "",
                    "created_at": stamp, "updated_at": stamp, "execution_mode": "integration",
                    "context": {"task_context": {"context_id": "context-" + task_id}}, "collector_result": {}})
        self.repository.mutate(inputs)

    def make_repository(self):
        repository = StateRepository(self.url, BACKEND / "app/seed.json", app_mode="integration")
        self.addCleanup(repository.engine.dispose)
        return repository

    def test_native_id_is_stable_across_urls_and_different_ids_do_not_collapse(self):
        variants = [
            "https://www.tiktok.com/@creator/video/" + VIDEO_ID,
            "https://m.tiktok.com/v/" + VIDEO_ID + ".html?share_app_id=1",
            "https://www.tiktok.com/share/video/" + VIDEO_ID + "?utm_source=copy#fragment",
            "https://www.tiktok.com/embed/v2/" + VIDEO_ID,
            "https://www.tiktok.com/?item_id=" + VIDEO_ID,
        ]
        self.assertEqual({identity_for(url=url)["id"] for url in variants}, {"tiktok:" + VIDEO_ID})
        self.assertEqual(identity_for(VIDEO_ID)["id"], "tiktok:" + VIDEO_ID)
        self.assertNotEqual(identity_for("7684584667923664151")["id"], identity_for(VIDEO_ID)["id"])
        self.assertNotEqual(identity_for(url=variants[0])["id"], identity_for(url=variants[0].replace(VIDEO_ID, "7684584667923664151"))["id"])

    def test_unresolved_short_urls_do_not_invent_identity(self):
        self.assertIsNone(identity_for(url="https://vm.tiktok.com/ZSUnresolved/"))
        self.assertIsNone(identity_for(url="https://vt.tiktok.com/ZSUnresolved/"))
        self.assertIsNone(identity_for("not-a-native-id"))
        state = self.repository.state()
        self.assertIsNone(claim_identity(state, "first", url="https://vm.tiktok.com/ZSUnresolved/"))
        self.assertEqual(state["collection_identities"], [])
        self.assertNotIn("collection_identity", state["tasks"][0]["context"])

    def test_conflicting_native_evidence_and_other_platform_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "COLLECTION_IDENTITY_CONFLICT"):
            identity_for(VIDEO_ID, "https://www.tiktok.com/@same-title/video/7684584667923664151")
        with self.assertRaisesRegex(ValueError, "COLLECTION_IDENTITY_CONFLICT"):
            identity_for(url="https://www.tiktok.com/@creator/video/" + VIDEO_ID + "?item_id=7684584667923664151")
        with self.assertRaisesRegex(ValueError, "COLLECTION_PLATFORM_MISMATCH"):
            identity_for(VIDEO_ID, "https://tiktok.com.example.com/video/" + VIDEO_ID)

    def test_history_lookup_preserves_prior_outcomes_and_chooses_first_collected_attempt(self):
        def history(state):
            for task in state["tasks"]:
                task["context"]["collection_source"] = {"video_id": VIDEO_ID, "video_url": "https://www.tiktok.com/@creator/video/" + VIDEO_ID}
            state["tasks"][0]["status"] = "CANCELLED"
            state["tasks"][1]["status"] = "REJECTED"
            state["tasks"][1]["collector_result"] = {"video_id": VIDEO_ID, "decision": "rejected", "decision_reason": "PRODUCT_MISMATCH"}
        self.repository.mutate(history)
        state = self.repository.state()
        existing = lookup_identity(state, VIDEO_ID)
        self.assertEqual(existing["canonical_task_id"], "second")
        result = claim_identity(state, "first", VIDEO_ID)
        self.assertTrue(result["duplicate"])
        self.assertEqual([task["status"] for task in state["tasks"]], ["CANCELLED", "REJECTED"])
        self.assertEqual(state["tasks"][1]["collector_result"]["decision_reason"], "PRODUCT_MISMATCH")
        groups, conflicts = history_groups(state)
        self.assertEqual(len(groups["tiktok:" + VIDEO_ID]), 2)
        self.assertEqual(conflicts, [])

    def test_distinct_repository_connections_atomically_choose_one_owner(self):
        other = self.make_repository()
        ready = threading.Barrier(2)
        original_counts = {key: len(self.repository.state()[key]) for key in ("reviews", "runs", "assets")}

        def claim(repository, task_id):
            ready.wait(timeout=10)
            return repository.mutate(lambda state: claim_identity(state, task_id, VIDEO_ID))

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(claim, self.repository, "first")
            second = pool.submit(claim, other, "second")
            results = [first.result(timeout=20), second.result(timeout=20)]
        self.assertEqual(sum(not result["duplicate"] for result in results), 1)
        self.assertEqual(len({result["canonical_task_id"] for result in results}), 1)
        state = self.repository.state()
        self.assertEqual(len(state["collection_identities"]), 1)
        self.assertEqual(sorted(task["context"]["collection_identity"]["status"] for task in state["tasks"]), ["CANONICAL", "DUPLICATE_SKIPPED"])
        self.assertEqual({key: len(state[key]) for key in original_counts}, original_counts)
        owner = state["collection_identities"][0]["canonical_task_id"]
        retried = other.mutate(lambda snapshot: claim_identity(snapshot, owner, VIDEO_ID))
        self.assertFalse(retried["duplicate"])
        self.assertEqual(len(other.state()["collection_identities"]), 1)

    def test_separate_python_processes_share_the_same_atomic_identity(self):
        script = """
import json, sys
from pathlib import Path
from app.repository import StateRepository
from app.integrations.collection_identity import claim_identity
repository = StateRepository(sys.argv[1], Path(sys.argv[2]), app_mode='integration')
result = repository.mutate(lambda state: claim_identity(state, sys.argv[3], sys.argv[4]))
print(json.dumps(result))
repository.engine.dispose()
"""
        children = [subprocess.Popen([sys.executable, "-c", script, self.url, str(BACKEND / "app/seed.json"), task_id, VIDEO_ID],
                    cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for task_id in ("first", "second")]
        results = []
        for child in children:
            output, error = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, error)
            results.append(json.loads(output))
        self.assertEqual(sum(result["duplicate"] for result in results), 1)
        self.assertEqual(len({result["canonical_task_id"] for result in results}), 1)
        self.assertEqual(len(self.repository.state()["collection_identities"]), 1)
        with self.repository.engine.connect() as connection:
            self.assertEqual(connection.execute(text("PRAGMA foreign_key_check")).all(), [])

    def test_database_unique_constraint_rejects_a_second_id_for_same_native_video(self):
        self.repository.mutate(lambda state: claim_identity(state, "first", VIDEO_ID))
        with Session(self.repository.engine) as session:
            session.add(m.CollectionIdentity(id="wrong-second-key", platform="tiktok", native_video_id=VIDEO_ID,
                canonical_task_id="second", canonical_url="https://www.tiktok.com/video/" + VIDEO_ID,
                confidence="NATIVE_ID", first_seen_at=datetime.now(timezone.utc)))
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()
            self.assertEqual(len(list(session.scalars(select(m.CollectionIdentity)))), 1)
        constraints = inspect(self.repository.engine).get_unique_constraints("collection_identities")
        self.assertTrue(any(constraint["column_names"] == ["platform", "native_video_id"] for constraint in constraints))

    def test_postgresql_offline_migration_has_native_unique_constraint_and_task_fk(self):
        output = StringIO()
        config = Config(str(BACKEND / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND / "migrations"))
        config.set_main_option("sqlalchemy.url", "postgresql://unit:unused@127.0.0.1/never_connect")
        config.output_buffer = output
        command.upgrade(config, "0010_asset_identity:0011_collection_identity", sql=True)
        ddl = output.getvalue()
        self.assertIn("CREATE TABLE collection_identities", ddl)
        self.assertIn("CONSTRAINT uq_collection_platform_video UNIQUE (platform, native_video_id)", ddl)
        self.assertIn("FOREIGN KEY(canonical_task_id) REFERENCES tasks (id)", ddl)
        self.assertIn("TIMESTAMP WITH TIME ZONE", ddl)

    def test_sqlite_migration_applies_twice_without_changing_existing_tasks(self):
        original = self.repository.state()["tasks"]
        with self.repository.engine.begin() as connection:
            connection.execute(text("DROP TABLE collection_identities"))
        config = Config(str(BACKEND / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND / "migrations"))
        config.set_main_option("sqlalchemy.url", self.url)
        command.stamp(config, "0010_asset_identity")
        command.upgrade(config, "0011_collection_identity")
        command.upgrade(config, "0011_collection_identity")
        self.assertEqual(self.repository.state()["tasks"], original)
        self.assertEqual(self.repository.state()["collection_identities"], [])
        with self.repository.engine.connect() as connection:
            self.assertEqual(connection.execute(text("PRAGMA foreign_key_check")).all(), [])
            self.assertEqual(connection.execute(text("PRAGMA integrity_check")).scalar(), "ok")

    def test_canonical_owner_is_immutable_and_failed_change_rolls_back(self):
        self.repository.mutate(lambda state: claim_identity(state, "first", VIDEO_ID))
        before = self.repository.state()

        def unsafe_change(state):
            state["tasks"][0]["status"] = "REJECTED"
            state["collection_identities"][0]["canonical_task_id"] = "second"

        with self.assertRaisesRegex(ValueError, "COLLECTION_CANONICAL_OWNER_IMMUTABLE"):
            self.repository.mutate(unsafe_change)
        self.assertEqual(self.repository.state(), before)

    def test_technical_validation_assets_and_tasks_never_own_production_identity(self):
        state = self.repository.state()
        state["assets"] = [{"id": "technical-source", "task_id": "first", "type": "source",
            "legacy_video_id": VIDEO_ID, "source_url": "https://www.tiktok.com/@technical/video/" + VIDEO_ID,
            "legacy_reference": {"execution_scope": "TECHNICAL_VALIDATION"}}]
        groups, conflicts = history_groups(state)
        self.assertEqual((groups, conflicts), ({}, []))
        self.assertIsNone(lookup_identity(state, VIDEO_ID))
        self.assertEqual(state["collection_identities"], [])
        state["assets"][0]["legacy_reference"] = {}
        state["tasks"][0]["context"]["execution_scope"] = "TECHNICAL_VALIDATION"
        state["tasks"][0]["collector_result"] = {"video_id": VIDEO_ID}
        self.assertEqual(history_groups(state), ({}, []))
        state["tasks"][0]["context"].pop("execution_scope")
        groups, conflicts = history_groups(state)
        self.assertEqual(set(groups), {"tiktok:" + VIDEO_ID})
        self.assertEqual(conflicts, [])
        self.assertEqual(lookup_identity(state, VIDEO_ID)["canonical_task_id"], "first")

    def test_board_counts_alias_as_raw_history_and_review_cannot_approve_it(self):
        stamp = datetime.now(timezone.utc).isoformat()

        def candidates(state):
            for task in state["tasks"]:
                task["context"].update(production_rules={"sha256": "test-rule-snapshot"})
                task["context"]["task_context"].update(video_id=VIDEO_ID,
                    video_url="https://www.tiktok.com/@creator/video/" + VIDEO_ID)
                task.update(status="WAITING_REVIEW", collector_result={"video_id": VIDEO_ID,
                    "video_url": "https://www.tiktok.com/@creator/video/" + VIDEO_ID,
                    "decision": "manual_review", "decision_reason": "PRODUCT_UNCERTAIN"})
                state["runs"].append({"id": "candidate-run-" + task["id"], "task_id": task["id"],
                    "stage": "collector", "provider": "LegacyCollectorAdapter", "status": "failed", "progress": 0,
                    "logs": ["ISOLATED_TEST"], "created_at": stamp})
            claim_identity(state, "first", VIDEO_ID)
            claim_identity(state, "second", VIDEO_ID)
            state["audits"].append({"id": "alias-encounter", "task_id": "second", "action": "collector.duplicate_seen",
                "entity_id": "tiktok:" + VIDEO_ID + ":second", "message": "Same native source", "created_at": stamp})

        self.repository.mutate(candidates)
        board = CollectorBoard(self.repository)
        result = board.list()
        counts = {category["status"]: category["count"] for category in result["categories"]}
        self.assertEqual((result["total"], result["matching_total"], result["effective_total"], result["duplicate_total"], result["duplicate_encounters"]), (2, 2, 1, 1, 1))
        self.assertEqual(counts["DUPLICATE_SKIPPED"], 1)
        self.assertEqual(counts["WAITING_REVIEW"], 1)
        alias_page = board.list(status="DUPLICATE_SKIPPED")
        self.assertEqual([task["id"] for task in alias_page["items"]], ["second"])
        self.assertEqual(alias_page["items"][0]["status"], "WAITING_REVIEW")
        self.assertEqual(alias_page["items"][0]["context"]["collection_identity"]["canonical_task_id"], "first")
        adapter = SimpleNamespace(collect_one=AsyncMock())
        collector = SimpleNamespace(repository=self.repository, hub=SimpleNamespace(broadcast=AsyncMock()), adapter=adapter)
        review = CollectorCandidateReview(collector)
        alias = review.detail("second")
        self.assertEqual(alias["state"], "DUPLICATE_SKIPPED")
        self.assertFalse(alias["reviewable"])
        self.assertIn("first", alias["next_step"])
        self.assertTrue(review.detail("first")["reviewable"])
        before = self.repository.state()
        with self.assertRaises(HTTPException) as blocked:
            asyncio.run(review.decide("second", CandidateDecision(action="accept", reason="ISOLATED_TEST",
                fingerprint=alias["fingerprint"], confirmed=True)))
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertIn("CANDIDATE_NOT_REVIEWABLE", blocked.exception.detail)
        self.assertEqual(self.repository.state(), before)
        adapter.collect_one.assert_not_called()
        collector.hub.broadcast.assert_not_called()


if __name__ == "__main__":
    unittest.main()
