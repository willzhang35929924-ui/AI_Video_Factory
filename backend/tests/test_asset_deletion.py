import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.config import Settings
from app.domain import now
from app.integrations.asset_registry import AssetRegistry, file_hash
from app.main import create_app
from app.repository import StateRepository


class AssetDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="factory-delete-unit-")
        self.root = Path(self.temporary.name)
        self.settings = Settings(app_mode="integration", integration_dry_run=True,
            database_url="sqlite:///" + str(self.root / "factory.db"), storage_root=self.root / "storage")
        self.settings.storage_root.mkdir()
        fixture = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json")
        seed = fixture.state()
        self.task = {**deepcopy(seed["tasks"][0]), "id": "unit-delete-task", "execution_mode": "integration",
            "source_video_id": None, "status": "FAILED", "context": {}, "collector_result": {}}
        self.template = deepcopy(seed["assets"][0])
        fixture.mutate(lambda state: state["tasks"].append(self.task))
        fixture.engine.dispose()
        self.app = create_app(self.settings)
        self.repository = self.app.state.repository
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.repository.engine.dispose()
        self.temporary.cleanup()

    def add_asset(self, identifier="unit-disposable", kind="background", path=None, reference=None):
        path = Path(path) if path else self.settings.storage_root / (identifier + ".unit")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"UNIT_FIXTURE_NOT_PRODUCTION_MEDIA_" + identifier.encode())
        asset = {**deepcopy(self.template), "id": identifier, "task_id": self.task["id"], "type": kind,
            "status": "READY", "name": path.name, "registry_status": "registered", "storage_path": str(path),
            "external_path": str(path), "sha256": file_hash(path), "size": path.stat().st_size,
            "source_system": "UNIT_TEST_ONLY", "legacy_reference": reference or {"version": 1}, "created_at": now()}
        self.repository.mutate(lambda state: state["assets"].append(asset))
        return asset, path

    def plan(self, identifier="unit-disposable"):
        response = self.client.get(f"/api/assets/{identifier}/deletion-status")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def remove(self, plan, **kwargs):
        return self.client.request("DELETE", f"/api/assets/{plan['asset_id']}",
            json={"fingerprint": plan["fingerprint"], "confirmed": True, **kwargs})

    def test_deletes_actual_factory_file_and_registry_row_retaining_audit_identity(self):
        asset, path = self.add_asset()
        plan = self.plan()
        self.assertTrue(plan["can_delete"])
        response = self.remove(plan)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "DELETED")
        self.assertEqual(response.json()["released_bytes"], asset["size"])
        self.assertGreaterEqual(response.json()["deleted_allocated_bytes"], 0)
        self.assertIsInstance(response.json()["disk_free_after"], int)
        self.assertFalse(path.exists())
        self.assertTrue(response.json()["record_deleted"])
        self.assertEqual(self.client.get(f"/api/assets/{asset['id']}/registry").status_code, 404)
        self.assertNotIn(asset["id"], [row["id"] for row in self.repository.state()["assets"]])
        self.assertIn(self.task["id"], [row["id"] for row in self.repository.state()["tasks"]])
        self.assertTrue(any(row["action"] == "asset.delete" and row["entity_id"] == asset["id"] for row in self.repository.state()["audits"]))
        with Session(self.repository.engine) as session:
            self.assertIsNone(session.get(m.VideoAsset, asset["id"]))
            identity = session.get(m.AssetIdentity, asset["id"])
            self.assertIsNotNone(identity.deleted_at)
            self.assertEqual(identity.deletion_snapshot["asset"]["sha256"], asset["sha256"])
            self.assertEqual(identity.deletion_snapshot["status"], "DELETED")

    def test_preserves_registered_background_typed_row_without_touching_prompt(self):
        asset, path = self.add_asset()
        self.repository.register_real_asset_links(asset, self.task)
        response = self.remove(self.plan())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(path.exists())
        with Session(self.repository.engine) as session:
            self.assertIsNotNone(session.scalar(select(m.BackgroundAsset).where(m.BackgroundAsset.asset_id == asset["id"])))

    def test_confirmation_fingerprint_and_extra_scope_are_not_optional(self):
        _, path = self.add_asset()
        plan = self.plan()
        self.assertEqual(self.remove(plan, confirmed=False).status_code, 422)
        self.assertEqual(self.remove(plan, fingerprint="a" * 64).status_code, 409)
        self.assertEqual(self.remove(plan, scope="technical").status_code, 422)
        path.write_bytes(b"changed-size")
        self.assertEqual(self.remove(plan).status_code, 409)
        self.assertTrue(path.exists())

    def test_reference_added_after_preflight_cannot_delete(self):
        asset, path = self.add_asset()
        plan = self.plan()
        self.repository.mutate(lambda state: state["reviews"].append({"id": "unit-review", "task_id": self.task["id"],
            "asset_id": asset["id"], "status": "approved", "issues": [], "note": "UNIT_TEST_ONLY"}))
        response = self.remove(plan)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(path.exists())
        self.assertEqual(response.json()["detail"]["code"], "DELETE_PLAN_CHANGED")
        plan = self.plan()
        self.assertTrue(plan["can_delete"])
        self.assertEqual(self.remove(plan).json()["detail"]["code"], "DELETE_IMPACT_CONFIRMATION_REQUIRED")
        self.assertEqual(self.remove(plan, impact_confirmed=True).status_code, 200)
        self.assertFalse(path.exists())
        self.assertIn("unit-review", [row["id"] for row in self.repository.state()["reviews"]])

    def test_external_and_symlinked_legacy_files_never_deleted(self):
        legacy = self.root / "legacy" / "original.mp4"
        asset, path = self.add_asset(path=legacy)
        self.assertIn("EXTERNAL_LEGACY_FILE", self.plan()["reasons"])
        self.assertEqual(self.remove(self.plan()).status_code, 409)
        self.assertTrue(path.exists())
        symlink = self.settings.storage_root / "symlink.mp4"
        symlink.symlink_to(legacy)
        self.add_asset("unit-symlink", path=symlink)
        self.assertIn("EXTERNAL_LEGACY_FILE", self.plan("unit-symlink")["reasons"])
        self.assertEqual(self.remove(self.plan("unit-symlink")).status_code, 409)
        self.assertTrue(legacy.exists())

    def test_shared_files_survive_other_asset_registry_deletion(self):
        asset, path = self.add_asset()
        self.add_asset("unit-shared", path=path)
        self.assertIn("SHARED_FILE", self.plan()["warnings"])
        self.assertEqual(self.remove(self.plan()).status_code, 422)
        other, _ = self.add_asset("unit-parent")
        self.add_asset("unit-child", reference={"parent_asset_id": other["id"], "version": 1})
        self.assertIn("REFERENCED", self.plan(other["id"])["warnings"])
        self.assertEqual(self.remove(self.plan(), impact_confirmed=True).status_code, 200)
        self.assertTrue(path.exists())
        self.assertEqual(self.repository.read_asset(asset["id"]), None)
        shared = next(row for row in self.repository.state()["assets"] if row["id"] == "unit-shared")
        self.assertEqual(shared["status"], "READY")
        self.assertTrue(self.client.get(f"/api/assets/{shared['id']}/registry").json()["media"]["available"])

    def test_current_person_composition_history_allow_delete_but_running_is_protected(self):
        self.add_asset("unit-person", "person")
        self.assertIn("CURRENT_PERSON_ASSET", self.plan("unit-person")["warnings"])
        self.add_asset("unit-compose", "compose")
        self.assertIn("CURRENT_COMPOSITION_ASSET", self.plan("unit-compose")["warnings"])
        self.add_asset("unit-old", reference={"version": 1, "prompt_id": "unit-same-prompt"})
        self.add_asset("unit-new", reference={"version": 2, "prompt_id": "unit-same-prompt"})
        self.assertIn("HISTORY_VERSION", self.plan("unit-old")["warnings"])
        self.repository.mutate(lambda state: state["runs"].append({"id": "unit-active-run", "task_id": self.task["id"],
            "stage": "cutout", "status": "running", "progress": 0, "logs": [], "created_at": now(), "provider": "UNIT_TEST_ONLY"}))
        self.assertIn("RUNNING", self.plan("unit-new")["reasons"])
        self.assertFalse(self.plan("unit-new")["can_delete"])
        self.assertEqual(self.remove(self.plan("unit-new")).status_code, 409)
        self.assertEqual(self.remove(self.plan("unit-new"), impact_confirmed=True).status_code, 409)
        self.assertIn("unit-active-run", [row["id"] for row in self.repository.state()["runs"]])

    def test_technical_factory_media_can_be_deleted_with_explicit_impact(self):
        asset, path = self.add_asset(reference={"execution_scope": "TECHNICAL_VALIDATION"})
        response = self.client.get(f"/api/assets/{asset['id']}/deletion-status?scope=technical")
        self.assertIn("PROTECTED_TECHNICAL_ASSET", response.json()["warnings"])
        self.assertEqual(self.remove(response.json()).status_code, 422)
        self.assertEqual(self.remove(response.json(), impact_confirmed=True).status_code, 200)
        self.assertFalse(path.exists())

    def test_manifest_previews_and_matching_diagnostic_cache_deleted_only(self):
        manifest = self.settings.storage_root / "disposable" / "manifest.json"
        manifest.parent.mkdir()
        preview = manifest.parent / "preview.mp4"
        preview.write_bytes(b"UNIT_PREVIEW")
        manifest.write_text(json.dumps({"previews": [{"filename": "preview.mp4"}]}))
        asset, path = self.add_asset(reference={"manifest_path": str(manifest), "version": 1})
        cache = self.settings.storage_root / "diagnostic_previews" / "unit-match"
        cache.mkdir(parents=True)
        cache_file = cache / "Alpha.mp4"
        cache_file.write_bytes(b"UNIT_CACHE")
        (cache / "preview.json").write_text(json.dumps({"inputs": {"asset_id": asset["id"]}}))
        unrelated = self.settings.storage_root / "diagnostic_previews" / "unit-other"
        unrelated.mkdir()
        (unrelated / "preview.json").write_text(json.dumps({"inputs": {"asset_id": "different-id"}}))
        response = self.remove(self.plan())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(any(value.exists() for value in (path, manifest, preview, cache_file)))
        self.assertTrue((unrelated / "preview.json").exists())

    def test_database_failure_restores_file_and_record(self):
        _, path = self.add_asset()
        plan = self.plan()
        with patch("app.integrations.asset_registry.Session.commit", side_effect=RuntimeError("UNIT_DATABASE_FAILURE")):
            response = self.remove(plan)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(path.exists())
        self.assertIn("unit-disposable", [row["id"] for row in self.repository.state()["assets"]])

    def test_bulk_failure_does_not_stop_later_unreferenced_asset(self):
        self.add_asset("unit-protected", path=self.root / "legacy-only.unit")
        _, path = self.add_asset()
        plans = [self.plan("unit-protected"), self.plan()]
        response = self.client.post("/api/assets/bulk-delete", json={"items": [{"asset_id": row["asset_id"],
            "fingerprint": row["fingerprint"], "confirmed": True} for row in plans]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["deleted"], 1)
        self.assertEqual(response.json()["failed"], 1)
        self.assertFalse(path.exists())

    def test_source_lineage_path_is_not_mistaken_for_owned_media(self):
        original = self.root / "legacy-original.mp4"
        original.write_bytes(b"PROTECTED_UNIT_ORIGINAL")
        asset, path = self.add_asset(reference={"source_audio_path": str(original), "source_path": str(original), "version": 1})
        plan = self.plan()
        self.assertTrue(plan["can_delete"])
        self.assertNotIn(str(original), [row["path"] for row in plan["files"]])
        self.assertEqual(self.remove(plan).status_code, 200)
        self.assertFalse(path.exists())
        self.assertTrue(original.exists())

    def test_successful_deleted_record_cannot_be_deleted_again(self):
        self.add_asset()
        self.assertEqual(self.remove(self.plan()).status_code, 200)
        self.assertEqual(self.client.get("/api/assets/unit-disposable/deletion-status").status_code, 404)

    def test_bulk_shared_files_are_counted_once_and_all_selected_records_removed(self):
        asset, path = self.add_asset()
        other, _ = self.add_asset("unit-shared", path=path)
        plans = [self.plan(asset["id"]), self.plan(other["id"])]
        response = self.client.post("/api/assets/bulk-delete", json={"items": [{"asset_id": row["asset_id"],
            "fingerprint": row["fingerprint"], "confirmed": True, "impact_confirmed": True} for row in plans]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["deleted"], 2)
        self.assertEqual(response.json()["failed"], 0)
        self.assertEqual(sum(row["released_bytes"] for row in response.json()["results"]), asset["size"])
        self.assertFalse(path.exists())
        self.assertFalse(any(row["id"] in {asset["id"], other["id"]} for row in self.repository.state()["assets"]))
        with Session(self.repository.engine) as session:
            self.assertIsNotNone(session.get(m.AssetIdentity, asset["id"]).deleted_at)
            self.assertIsNotNone(session.get(m.AssetIdentity, other["id"]).deleted_at)

    def family_plan(self, identifiers):
        response = self.client.post("/api/assets/family-deletion-plan", json={"asset_ids": identifiers})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def remove_family(self, plan, **kwargs):
        return self.client.post("/api/assets/family-delete", json={"asset_ids": plan["requested_asset_ids"],
            "fingerprint": plan["fingerprint"], "confirmed": True, "impact_confirmed": True, **kwargs})

    def test_family_enumerates_each_asset_files_once_per_plan(self):
        parent, rgb = self.add_asset("unit-index-parent", "person")
        children = [self.add_asset(f"unit-index-child-{index}", "person", path=rgb,
            reference={"parent_person_asset_id": parent["id"], "version": index + 2})[0] for index in range(6)]
        for index in range(10):
            self.add_asset(f"unit-index-unrelated-{index}")
        registry = self.app.state.production.registry
        with patch.object(registry, "_asset_files", wraps=registry._asset_files) as enumerate_files:
            plan = self.family_plan([children[-1]["id"]])
        calls = [call.args[0]["id"] for call in enumerate_files.call_args_list]
        expected = {row["id"] for row in self.repository.state()["assets"]
            if row["type"] != "source_candidate" and row["registry_status"] != "mock"}
        self.assertEqual(set(calls), expected)
        self.assertEqual(len(calls), len(expected))
        self.assertEqual(set(plan["expanded_asset_ids"]), {parent["id"], *(row["id"] for row in children)})
        self.assertEqual(plan["file_count"], 1)
        self.assertEqual(plan["retained_shared_bytes"], 0)
        self.assertTrue(rgb.exists())

    def test_family_cached_file_error_still_blocks_all_deletion(self):
        parent, path = self.add_asset("unit-cache-protected-parent", "person")
        child, _ = self.add_asset("unit-cache-protected-child", "person", path=path,
            reference={"parent_person_asset_id": parent["id"], "version": 2})
        manifest = self.settings.storage_root / "invalid-manifest.json"
        manifest.write_text("INVALID_UNIT_ONLY_JSON")
        other, control = self.add_asset("unit-cache-invalid-manifest", reference={"manifest_path": str(manifest)})
        plan = self.family_plan([child["id"]])
        self.assertFalse(plan["can_delete"])
        self.assertIn("REFERENCE_FILES_UNVERIFIED", plan["reasons"])
        self.assertTrue(any(row["id"] == other["id"] and row["reason"] == "REFERENCE_FILES_UNVERIFIED" for row in plan["references"]))
        self.assertEqual(self.remove_family(plan).status_code, 409)
        self.assertTrue(path.exists())
        self.assertTrue(control.exists())

    def test_family_includes_bound_manifest_directory_outputs_and_rechecks_new_files(self):
        fingerprint = "f" * 64
        directory = self.settings.storage_root / "projects" / "unit-project" / "tasks" / self.task["id"] / "compose" / "compositions" / fingerprint
        asset, video = self.add_asset("unit-bound-compose", "compose", path=directory / "Composite.mp4",
            reference={"manifest_path": str(directory / "manifest.json"), "input_fingerprint": fingerprint,
                "processing_run_id": "unit-bound-run"})
        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps({"schema": "factory.composition.v1", "task_id": self.task["id"],
            "processing_run_id": "unit-bound-run", "input_fingerprint": fingerprint, "output_sha256": asset["sha256"]}))
        outputs = [directory / "Composite.concat.txt", directory / "comfy" / "part_000000.api.json",
            directory / "comfy" / "part_000000.history.json", directory / "comfy" / "part_000000.mp4"]
        for path in outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"UNIT_ONLY_EXPLICIT_OUTPUT")
        shared, _ = self.add_asset("unit-shared-part-record", path=outputs[-1])
        unrelated, control = self.add_asset("unit-task-neighbour", path=directory.parent / "unrelated.unit")
        plan = self.family_plan([asset["id"]])
        self.assertEqual(set(plan["expanded_asset_ids"]), {asset["id"], shared["id"]})
        self.assertEqual({row["path"] for row in plan["files"]}, {str(path) for path in [video, manifest, *outputs]})
        self.assertNotIn(unrelated["id"], plan["expanded_asset_ids"])
        self.assertEqual(plan["retained_shared_bytes"], 0)
        late = directory / "comfy" / "late-worker-output.json"
        late.write_text("UNIT_ONLY_NEW_OUTPUT")
        self.assertEqual(self.remove_family(plan).json()["detail"]["code"], "DELETE_PLAN_CHANGED")
        self.assertTrue(all(path.exists() for path in [video, manifest, *outputs, late, control]))
        current = self.family_plan([asset["id"]])
        self.assertEqual(self.remove_family(current).status_code, 200)
        self.assertFalse(any(path.exists() for path in [video, manifest, *outputs, late]))
        self.assertTrue(control.exists())

    def test_family_unverified_manifest_identity_and_nested_symlink_never_delete(self):
        fingerprint = "a" * 64
        directory = self.settings.storage_root / "tasks" / self.task["id"] / "compose" / "compositions" / fingerprint
        asset, video = self.add_asset("unit-invalid-bound-compose", "compose", path=directory / "Composite.mp4",
            reference={"manifest_path": str(directory / "manifest.json"), "input_fingerprint": fingerprint})
        manifest = directory / "manifest.json"
        data = {"schema": "factory.composition.v1", "task_id": "unit-wrong-task", "input_fingerprint": fingerprint,
            "output_sha256": asset["sha256"]}
        manifest.write_text(json.dumps(data))
        blocked = self.family_plan([asset["id"]])
        self.assertFalse(blocked["can_delete"])
        self.assertIn("FILE_PLAN_UNVERIFIED", blocked["reasons"])
        self.assertEqual(self.remove_family(blocked).status_code, 409)
        control, control_path = self.add_asset("unit-outside-unverified-directory")
        self.assertTrue(self.family_plan([control["id"]])["can_delete"])
        self.assertTrue(control_path.exists())
        data["task_id"] = self.task["id"]
        manifest.write_text(json.dumps(data))
        external = self.root / "legacy-external.unit"
        external.write_bytes(b"UNIT_ONLY_PROTECTED_EXTERNAL")
        linked = directory / "comfy-link"
        linked.symlink_to(external)
        protected = self.family_plan([asset["id"]])
        self.assertIn("SYMLINK_FILE_PROTECTED", protected["reasons"])
        self.assertEqual(self.remove_family(protected).status_code, 409)
        self.assertTrue(video.exists())
        self.assertTrue(external.exists())

    def test_family_deletes_exact_version_dependency_shared_files_and_finished_history(self):
        person, rgb = self.add_asset("unit-family-person", "person", reference={"version": 1, "processing_run_id": "unit-finished-run"})
        child, _ = self.add_asset("unit-family-person-v2", "person", path=rgb,
            reference={"parent_person_asset_id": person["id"], "version": 2, "processing_run_id": "unit-finished-run"})
        background, image = self.add_asset("unit-family-bg")
        compose, video = self.add_asset("unit-family-compose", "compose", reference={"person_asset_id": child["id"], "background_asset_id": background["id"]})
        control, control_path = self.add_asset("unit-unrelated-control")
        self.repository.register_real_asset_links(background, self.task)
        self.repository.register_real_asset_links(compose, self.task)
        self.repository.mutate(lambda state: (
            state["runs"].append({"id": "unit-finished-run", "task_id": self.task["id"], "stage": "cutout", "status": "success",
                "progress": 100, "logs": [], "created_at": now(), "provider": "UNIT_TEST_ONLY"}),
            state["reviews"].append({"id": "unit-family-review", "task_id": self.task["id"], "asset_id": child["id"], "status": "approved", "issues": [], "note": "UNIT_TEST_ONLY"})))
        plan = self.family_plan([child["id"]])
        self.assertEqual(set(plan["expanded_asset_ids"]), {person["id"], child["id"], background["id"], compose["id"]})
        self.assertEqual(plan["file_count"], 3)
        self.assertEqual(plan["record_actions"]["reviews:DELETE"], 1)
        self.assertEqual(plan["record_actions"]["processing_runs:DELETE"], 1)
        self.assertNotIn("_snapshots", plan)
        response = self.remove_family(plan)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "DELETED")
        self.assertFalse(any(path.exists() for path in (rgb, image, video)))
        self.assertTrue(control_path.exists())
        self.assertEqual(self.repository.read_asset(control["id"])["id"], control["id"])
        with Session(self.repository.engine) as session:
            for identifier in plan["expanded_asset_ids"]:
                self.assertIsNone(session.get(m.VideoAsset, identifier))
                self.assertIsNone(session.get(m.AssetIdentity, identifier))
            self.assertIsNone(session.get(m.Review, "unit-family-review"))
            self.assertIsNone(session.get(m.ProcessingRun, "unit-finished-run"))
            self.assertIsNotNone(session.get(m.Task, self.task["id"]))
        self.assertEqual(self.repository.read_task(self.task["id"])["context"]["production"]["asset_family_deleted"]["status"], "INVALIDATED")

    def test_family_never_expands_by_only_hash_or_same_task_and_requires_confirmation(self):
        first, path = self.add_asset("unit-family-first")
        copy = self.settings.storage_root / "same-content-other-file.unit"
        copy.write_bytes(path.read_bytes())
        other, other_path = self.add_asset("unit-same-hash", path=copy)
        plan = self.family_plan([first["id"]])
        self.assertEqual(plan["expanded_asset_ids"], [first["id"]])
        self.assertEqual(self.remove_family(plan, confirmed=False).status_code, 422)
        self.assertEqual(self.remove_family(plan, impact_confirmed=False).status_code, 422)
        self.assertEqual(self.remove_family(plan).status_code, 200)
        self.assertTrue(other_path.exists())
        self.assertIsNotNone(self.repository.read_asset(other["id"]))

    def test_confirmed_family_expands_registered_external_source_and_owned_descendant(self):
        self.settings.legacy_tiktok_auto_root = self.root / "legacy"
        source, original = self.add_asset("unit-external-source", "source", path=self.root / "legacy" / "downloads" / "source.mp4")
        person, rgb = self.add_asset("unit-owned-person", "person", reference={"source_asset_id": source["id"], "version": 1})
        plan = self.family_plan([person["id"]])
        self.assertTrue(plan["can_delete"], plan)
        self.assertEqual(set(plan["expanded_asset_ids"]), {person["id"], source["id"]})
        file = next(row for row in plan["files"] if row["path"] == str(original))
        self.assertEqual(file["sha256"], source["sha256"])
        self.assertTrue(file["external_registered_media"])
        self.assertEqual(self.remove_family(plan, confirmed=False).status_code, 422)
        self.assertTrue(original.exists())
        self.assertEqual(self.remove_family(plan).status_code, 200)
        self.assertFalse(rgb.exists())
        self.assertFalse(original.exists())

    def test_family_external_media_hash_change_unapproved_root_and_symlink_are_blocked(self):
        self.settings.legacy_tiktok_auto_root = self.root / "legacy"
        source, original = self.add_asset("unit-external-source", "source", path=self.root / "legacy" / "downloads" / "source.mp4")
        original.write_bytes(b"MEDIA_REPLACED_AFTER_REGISTRATION")
        blocked = self.family_plan([source["id"]])
        self.assertIn("MEDIA_IDENTITY_MISMATCH", blocked["reasons"])
        self.assertEqual(self.remove_family(blocked).status_code, 409)
        self.assertTrue(original.exists())
        outside, path = self.add_asset("unit-unapproved-file", "source", path=self.root / "unrelated-project" / "source.mp4")
        self.assertIn("EXTERNAL_FILE_OWNERSHIP_UNVERIFIED", self.family_plan([outside["id"]])["reasons"])
        link = original.parent / "symlink.mp4"
        link.symlink_to(path)
        linked, _ = self.add_asset("unit-symbolic-source", "source", path=link)
        self.assertIn("SYMLINK_FILE_PROTECTED", self.family_plan([linked["id"]])["reasons"])
        self.assertTrue(path.exists())

    def test_family_external_aux_requires_exact_registered_hash_and_rejects_traversal(self):
        self.settings.legacy_tiktok_auto_root = self.root / "legacy"
        source, original = self.add_asset("unit-external-source", "source", path=self.root / "legacy" / "downloads" / "source.mp4")
        alpha = original.parent / "Alpha.mkv"
        alpha.write_bytes(b"UNIT_ALPHA_AUX")
        self.repository.mutate(lambda state: next(row for row in state["assets"] if row["id"] == source["id"])
            .update(legacy_reference={"alpha_path": str(alpha)}))
        blocked = self.family_plan([source["id"]])
        self.assertIn("EXTERNAL_FILE_OWNERSHIP_UNVERIFIED", blocked["reasons"])
        self.assertEqual(self.remove_family(blocked).status_code, 409)
        self.assertTrue(alpha.exists())
        self.repository.mutate(lambda state: next(row for row in state["assets"] if row["id"] == source["id"])
            ["legacy_reference"].update(alpha_sha256=file_hash(alpha)))
        allowed = self.family_plan([source["id"]])
        self.assertTrue(allowed["can_delete"], allowed)
        traversal = original.parent / ".." / "downloads" / "source.mp4"
        self.repository.mutate(lambda state: next(row for row in state["assets"] if row["id"] == source["id"])
            .update(external_path=str(traversal), storage_path=str(traversal)))
        self.assertIn("EXTERNAL_FILE_OWNERSHIP_UNVERIFIED", self.family_plan([source["id"]])["reasons"])
        self.assertTrue(original.exists())

    def test_family_stale_plan_and_active_run_preserve_every_member(self):
        asset, path = self.add_asset("unit-family-first")
        plan = self.family_plan([asset["id"]])
        self.add_asset("unit-new-child", reference={"parent_asset_id": asset["id"]})
        self.assertEqual(self.remove_family(plan).json()["detail"]["code"], "DELETE_PLAN_CHANGED")
        self.repository.mutate(lambda state: state["runs"].append({"id": "unit-family-running", "task_id": self.task["id"],
            "stage": "cutout", "status": "running", "progress": 10, "logs": [], "created_at": now(), "provider": "UNIT_TEST_ONLY"}))
        blocked = self.family_plan([asset["id"]])
        self.assertFalse(blocked["can_delete"])
        self.assertIn("RUNNING", blocked["reasons"])
        self.assertEqual(self.remove_family(blocked).status_code, 409)
        self.assertTrue(path.exists())
        self.assertIsNotNone(self.repository.read_asset("unit-new-child"))

    def test_family_shared_finished_run_is_detached_not_deleted_with_unselected_output(self):
        selected, _ = self.add_asset("unit-family-first", reference={"processing_run_id": "unit-shared-run", "version": 1})
        other, _ = self.add_asset("unit-unrelated-output", reference={"processing_run_id": "unit-shared-run", "version": 1})
        self.repository.mutate(lambda state: state["runs"].append({"id": "unit-shared-run", "task_id": self.task["id"],
            "stage": "cutout", "status": "success", "progress": 100, "logs": [], "created_at": now(), "provider": "UNIT_TEST_ONLY",
            "resource_info": {"outputs": [{"asset_id": selected["id"]}, {"asset_id": other["id"]}]}}))
        plan = self.family_plan([selected["id"]])
        self.assertEqual(plan["record_actions"]["processing_runs:DETACH"], 1)
        self.assertEqual(self.remove_family(plan).status_code, 200)
        run = self.repository.read_run("unit-shared-run")
        self.assertEqual(run["resource_info"]["outputs"], [{"asset_id": other["id"]}])

    def test_family_database_failure_restores_all_files_and_records(self):
        asset, path = self.add_asset("unit-family-first")
        plan = self.family_plan([asset["id"]])
        with patch("app.integrations.asset_registry.Session.commit", side_effect=RuntimeError("UNIT_DATABASE_FAILURE")):
            response = self.remove_family(plan)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(path.exists())
        self.assertIsNotNone(self.repository.read_asset(asset["id"]))

    def test_family_source_binding_requires_both_exact_path_and_hash(self):
        source, path = self.add_asset("unit-bound-source", "source")
        copy_path = self.settings.storage_root / "unit-unrelated-copy.unit"
        copy_path.write_bytes(path.read_bytes())
        unrelated, _ = self.add_asset("unit-same-content-other-source", "source", path=copy_path)
        person, rgb = self.add_asset("unit-bound-person", "person", reference={"source_path": str(path),
            "source_sha256": source["sha256"], "version": 1})
        plan = self.family_plan([source["id"]])
        self.assertEqual(set(plan["expanded_asset_ids"]), {source["id"], person["id"]})
        response = self.remove_family(plan)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(path.exists())
        self.assertFalse(rgb.exists())
        self.assertTrue(copy_path.exists())
        self.assertIsNotNone(self.repository.read_asset(unrelated["id"]))

    def test_family_source_candidate_cleans_only_owned_metadata(self):
        candidate, path = self.add_asset("unit-source-candidate", "source_candidate")
        analysis = self.settings.storage_root / "candidate-analysis.json"
        analysis.write_text('{"decision":"REJECTED"}')
        external = self.root / "unowned-analysis.json"
        external.write_text('{"keep":true}')
        self.repository.mutate(lambda state: next(row for row in state["assets"] if row["id"] == candidate["id"])
            .update(legacy_reference={"raw_result_path": str(path), "analysis_report": str(analysis), "version": 1}))
        plan = self.family_plan([candidate["id"]])
        self.assertEqual({row["path"] for row in plan["files"]}, {str(path), str(analysis)})
        self.assertEqual(self.remove_family(plan).status_code, 200)
        self.assertFalse(path.exists())
        self.assertFalse(analysis.exists())
        self.assertTrue(external.exists())

    def orphan_preview(self, source, suffix):
        person_id, run_id = "unit-deleted-person-" + suffix, "run-unit-orphan-" + suffix
        old = {**deepcopy(self.template), "id": person_id, "task_id": self.task["id"], "type": "person",
            "sha256": "1" * 64, "legacy_reference": {"source_audio_path": source["external_path"],
                "source_sha256": source["sha256"], "alpha_sha256": "2" * 64}}
        with Session(self.repository.engine) as session:
            session.add(m.AuditLog(id="audit-unit-orphan-" + suffix, task_id=self.task["id"], action="asset.family_delete",
                entity_id=person_id, message=json.dumps({"status": "DELETED", "assets": [old]}), created_at=m.utc_now()))
            session.commit()
        directory = self.settings.storage_root / "projects" / self.task["project_id"] / "tasks" / self.task["id"] / "compose" / run_id
        directory.mkdir(parents=True)
        preview = directory / "Preview.mp4"
        preview.write_bytes(b"UNIT_ORPHAN_PREVIEW_" + suffix.encode())
        manifest = {"schema": "factory.manual-composition.v1", "status": "READY", "task_id": self.task["id"],
            "processing_run_id": run_id, "person_asset_id": person_id, "rgb_sha256": old["sha256"],
            "alpha_sha256": old["legacy_reference"]["alpha_sha256"], "output_path": str(preview), "output_sha256": file_hash(preview)}
        (directory / "manifest.json").write_text(json.dumps(manifest))
        (directory / "render.log").write_text("UNIT_ORPHAN_RENDER_LOG")
        with Session(self.repository.engine) as session:
            session.add(m.ProcessingRun(id=run_id, task_id=self.task["id"], stage="compose", status="success", progress=100,
                logs=[], created_at=m.utc_now(), provider="UNIT_TEST_ONLY", stdout_log_path=str(directory / "render.log")))
            session.commit()
        return directory, preview

    def test_family_deleted_person_audit_expands_verified_orphan_preview_not_same_task(self):
        source, original = self.add_asset("unit-audited-source", "source")
        directory, preview = self.orphan_preview(source, "selected")
        other_path = self.settings.storage_root / "unit-same-hash-different-source.unit"
        other_path.write_bytes(original.read_bytes())
        other, _ = self.add_asset("unit-other-source", "source", path=other_path)
        unrelated, unrelated_preview = self.orphan_preview(other, "unrelated")
        (unrelated / "manifest.json").write_text("UNIT_UNRELATED_UNFINISHED_MANIFEST")
        control, control_file = self.add_asset("unit-unrelated-control")
        plan = self.family_plan([source["id"]])
        self.assertTrue(plan["can_delete"], plan)
        self.assertEqual(plan["expanded_asset_ids"], [source["id"]])
        self.assertEqual(len(plan["orphan_outputs"]), 1)
        self.assertEqual(plan["orphan_outputs"][0]["output_path"], str(preview))
        planned_files = {Path(row["path"]).resolve() for row in plan["files"]}
        self.assertIn(preview.resolve(), planned_files)
        self.assertIn((directory / "manifest.json").resolve(), planned_files)
        self.assertIn((directory / "render.log").resolve(), planned_files)
        self.assertNotIn(unrelated_preview.resolve(), planned_files)
        response = self.remove_family(plan)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(original.exists())
        self.assertFalse(preview.exists())
        self.assertFalse((directory / "manifest.json").exists())
        self.assertFalse((directory / "render.log").exists())
        self.assertTrue(unrelated_preview.exists())
        self.assertTrue((unrelated / "manifest.json").exists())
        self.assertTrue(control_file.exists())
        self.assertIsNotNone(self.repository.read_asset(other["id"]))
        self.assertIsNotNone(self.repository.read_asset(control["id"]))
        with Session(self.repository.engine) as session:
            self.assertIsNone(session.get(m.ProcessingRun, directory.name))
            self.assertIsNotNone(session.get(m.ProcessingRun, unrelated.name))

    def test_family_orphan_preview_changed_hash_or_symlink_blocks_whole_family(self):
        source, original = self.add_asset("unit-audited-source", "source")
        directory, preview = self.orphan_preview(source, "unsafe")
        content = preview.read_bytes()
        preview.write_bytes(b"UNIT_REPLACED_ORPHAN_OUTPUT")
        plan = self.family_plan([source["id"]])
        self.assertIn("ORPHAN_OUTPUT_IDENTITY_UNVERIFIED", plan["reasons"])
        self.assertEqual(self.remove_family(plan).status_code, 409)
        self.assertTrue(original.exists())
        preview.write_bytes(content)
        outside = self.root / "unowned-file.log"
        outside.write_text("KEEP_UNRELATED")
        (directory / "unsafe-link.log").symlink_to(outside)
        plan = self.family_plan([source["id"]])
        self.assertIn("SYMLINK_FILE_PROTECTED", plan["reasons"])
        self.assertEqual(self.remove_family(plan).status_code, 409)
        self.assertTrue(original.exists())
        self.assertTrue(preview.exists())
        self.assertTrue(outside.exists())

    def test_family_cleans_exact_repair_record_and_finished_run_cache_only(self):
        person, rgb = self.add_asset("unit-repair-person", "person")
        directory = self.settings.storage_root / "person" / "repairs" / "unit-repair-record"
        directory.mkdir(parents=True)
        cache = directory / "keyframe_000.png"
        cache.write_bytes(b"UNIT_REPAIR_CACHE")
        retained = self.settings.storage_root / "person" / "repairs" / "unrelated-repair"
        retained.mkdir()
        other_cache = retained / "keyframe_000.png"
        other_cache.write_bytes(b"UNRELATED_UNIT_CACHE")
        self.repository.mutate(lambda state: (
            next(row for row in state["tasks"] if row["id"] == self.task["id"])["context"].update(matte_repairs=[
                {"id": "unit-repair-record", "person_asset_id": person["id"], "run_id": "unit-repair-run", "directory": str(directory), "status": "PREVIEW_READY"},
                {"id": "other-repair-record", "person_asset_id": "other-asset", "directory": str(retained)}]),
            state["runs"].append({"id": "unit-repair-run", "task_id": self.task["id"], "stage": "person", "status": "success",
                "progress": 100, "logs": [], "created_at": now(), "provider": "UNIT_TEST_ONLY"})))
        plan = self.family_plan([person["id"]])
        self.assertEqual(plan["record_actions"]["task_context:DELETE"], 1)
        self.assertEqual(plan["record_actions"]["processing_runs:DELETE"], 1)
        self.assertIn(str(cache), [row["path"] for row in plan["files"]])
        self.assertEqual(self.remove_family(plan).status_code, 200)
        self.assertFalse(cache.exists())
        self.assertFalse(rgb.exists())
        self.assertTrue(other_cache.exists())
        self.assertEqual(self.repository.read_task(self.task["id"])["context"]["matte_repairs"][0]["id"], "other-repair-record")
        self.assertIsNone(self.repository.read_run("unit-repair-run"))

    def test_family_removes_exact_owned_source_video_fk_without_removing_task(self):
        source, path = self.add_asset("unit-factory-source", "source")
        with Session(self.repository.engine) as session:
            row = m.SourceVideo(id="unit-source-model", task_id=self.task["id"], original_filename=path.name,
                fps=30, width=1080, height=1920, duration=1, codec="UNIT", external_path=str(path), views=1, likes=1)
            session.add(row)
            session.flush()
            session.get(m.Task, self.task["id"]).source_video_id = row.id
            session.commit()
        plan = self.family_plan([source["id"]])
        self.assertEqual(plan["record_actions"]["source_videos:DELETE"], 1)
        response = self.remove_family(plan)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(path.exists())
        with Session(self.repository.engine) as session:
            self.assertIsNone(session.get(m.SourceVideo, "unit-source-model"))
            self.assertIsNone(session.get(m.Task, self.task["id"]).source_video_id)

    def test_family_quarantine_symlink_cannot_move_files_outside_factory(self):
        asset, path = self.add_asset("unit-family-first")
        outside = self.root / "outside-quarantine"
        outside.mkdir()
        (self.settings.storage_root / ".asset-delete").symlink_to(outside, target_is_directory=True)
        response = self.remove_family(self.family_plan([asset["id"]]))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "SYMLINK_FILE_PROTECTED")
        self.assertTrue(path.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_hardlinked_external_original_is_preserved_and_not_claimed_freed(self):
        original = self.root / "original.unit"
        original.write_bytes(b"KEEP_LEGACY_ORIGINAL")
        managed = self.settings.storage_root / "managed.unit"
        managed.hardlink_to(original)
        self.add_asset(path=managed)
        response = self.remove(self.plan())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(managed.exists())
        self.assertEqual(original.read_bytes(), b"KEEP_LEGACY_ORIGINAL")
        self.assertEqual(response.json()["deleted_allocated_bytes"], 0)

    def test_referenced_person_deletes_registry_without_rewriting_review_or_composition(self):
        person, path = self.add_asset("unit-person", "person")
        composite, _ = self.add_asset("unit-composition", "compose", reference={"person_asset_id": person["id"]})
        self.repository.register_real_asset_links(composite, self.task)
        self.repository.mutate(lambda state: state["reviews"].append({"id": "unit-rejected-review", "task_id": self.task["id"],
            "asset_id": person["id"], "status": "rejected", "issues": ["UNIT_REJECT"], "note": "Retain this decision"}))
        before = self.repository.read_reviews(self.task["id"])
        plan = self.plan(person["id"])
        self.assertTrue(plan["can_delete"])
        self.assertIn("REFERENCED", plan["warnings"])
        response = self.remove(plan, impact_confirmed=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(path.exists())
        self.assertIsNone(self.repository.read_asset(person["id"]))
        self.assertEqual(self.repository.read_reviews(self.task["id"]), before)
        with Session(self.repository.engine) as session:
            self.assertEqual(session.get(m.CompositionAsset, "composition-" + composite["id"]).person_asset_id, person["id"])
            self.assertEqual(session.get(m.Review, "unit-rejected-review").status, "rejected")
        self.repository.mutate(lambda state: state["settings"].update(output_fps=24))
        self.assertIsNone(self.repository.read_asset(person["id"]))

    def test_parent_rgb_shared_with_revision_survives_parent_record_deletion(self):
        person, rgb = self.add_asset("unit-parent", "person")
        alpha = self.settings.storage_root / "revision-alpha.mkv"
        alpha.write_bytes(b"UNIT_ALPHA")
        child, _ = self.add_asset("unit-revision", "person", path=rgb,
            reference={"parent_asset_id": person["id"], "alpha_path": str(alpha), "version": 2})
        response = self.remove(self.plan(person["id"]), impact_confirmed=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["released_bytes"], 0)
        self.assertTrue(rgb.exists())
        self.assertTrue(alpha.exists())
        self.assertEqual(self.repository.read_asset(child["id"])["status"], "READY")

    def test_symlinked_manifest_preview_inside_storage_is_rejected(self):
        manifest = self.settings.storage_root / "unit-preview" / "manifest.json"
        manifest.parent.mkdir()
        original = manifest.parent / "preserve.unit"
        original.write_bytes(b"KEEP_SHARED")
        (manifest.parent / "preview.mp4").symlink_to(original)
        manifest.write_text(json.dumps({"previews": [{"filename": "preview.mp4"}]}))
        _, path = self.add_asset(reference={"manifest_path": str(manifest)})
        plan = self.plan()
        self.assertIn("SYMLINK_FILE_PROTECTED", plan["reasons"])
        self.assertEqual(self.remove(plan, impact_confirmed=True).status_code, 409)
        self.assertTrue(original.exists())
        self.assertTrue(path.exists())

    def test_manifest_path_traversal_is_rejected(self):
        manifest = self.settings.storage_root / "unit-preview" / "manifest.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"previews": [{"filename": "../../outside.mp4"}]}))
        _, path = self.add_asset(reference={"manifest_path": str(manifest)})
        plan = self.plan()
        self.assertIn("FILE_PLAN_UNVERIFIED", plan["reasons"])
        self.assertEqual(self.remove(plan).status_code, 409)
        self.assertTrue(path.exists())

    def test_failed_file_staging_does_not_delete_record_and_restores_files(self):
        _, path = self.add_asset()
        plan = self.plan()
        with patch("app.integrations.asset_registry.os.replace", side_effect=PermissionError("UNIT_PERMISSION")):
            response = self.remove(plan)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["code"], "ASSET_DELETE_FAILED")
        self.assertTrue(path.exists())
        self.assertIsNotNone(self.repository.read_asset(plan["asset_id"]))
        failures = [row for row in self.repository.state()["audits"] if row["entity_id"] == plan["asset_id"] and row["action"] == "asset.delete_failed"]
        self.assertEqual(json.loads(failures[-1]["message"])["error"]["code"], "ASSET_DELETE_FAILED")

    def test_cleanup_failure_is_partial_and_audited_not_success(self):
        asset, path = self.add_asset()
        original = Path.unlink

        def fail_quarantined_media(candidate, *args, **kwargs):
            if candidate.name.isdigit() and ".asset-delete" in candidate.parts:
                raise PermissionError("UNIT_CLEANUP_DENIED")
            return original(candidate, *args, **kwargs)

        with patch("pathlib.Path.unlink", new=fail_quarantined_media):
            response = self.remove(self.plan())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "DELETED_WITH_CLEANUP_ERROR")
        self.assertEqual(response.json()["released_bytes"], 0)
        self.assertTrue(response.json()["cleanup_errors"])
        self.assertIn("UNIT_CLEANUP_DENIED", response.json()["cleanup_errors"][0]["error"])
        self.assertFalse(path.exists())
        self.assertIsNone(self.repository.read_asset(asset["id"]))
        audit = next(row for row in self.repository.state()["audits"] if row["entity_id"] == asset["id"])
        self.assertEqual(json.loads(audit["message"])["status"], "DELETED_WITH_CLEANUP_ERROR")

    def test_deleted_asset_identity_cannot_be_reintroduced_or_version_reused(self):
        asset, _ = self.add_asset(reference={"version": 7})
        self.assertEqual(self.remove(self.plan(), impact_confirmed=True).status_code, 200)
        self.assertEqual(self.repository.next_asset_version(self.task["id"], "background"), 8)
        with self.assertRaisesRegex(ValueError, "ASSET_ID_PREVIOUSLY_DELETED"):
            self.repository.mutate(lambda state: state["assets"].append(asset))
        self.assertIsNone(self.repository.read_asset(asset["id"]))

    def test_selected_plan_query_and_actual_storage_usage_update(self):
        asset, path = self.add_asset()
        other, _ = self.add_asset("unit-other")
        response = self.client.get("/api/assets/deletion-status", params={"asset_ids": asset["id"]})
        self.assertEqual([row["asset_id"] for row in response.json()], [asset["id"]])
        before = self.client.get("/api/assets/storage-usage").json()
        self.assertEqual(before["status"], "PASS")
        self.assertEqual(self.remove(self.plan(), impact_confirmed=True).status_code, 200)
        after = self.client.get("/api/assets/storage-usage").json()
        self.assertEqual(before["logical_bytes"] - after["logical_bytes"], asset["size"])
        self.assertEqual(before["file_count"] - after["file_count"], 1)
        self.assertFalse(path.exists())
        self.assertIsNotNone(self.repository.read_asset(other["id"]))

    def test_unregistered_process_writing_file_is_protected_without_killing_it(self):
        _, path = self.add_asset()
        output = subprocess.CompletedProcess([], 0, stdout="p31415\0f9\0aw\0n" + str(path) + "\0", stderr="")
        with patch("app.integrations.asset_registry.shutil.which", return_value="/usr/sbin/lsof"), \
                patch("app.integrations.asset_registry.subprocess.run", return_value=output):
            plan = self.plan()
            self.assertEqual(plan["reasons"], ["FILE_IN_USE"])
            self.assertIn({"type": "process", "id": "31415", "reason": "FILE_IN_USE"}, plan["references"])
            self.assertEqual(self.remove(plan, impact_confirmed=True).status_code, 409)
        self.assertTrue(path.exists())

    def test_large_file_lock_scan_has_bounded_command_and_matches_hardlink_writers(self):
        _, path = self.add_asset()
        other = self.root / "unregistered-hardlink.unit"
        other.hardlink_to(path)
        stat = other.stat()
        paths = [path] * 12000
        output = subprocess.CompletedProcess([], 0, stdout=("p123\0f7\0ar\0n" + str(path) + "\0"
            "p456\0f8\0au\0D" + hex(stat.st_dev) + "\0i" + str(stat.st_ino) + "\0n" + str(other) + "\0"
            "p789\0f9\0aw\0n" + str(self.root / "unrelated.unit") + "\0"), stderr="")
        with patch("app.integrations.asset_registry.shutil.which", return_value="/usr/sbin/lsof"), \
                patch("app.integrations.asset_registry.subprocess.run", return_value=output) as process:
            self.assertEqual(AssetRegistry._file_writers(paths), ["456"])
        command = process.call_args.args[0]
        self.assertEqual(command, ["/usr/sbin/lsof", "-nP", "-w", "-F0pafnDi"])
        self.assertNotIn(str(path), command)
        self.assertFalse(process.call_args.kwargs["shell"])
        self.assertEqual(process.call_args.kwargs["timeout"], 10)
        self.assertTrue(path.exists())
        self.assertTrue(other.exists())

    def test_file_lock_command_failure_or_timeout_blocks_entire_family(self):
        asset, path = self.add_asset("unit-lock-failure")
        for outcome in (subprocess.CompletedProcess([], 1, stdout="", stderr="UNIT_ONLY_LSOF_FAILURE"),
                subprocess.CompletedProcess([], 2, stdout="", stderr="UNIT_ONLY_LSOF_FAILURE"),
                subprocess.TimeoutExpired(["lsof"], 10)):
            with self.subTest(outcome=type(outcome).__name__), \
                    patch("app.integrations.asset_registry.shutil.which", return_value="/usr/sbin/lsof"), \
                    patch("app.integrations.asset_registry.subprocess.run",
                        **({"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome})):
                plan = self.family_plan([asset["id"]])
                self.assertFalse(plan["can_delete"])
                self.assertIn("FILE_LOCK_CHECK_FAILED", plan["reasons"])
                self.assertEqual(self.remove_family(plan).status_code, 409)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
