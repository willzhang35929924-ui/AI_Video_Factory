import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.domain import find
from app.integrations.asset_registry import file_hash
from app.integrations.production_service import ProductionService
from test_real_review import MemoryRepository


class FirstCompositionGateTests(unittest.IsolatedAsyncioTestCase):
    """Isolated orchestration fixtures, never media production or paid providers."""
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        task = {"id": "unit-task", "project_id": "unit-project", "status": "CREATED", "current_stage": "collector",
            "context": {"production": {"previous_status": "CREATED"}, "task_context": {"context_id": "unit-context"}},
            "collector_result": {"decision": "accepted", "video_id": "unit-video", "video_url": "https://unit.invalid/video",
                "context_id": "unit-context", "legacy_vidbee_task_id": "unit-download"}}
        prompts = [{"id": f"unit-prompt-{index:02d}", "task_id": task["id"], "ordinal": index, "status": "READY",
            "prompt": f"unit scene {index}", "negative_prompt": "unit negative", "scene_type": "isolated"} for index in range(1, 21)]
        self.repository = MemoryRepository({"tasks": [task], "assets": [], "prompts": prompts, "reviews": [], "runs": [],
            "quality_assessments": [], "errors": [], "settings": {"production_policy": {"person_review_required": True}},
            "stages": [{"id": "unit-review-stage", "task_id": task["id"], "name": "review", "status": "pending"}]})
        self.service = ProductionService.__new__(ProductionService)
        self.service.jobs = {}
        self.service.cancels = {"unit-batch": threading.Event()}
        self.service.closing = False
        self.service.repository = self.repository
        self.service.settings = SimpleNamespace(storage_root=self.root)
        self.service.registry = SimpleNamespace(allowed_path=lambda value: Path(value).resolve(), content=self.content)
        self.service.collector = SimpleNamespace(context=lambda _: {"task_id": "unit-task", "context_id": "unit-context"})
        self.service.vidbee = SimpleNamespace(verify=AsyncMock())
        self.service._checkpoint = AsyncMock()
        self.service.event = AsyncMock()
        self.service._stage = AsyncMock(return_value={"status": "READY"})
        self.service._stage_status = lambda *_: None
        self.service._asset = self.asset
        self.service._video_stage = self.video_stage
        self.service._quality = self.quality
        self.calls, self.failure = [], None
        self.source = self.make_asset("source", "unit-source")
        self.person = self.make_asset("person", "unit-person")
        alpha = self.root / "unit-alpha.mkv"
        alpha.write_bytes(b"isolated alpha fixture; not production media")
        mother = self.root / "unit-mother-manifest.json"
        mother.write_text(json.dumps({"test_fixture": True}))
        self.person["probe_metadata"] = {"frame_count": 3}
        self.person["legacy_reference"].update(alpha_path=str(alpha), alpha_sha256=file_hash(alpha), manifest_path=str(mother),
            manifest_sha256=file_hash(mother), source_sha256=self.source["sha256"])
        self.repository.mutate(lambda state: find(state, "assets", self.person["id"]).update(copy.deepcopy(self.person)))
        await self.quality("unit-batch", "unit-task", self.person)
        self.repository.mutate(lambda state: next(row for row in state["reviews"] if row["asset_id"] == self.person["id"]).update(status="approved"))
        self.calls.clear()

    def content(self, identifier):
        return Path(find(self.repository.state(), "assets", identifier)["storage_path"]), "video/mp4"

    def make_asset(self, kind, identifier, prompt_id=None):
        path = self.root / (identifier + ".media")
        path.write_bytes(("isolated fixture " + identifier).encode())
        asset = {"id": identifier, "task_id": "unit-task", "type": kind, "registry_status": "registered", "status": "READY",
            "storage_path": str(path), "external_path": str(path), "sha256": file_hash(path),
            "legacy_reference": {"prompt_id": prompt_id, "processing_run_id": "unit-" + identifier + "-run"}}
        self.repository.mutate(lambda state: state["assets"].append(copy.deepcopy(asset)))
        return asset

    def asset(self, task_id, kind, prompt_id=None, **_):
        return next((row for row in self.repository.state()["assets"] if row["task_id"] == task_id and row["type"] == kind
                     and row["legacy_reference"].get("prompt_id") == prompt_id), None)

    async def video_stage(self, batch_id, task_id, stage, context, prompt=None):
        ordinal = prompt["ordinal"] if prompt else None
        self.calls.append((stage, ordinal))
        if self.failure and self.failure[:2] == (stage, ordinal):
            return {"status": self.failure[2], "error_code": self.failure[3]}
        if stage == "background":
            self.make_asset("background", f"unit-bg-{ordinal:02d}", prompt["id"])
        elif stage == "compose":
            composition = self.make_asset("compose", f"unit-compose-{ordinal:02d}", prompt["id"])
            manifest_path = self.root / f"unit-compose-{ordinal:02d}-manifest.json"
            manifest = {"schema": "factory.composition.v1", "status": "READY", "full_decode_verified": True,
                "cutout_inference_executions": 0, "task_id": task_id, "context_id": "unit-context",
                "person_asset_id": self.person["id"], "background_asset_id": context["background_asset"]["id"],
                "rgb_sha256": self.person["sha256"], "alpha_sha256": self.person["legacy_reference"]["alpha_sha256"],
                "source_sha256": self.source["sha256"], "background_sha256": context["background_asset"]["sha256"],
                "output_sha256": composition["sha256"], "source_media": {"frame_count": 3}, "output_media": {"frame_count": 3}}
            manifest_path.write_text(json.dumps(manifest))
            self.repository.mutate(lambda state: find(state, "assets", composition["id"])["legacy_reference"].update(manifest_path=str(manifest_path)))
        return {"status": "READY"}

    async def quality(self, batch_id, task_id, person, composition=None):
        ordinal = int(composition["id"].rsplit("-", 1)[1]) if composition else None
        self.calls.append(("quality", ordinal))
        status = self.failure[2] if self.failure and self.failure[:2] == ("quality", ordinal) else "PASS"
        index = len(self.repository.state()["quality_assessments"])
        assessment = {"id": "unit-quality-" + str(index), "task_id": task_id, "asset_id": person["id"], "status": status,
            "fingerprint": "unit-alpha-quality-fingerprint", "processing_run_id": "unit-quality-run-" + str(index)}
        report = {"status": status, "fingerprint": assessment["fingerprint"], "input_sha256": person["legacy_reference"]["alpha_sha256"],
            "mode": "REAL", "provider": "OpenCVCutoutQualityAnalyzer"}
        path = self.root / (assessment["id"] + ".json")
        path.write_text(json.dumps(report))
        def change(state):
            state["quality_assessments"].append(assessment)
            state["runs"].append({"id": assessment["processing_run_id"], "task_id": task_id, "stage": "quality",
                "provider": "OpenCVCutoutQualityAnalyzer", "status": "success" if status == "PASS" else "failed"})
            find(state, "tasks", task_id)["context"]["production"].setdefault("results", {})["quality"] = {
                **report, "run_id": assessment["processing_run_id"], "report_path": str(path)}
            reviewed_asset = composition or person
            if not any(row["asset_id"] == reviewed_asset["id"] for row in state["reviews"]):
                state["reviews"].append({"task_id": task_id, "asset_id": reviewed_asset["id"], "status": "pending"})
        self.repository.mutate(change)
        return assessment

    async def run_task(self):
        await self.service._task("unit-batch", "unit-task")

    def production(self):
        return self.repository.state()["tasks"][0]["context"]["production"]

    def approve_first(self):
        self.repository.mutate(lambda state: next(row for row in state["reviews"] if row["asset_id"] == "unit-compose-01").update(status="approved"))

    async def test_unapproved_person_holds_full_pipeline_before_paid_stages(self):
        self.repository.mutate(lambda state: next(row for row in state["reviews"] if row["asset_id"] == self.person["id"]).update(status="pending"))
        await self.run_task()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "WAITING_REVIEW")
        self.assertEqual(len(self.repository.state()["quality_assessments"]), 1)
        self.assertFalse(any(row["type"] in {"background", "compose"} for row in self.repository.state()["assets"]))

    async def test_person_approval_continues_using_same_mother_and_quality(self):
        self.repository.mutate(lambda state: next(row for row in state["reviews"] if row["asset_id"] == self.person["id"]).update(status="pending"))
        await self.run_task()
        self.repository.mutate(lambda state: next(row for row in state["reviews"] if row["asset_id"] == self.person["id"]).update(status="approved"))
        await self.run_task()
        self.assertNotIn(("cutout", None), self.calls)
        self.assertNotIn(("quality", None), self.calls)
        self.assertEqual(self.production()["first_composition"]["person_asset_id"], self.person["id"])
        self.assertEqual([ordinal for stage, ordinal in self.calls if stage == "background"], [1])

    async def test_missing_person_quality_is_analyzed_before_human_gate(self):
        self.repository.mutate(lambda state: state.update(quality_assessments=[], reviews=[]))
        await self.run_task()
        self.assertEqual(self.calls, [("quality", None)])
        self.assertEqual(self.repository.state()["reviews"][0]["asset_id"], self.person["id"])
        self.assertEqual(self.repository.state()["reviews"][0]["status"], "pending")

    async def test_person_geometry_fail_does_not_replace_human_approval(self):
        self.failure = ("quality", None, "FAIL", "UNIT_GEOMETRY_FAIL")
        self.repository.mutate(lambda state: state.update(quality_assessments=[]))
        await self.run_task()
        self.assertIn(("background", 1), self.calls)
        self.assertNotIn(("prompt", None), self.calls)
        self.assertEqual(self.repository.state()["quality_assessments"][0]["status"], "FAIL")

    async def test_rejected_person_and_changed_alpha_cannot_release_paid_stages(self):
        self.repository.mutate(lambda state: next(row for row in state["reviews"] if row["asset_id"] == self.person["id"]).update(status="rejected"))
        await self.run_task()
        self.assertEqual(self.calls, [])
        self.repository.mutate(lambda state: next(row for row in state["reviews"] if row["asset_id"] == self.person["id"]).update(status="approved"))
        Path(self.person["legacy_reference"]["alpha_path"]).write_bytes(b"changed alpha")
        await self.run_task()
        self.assertNotIn(("prompt", None), self.calls)

    async def test_first_background_failure_or_unknown_never_creates_other_nineteen(self):
        for status, code in (("FAILED", "BACKGROUND_FAILED"), ("UNKNOWN", "UNKNOWN_EXTERNAL_REQUEST")):
            with self.subTest(status=status):
                self.calls.clear()
                self.failure = ("background", 1, status, code)
                await self.run_task()
                self.assertEqual(self.calls, [("background", 1)])
                self.assertEqual(self.repository.state()["tasks"][0]["status"], "WAITING_REVIEW")
                self.assertEqual(self.production()["first_composition"]["error_code"], code)

    async def test_first_composition_failure_keeps_first_background_and_stops_expansion(self):
        self.failure = ("compose", 1, "FAILED", "COMFYUI_FAILED")
        await self.run_task()
        self.assertEqual(self.calls, [("background", 1), ("compose", 1)])
        self.assertIsNotNone(self.asset("unit-task", "background", "unit-prompt-01"))
        self.assertEqual(self.production()["first_composition"]["status"], "BLOCKED")

    async def test_first_gate_hold_continues_next_task_in_serial_queue(self):
        next_task = {**copy.deepcopy(self.repository.state()["tasks"][0]), "id": "unit-next-task"}
        def change(state):
            state["tasks"].append(next_task)
            state["batches"] = [{"id": "unit-batch", "status": "QUEUED", "task_ids": ["unit-task", "unit-next-task"],
                "items": [{"task_id": identifier, "status": "QUEUED"} for identifier in ("unit-task", "unit-next-task")]}]
        self.repository.mutate(change)
        self.service.cancels, self.service.closing = {"unit-batch": threading.Event()}, False
        self.service.event = AsyncMock()
        self.service.summary = lambda _: {"unit_queue_verified": True}
        original = self.service._task
        visited = []
        async def execute(batch_id, task_id):
            visited.append(task_id)
            if task_id == "unit-task":
                await original(batch_id, task_id)
            else:
                self.service._task_status(task_id, "REJECTED")
        self.service._task = execute
        self.failure = ("background", 1, "UNKNOWN", "UNKNOWN_EXTERNAL_REQUEST")
        await self.service.run("unit-batch")
        self.assertEqual(visited, ["unit-task", "unit-next-task"])
        batch = self.repository.state()["batches"][0]
        self.assertEqual([row["status"] for row in batch["items"]], ["WAITING_REVIEW", "REJECTED"])
        self.assertEqual(batch["status"], "COMPLETED_WITH_ERRORS")
        self.assertNotIn(("background", 2), self.calls)

    async def test_first_quality_fail_or_unknown_holds_rest_and_preserves_media(self):
        for status in ("FAIL", "UNKNOWN"):
            with self.subTest(status=status):
                self.calls.clear()
                self.failure = ("quality", 1, status, "UNIT_QUALITY_ERROR")
                await self.run_task()
                self.assertNotIn(("background", 2), self.calls)
                if status == "FAIL":
                    self.assertEqual(self.production()["first_composition"]["geometry_status"], "FAIL")
                    self.assertEqual(self.production()["first_composition"]["status"], "VERIFIED")
                    self.assertEqual(self.production()["first_composition"]["human_review"], "PENDING")
                else:
                    self.assertEqual(self.production()["first_composition"]["stage"], "quality")
                self.assertIsNotNone(self.asset("unit-task", "compose", "unit-prompt-01"))
                self.assertEqual(self.repository.state()["tasks"][0]["status"], "WAITING_REVIEW")

    async def test_verified_first_waits_for_human_before_other_nineteen(self):
        await self.run_task()
        self.assertEqual([ordinal for stage, ordinal in self.calls if stage == "background"], [1])
        checkpoint = self.production()["first_composition"]
        self.assertEqual(checkpoint["status"], "PASS")
        self.assertEqual(checkpoint["human_review"], "PENDING")
        self.assertEqual(checkpoint["person_asset_id"], self.person["id"])
        self.assertEqual(checkpoint["background_asset_id"], "unit-bg-01")
        self.assertEqual(checkpoint["composition_asset_id"], "unit-compose-01")
        self.assertTrue(checkpoint["full_decode_verified"])
        self.assertEqual(len(self.production()["first_composition_checkpoints"]), 1)

    async def test_human_approved_first_expands_twenty_using_same_person(self):
        await self.run_task()
        self.approve_first()
        await self.run_task()
        self.assertEqual([ordinal for stage, ordinal in self.calls if stage == "background"], list(range(1, 21)))
        self.assertEqual(self.production()["first_composition"]["person_asset_id"], self.person["id"])

    async def test_approved_first_does_not_accept_changed_rgb_or_alpha(self):
        await self.run_task()
        self.approve_first()
        self.assertTrue(self.service._first_review_approved("unit-task", self.person))
        alpha = Path(self.person["legacy_reference"]["alpha_path"])
        original = alpha.read_bytes()
        alpha.write_bytes(b"changed alpha after approval")
        self.assertFalse(self.service._first_review_approved("unit-task", self.person))
        alpha.write_bytes(original)
        Path(self.person["storage_path"]).write_bytes(b"changed rgb after approval")
        self.assertFalse(self.service._first_review_approved("unit-task", self.person))

    async def test_repeated_same_assets_do_not_generate_first_image_or_compose_again(self):
        await self.run_task()
        original = copy.deepcopy(self.production()["first_composition_checkpoints"])
        self.calls.clear()
        await self.run_task()
        self.assertEqual([row for row in self.calls if row[0] in {"background", "compose"}], [])
        self.assertEqual(self.production()["first_composition_checkpoints"], original)
        self.assertEqual(len(self.repository.state()["assets"]), 4)

    async def test_paid_unknown_after_first_pass_freezes_remaining_backgrounds(self):
        self.failure = ("background", 2, "UNKNOWN", "UNKNOWN_EXTERNAL_REQUEST")
        await self.run_task()
        self.approve_first()
        await self.run_task()
        self.assertEqual([ordinal for stage, ordinal in self.calls if stage == "background"], [1, 2])
        self.assertEqual(self.production()["first_composition"]["status"], "BLOCKED")
        self.assertEqual(self.production()["first_composition_checkpoints"][0]["status"], "PASS")

    async def test_missing_or_twenty_first_prompt_cannot_spend_background_request(self):
        state = self.repository.state()
        for prompts in (state["prompts"][:-1], [*state["prompts"], {**state["prompts"][0], "id": "unit-prompt-21", "ordinal": 21}]):
            with self.subTest(count=len(prompts)):
                self.calls.clear()
                self.repository.mutate(lambda current: current.update(prompts=prompts))
                await self.run_task()
                self.assertEqual(self.calls, [("prompt", None)])
                self.assertEqual(self.repository.state()["errors"][-1]["code"], "PROMPT_SET_INVALID")

    async def test_changed_media_or_false_decode_flag_cannot_expand(self):
        self.failure = ("background", 2, "FAILED", "UNIT_FAILURE")
        await self.run_task()
        composition = self.asset("unit-task", "compose", "unit-prompt-01")
        manifest_path = Path(composition["legacy_reference"]["manifest_path"])
        manifest = json.loads(manifest_path.read_text())
        manifest["full_decode_verified"] = False
        manifest_path.write_text(json.dumps(manifest))
        self.calls.clear()
        await self.run_task()
        self.assertNotIn(("background", 2), self.calls)
        self.assertEqual(self.production()["first_composition"]["error_code"], "FIRST_COMPOSITION_NOT_VERIFIED")
        self.assertEqual(len(self.production()["first_composition_checkpoints"]), 1)

    async def test_checkpoint_rejects_changed_hash_and_mismatched_alpha_report(self):
        await self.run_task()
        background = self.asset("unit-task", "background", "unit-prompt-01")
        composition = self.asset("unit-task", "compose", "unit-prompt-01")
        quality = self.repository.state()["quality_assessments"][-1]
        report = self.production()["results"]["quality"]
        self.repository.mutate(lambda state: find(state, "tasks", "unit-task")["context"]["production"]["results"]["quality"].update(input_sha256="wrong-alpha"))
        with self.assertRaisesRegex(ValueError, "QUALITY_EVIDENCE_MISMATCH"):
            self.service._first_composition_checkpoint("unit-task", self.source, self.person, background, composition, quality)
        self.repository.mutate(lambda state: find(state, "tasks", "unit-task")["context"]["production"]["results"].update(quality=report))
        Path(background["storage_path"]).write_bytes(b"changed same-scope background")
        with self.assertRaisesRegex(ValueError, "ASSET_HASH_MISMATCH"):
            self.service._first_composition_checkpoint("unit-task", self.source, self.person, background, composition, quality)


if __name__ == "__main__":
    unittest.main()
