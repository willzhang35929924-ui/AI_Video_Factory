"""Selection/lineage contracts with isolated byte fixtures, not production media."""

import copy
import json
import unittest
from pathlib import Path

from app.integrations.asset_registry import file_hash
from test_real_review import ReviewFixture


class RepairedPersonSelectionTests(ReviewFixture, unittest.TestCase):
    def setUp(self):
        self.prepare("person")
        self.parent = self.verified_person()
        self.root = self.production.settings.storage_root
        manifest_path = Path(self.parent["legacy_reference"]["manifest_path"])
        manifest = json.loads(manifest_path.read_text())
        manifest.update(status="READY", context_id="unit-context", alpha_mode="straight", execution_scope="PRODUCTION")
        manifest_path.write_text(json.dumps(manifest))
        def save(state):
            state["assets"][0]["legacy_reference"].update(alpha_mode="straight", manifest_sha256=file_hash(manifest_path), execution_scope="PRODUCTION")
            state["reviews"] = []
        self.repository.mutate(save)

    def approve_parent(self):
        def save(state):
            state["assets"][0]["status"] = "approved"
            state["reviews"].append({"id": "review-20261008010000000000-human", "task_id": "unit-task",
                "asset_id": "unit-asset-v1", "status": "approved"})
            state["audits"].append({"id": "unit-human-audit", "task_id": "unit-task",
                "action": "review.approve", "entity_id": "review-20261008010000000000-human"})
        self.repository.mutate(save)

    def child(self, parent_id="unit-asset-v1", suffix="2"):
        parent = next(row for row in self.repository.state()["assets"] if row["id"] == parent_id)
        directory = self.root / ("repair-" + suffix)
        directory.mkdir()
        alpha, manifest_path = directory / "Alpha.mkv", directory / "manifest.json"
        alpha.write_bytes(b"isolated alpha revision fixture " + suffix.encode())
        run_id, repair_id, asset_id = "unit-repair-run-" + suffix, "unit-repair-" + suffix, "unit-person-" + suffix
        manifest = json.loads(Path(parent["legacy_reference"]["manifest_path"]).read_text())
        manifest.update(schema="factory.foreground.repair.v1", status="READY", processing_run_id=run_id,
            repair_id=repair_id, alpha_path=str(alpha), alpha_sha256=file_hash(alpha),
            parent_rgb_path=parent["storage_path"], parent_alpha_path=parent["legacy_reference"]["alpha_path"],
            parent_alpha_sha256=parent["legacy_reference"]["alpha_sha256"])
        manifest_path.write_text(json.dumps(manifest))
        child = {**copy.deepcopy(parent), "id": asset_id, "status": "READY", "legacy_reference": {
            **parent["legacy_reference"], "factory_fingerprint": "unit-local-alpha-edit-" + suffix,
            "parent_factory_fingerprint": parent["legacy_reference"].get("parent_factory_fingerprint", parent["legacy_reference"]["factory_fingerprint"]),
            "parent_person_asset_id": parent_id, "repair_id": repair_id, "processing_run_id": run_id,
            "alpha_path": str(alpha), "alpha_sha256": file_hash(alpha), "manifest_path": str(manifest_path),
            "manifest_sha256": file_hash(manifest_path), "version": int(suffix)}}
        record = {"id": repair_id, "task_id": "unit-task", "status": "APPLIED", "person_asset_id": parent_id,
            "asset_id": asset_id, "run_id": run_id, "parent_rgb_sha256": parent["sha256"],
            "parent_alpha_sha256": parent["legacy_reference"]["alpha_sha256"], "execution_scope": "PRODUCTION",
            "applied_at": "2026-10-08T02:00:00+00:00"}
        def save(state):
            state["assets"].append(child)
            state["tasks"][0]["context"].setdefault("matte_repairs", []).append(record)
            state["tasks"][0]["context"]["production"].update(current_person_asset_id=asset_id, review_approved=False)
            state["runs"].append({"id": run_id, "task_id": "unit-task", "stage": "person",
                "provider": "FactoryMatteRepair", "status": "success", "pid": 54321, "exit_code": 0})
            state["reviews"].append({"id": "rev-20261008020000000000-child-" + suffix,
                "task_id": "unit-task", "asset_id": asset_id, "status": "pending"})
            state["audits"].append({"id": "unit-apply-audit-" + suffix, "task_id": "unit-task",
                "action": "repair.applied", "entity_id": repair_id})
        self.repository.mutate(save)
        return child

    def select(self):
        return self.production._asset("unit-task", "person")

    def test_old_generation_applied_child_reaches_quality_without_inheriting_approval(self):
        self.approve_parent()
        child = self.child()
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(cutout_generation=2))
        before = self.repository.state()
        self.assertEqual(self.select()["id"], child["id"])
        self.assertFalse(self.production._person_review_approved("unit-task", child))
        self.assertEqual(self.repository.state(), before)

    def test_current_generation_parent_allows_applied_child_without_human_approval(self):
        child = self.child()
        self.assertEqual(self.select()["id"], child["id"])
        self.assertFalse(self.production._person_review_approved("unit-task", child))

    def test_current_approved_repaired_mother_remains_usable_after_generation_change(self):
        child = self.child()
        def approved(state):
            next(row for row in state["assets"] if row["id"] == child["id"])["status"] = "approved"
            state["assets"][0]["status"] = "rejected"
            state["tasks"][0]["context"]["production"]["cutout_generation"] = 2
            state["reviews"][-1]["status"] = "approved"
            state["audits"].append({"id": "unit-approved-child-audit", "task_id": "unit-task",
                "action": "review.approve", "entity_id": state["reviews"][-1]["id"]})
        self.repository.mutate(approved)
        self.assertEqual(self.select()["id"], child["id"])

    def test_multi_revision_chain_is_traced_without_recutout_or_mutation(self):
        self.approve_parent()
        second = self.child()
        third = self.child(second["id"], "3")
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(cutout_generation=2))
        self.assertEqual(self.select()["id"], third["id"])

    def test_missing_apply_audit_or_success_run_cannot_adopt_child(self):
        for failure in ("audit", "record", "run", "pid", "exit_code"):
            with self.subTest(failure=failure):
                self.setUp()
                self.approve_parent()
                self.child()
                def corrupt(state):
                    if failure == "audit":
                        state["audits"] = [row for row in state["audits"] if row["action"] != "repair.applied"]
                    elif failure == "record":
                        state["tasks"][0]["context"]["matte_repairs"] = []
                    else:
                        state["runs"][0].update(**{"status": "failed"} if failure == "run" else {failure: None if failure == "pid" else 9})
                self.repository.mutate(corrupt)
                self.assertIsNone(self.select())

    def test_wrong_parent_identity_or_technical_scope_is_not_adopted(self):
        for failure in ("parent_rgb", "parent_alpha", "child_alpha", "parent_scope", "child_scope", "parent_task", "manifest_parent"):
            with self.subTest(failure=failure):
                self.setUp()
                self.approve_parent()
                child = self.child()
                def corrupt(state):
                    record = state["tasks"][0]["context"]["matte_repairs"][0]
                    if failure in {"parent_rgb", "parent_alpha"}:
                        record[failure + "_sha256"] = "wrong-parent"
                    elif failure == "child_alpha":
                        Path(child["legacy_reference"]["alpha_path"]).write_bytes(b"tampered alpha")
                    elif failure in {"parent_scope", "child_scope"}:
                        next(row for row in state["assets"] if row["id"] == ("unit-asset-v1" if failure == "parent_scope" else child["id"]))["legacy_reference"]["execution_scope"] = "TECHNICAL_VALIDATION"
                    elif failure == "parent_task":
                        state["assets"][0]["task_id"] = "foreign-task"
                    else:
                        path = Path(child["legacy_reference"]["manifest_path"])
                        manifest = json.loads(path.read_text())
                        manifest["parent_alpha_sha256"] = "wrong-parent"
                        path.write_text(json.dumps(manifest))
                        next(row for row in state["assets"] if row["id"] == child["id"])["legacy_reference"]["manifest_sha256"] = file_hash(path)
                self.repository.mutate(corrupt)
                self.assertIsNone(self.select())

    def test_child_latest_rejected_review_blocks_even_ready_asset(self):
        self.approve_parent()
        self.child()
        self.repository.mutate(lambda state: state["reviews"][-1].update(status="rejected"))
        self.assertIsNone(self.select())

    def test_old_parent_approval_requires_genuine_audit_and_latest_decision(self):
        for failure in ("human_audit", "pending", "rejected"):
            with self.subTest(failure=failure):
                self.setUp()
                self.approve_parent()
                self.child()
                def corrupt(state):
                    state["tasks"][0]["context"]["production"]["cutout_generation"] = 2
                    if failure == "human_audit":
                        state["audits"] = [row for row in state["audits"] if row["action"] != "review.approve"]
                    else:
                        state["reviews"].append({"id": "rev-20261008030000000000-parent-latest", "task_id": "unit-task",
                            "asset_id": "unit-asset-v1", "status": failure})
                self.repository.mutate(corrupt)
                self.assertIsNone(self.select())

    def test_parent_cycle_is_not_adopted_despite_old_human_approval(self):
        self.approve_parent()
        child = self.child()
        self.repository.mutate(lambda state: state["assets"][0]["legacy_reference"].update(parent_person_asset_id=child["id"]))
        self.assertIsNone(self.select())

    def test_undone_child_requires_explicit_newer_restore_audit(self):
        child = self.child()
        def undone(state):
            state["tasks"][0]["context"]["matte_repairs"][0].update(status="UNDONE", undone_at="2026-10-08T03:00:00+00:00")
        self.repository.mutate(undone)
        self.assertIsNone(self.select())
        self.repository.mutate(lambda state: state["audits"].append({"id": "unit-restore-audit", "task_id": "unit-task",
            "action": "person.version.restore", "entity_id": child["id"], "created_at": "2026-10-08T03:00:01Z"}))
        self.assertEqual(self.select()["id"], child["id"])
        self.assertFalse(self.production._person_review_approved("unit-task", child))


if __name__ == "__main__":
    unittest.main()
