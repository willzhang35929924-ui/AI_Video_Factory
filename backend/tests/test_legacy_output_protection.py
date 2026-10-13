import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from app.integrations.legacy.collector_worker import LegacyOutputGuard, LegacyOutputProtected, execute


class LegacyOutputProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.legacy = self.root / "legacy"
        self.factory = self.root / "factory"
        self.factory.mkdir()
        for folder in ("downloads", "output", "data", "logs"):
            (self.legacy / folder).mkdir(parents=True)
        self.guard = LegacyOutputGuard({}, self.legacy)

    def test_existing_metadata_cannot_be_overwritten(self):
        path = self.legacy / "downloads" / "source.json"
        path.write_text("original metadata")
        with self.guard:
            with self.assertRaises(LegacyOutputProtected):
                path.write_text("changed metadata")
        self.assertEqual(path.read_text(), "original metadata")
        self.assertEqual(self.guard.blocked_operations[-1]["operation"], "open")

    def test_legacy_source_aliases_and_sample_frames_cannot_be_removed_or_moved(self):
        for directory in ("downloads", "data", "logs", "output"):
            path = self.legacy / directory / "original.bin"
            path.write_bytes(b"original")
            with self.guard:
                with self.assertRaises(LegacyOutputProtected):
                    path.unlink()
                with self.assertRaises(LegacyOutputProtected):
                    path.rename(self.factory / "moved.bin")
            self.assertEqual(path.read_bytes(), b"original")

    def test_existing_sample_hardlink_and_sidecar_survive_archive_cleanup(self):
        original = self.legacy / "downloads" / "source.mp4"
        original.write_bytes(b"video fixture")
        alias = self.legacy / "downloads" / "unclassified.mp4"
        os.link(original, alias)
        sidecar = alias.with_suffix(".archive.json")
        sidecar.write_text("original archive")
        with self.guard:
            with self.assertRaises(LegacyOutputProtected):
                alias.unlink()
            with self.assertRaises(LegacyOutputProtected):
                sidecar.unlink()
        self.assertTrue(os.path.samefile(original, alias))
        self.assertEqual(sidecar.read_text(), "original archive")

    def test_known_csv_atomic_merge_and_log_append_are_allowed(self):
        csv = self.legacy / "output" / "candidates.csv"
        part = csv.with_name(".candidates.csv.part")
        csv.write_text("old row\n")
        log = self.legacy / "logs" / "run.log"
        log.write_text("old log\n")
        with self.guard:
            part.write_text(csv.read_text() + "new row\n")
            part.replace(csv)
            with log.open("a") as handle:
                handle.write("new log\n")
            with self.assertRaises(LegacyOutputProtected):
                log.write_text("truncate")
        self.assertEqual(csv.read_text(), "old row\nnew row\n")
        self.assertEqual(log.read_text(), "old log\nnew log\n")

    def test_factory_cache_cleanup_does_not_touch_old_data(self):
        old = self.legacy / "data" / "frame-001.jpg"
        old.write_bytes(b"old")
        cached = self.factory / "frame-001.jpg"
        with self.guard:
            cached.write_bytes(b"temporary")
            cached.unlink()
            (self.legacy / "downloads" / "new-source.json").write_text("new task metadata")
        self.assertEqual(old.read_bytes(), b"old")

    def test_symlink_cannot_bypass_protected_target(self):
        original = self.legacy / "downloads" / "original.json"
        original.write_text("keep")
        alias = self.factory / "alias.json"
        alias.symlink_to(original)
        with self.guard:
            with self.assertRaises(LegacyOutputProtected):
                alias.write_text("changed")
        self.assertEqual(original.read_text(), "keep")

    def test_wrapper_disables_archive_and_detects_swallowed_old_sidecar_write(self):
        metadata = self.legacy / "downloads" / "source.json"
        metadata.write_text("keep metadata")
        factory = self.factory
        metadata_path = metadata
        instances = []

        class Pipeline:
            def __init__(self, *_args):
                self.config = {"search_max_results": 0, "vidbee_wait_timeout_s": 0, "vidbee_poll_interval_s": .2}
                self.logger = None
                self.vidbee = None
                instances.append(self)

            def _load_checkpoint(self, fill_authors=False):
                return {"old": {"video_id": "old", "decision": "rejected"}}

            def _checkpoint_key(self, keyword, video_id, context_id):
                return context_id + video_id

            def _base_row(self, spec, hit):
                return {"checkpoint_key": "current", "video_id": hit.video_id, "decision": "manual_review"}

            def process_hit(self, browser, spec, hit, checkpoint):
                if self._archive_active:
                    raise AssertionError("Factory must not invoke legacy country archive cleanup")
                if not Path(self.config["data_dir"]).is_relative_to(factory):
                    raise AssertionError("analysis cache must belong to Factory")
                try:
                    metadata_path.write_text("overwrite")
                except Exception:
                    return {**self._base_row(spec, hit), "reason": "processing_error"}

            def _write_outputs(self):
                self.saved = self.rows

        class Browser:
            def __init__(self, *_args):
                pass

            def connect(self):
                return self

            def ensure_tiktok_search_login(self):
                pass

            def close(self):
                pass

        modules = {}
        for name, values in {
            "src.browser": {"BrowserClient": Browser, "LoginRequired": type("LoginRequired", (Exception,), {}), "VerificationRequired": type("VerificationRequired", (Exception,), {})},
            "src.excel_input": {"KeywordSpec": lambda keyword, references, fingerprint, image_error, country, source_row, source_document, context_id: SimpleNamespace(keyword=keyword, country=country, context_id=context_id)},
            "src.pipeline": {"Pipeline": Pipeline},
            "src.tiktok_search": {"SearchHit": SimpleNamespace, "search_keyword": lambda *_args: [SimpleNamespace(url="https://www.tiktok.com/@unit/video/1", video_id="1")], "video_id": lambda url: "1"},
            "src.tencent_docs": {"TencentDocsLoginRequired": type("TencentDocsLoginRequired", (Exception,), {})},
        }.items():
            module = ModuleType(name)
            module.__dict__.update(values)
            modules[name] = module
        image = self.factory / "product.jpg"
        image.write_bytes(b"fixture")
        context = {"task_id": "unit", "context_id": "unit-context", "country": "MX", "keyword": "body", "product_images": [str(image)]}
        with patch.dict(sys.modules, modules), patch.object(sys, "path", sys.path.copy()):
            result = execute(context, self.legacy, self.factory, self.factory / "collector-result.json")
        self.assertEqual(result["error_code"], "LEGACY_OUTPUT_PROTECTED")
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["retryable"])
        self.assertEqual(metadata.read_text(), "keep metadata")
        self.assertEqual(len(instances[0].saved), 2)
        self.assertTrue(result["legacy_result_reference"]["protected_operations"])


if __name__ == "__main__":
    unittest.main()
