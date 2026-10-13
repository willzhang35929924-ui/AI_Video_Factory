import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from app.integrations import factory_shadowbot_chatgpt as handler
from app.integrations.legacy import video_worker


class FactoryRPADownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / "storage/rpa/backgrounds/unit/BG01"
        self.directory.mkdir(parents=True)
        self.queue = self.root / "storage/rpa/rpa_queue"
        self.queue.mkdir()
        self.downloads = self.root / "Downloads"
        self.downloads.mkdir()
        instruction, app_id = "完整背景提示词\n单张客厅图片", "isolated-app"
        identity = hashlib.sha256(json.dumps({"instruction": instruction, "app_id": app_id}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.request = {"action": "generate", "identity": identity, "execution_id": "a" * 32, "instruction": instruction,
            "chatgpt_url": "https://chatgpt.com/c/12345678-1234-1234-1234-123456789abc",
            "download_dir": str(self.directory), "response_path": str(self.directory / "rpa_response.json"),
            "browser_download_dir": str(self.downloads), "download_timeout_s": 10}
        self.request_path = self.directory / "background_image_request.json"
        self.request_path.write_text(json.dumps(self.request, ensure_ascii=False))
        (self.directory / "background_image.json").write_text(json.dumps({"identity": identity, "execution_id": "a" * 32,
            "app_id": app_id, "instruction": instruction, "status": "failed"}))
        self.response = {"identity": identity, "execution_id": "a" * 32, "status": "failed",
            "error": handler.DOWNLOAD_NOT_READY + "; browser preserved"}
        Path(self.request["response_path"]).write_text(json.dumps(self.response))
        (self.directory / "rpa_ui_state.json").write_text(json.dumps({"identity": identity, "status": "submitted", "conversation_url": self.request["chatgpt_url"]}))
        ticket = {"identity": identity, "execution_id": "a" * 32, "request_path": str(self.request_path)}
        for suffix in (".json", ".claim.json"):
            (self.queue / ("a" * 32 + suffix)).write_text(json.dumps(ticket))
        self.legacy = handler.legacy_consumer()
        self.patches = [patch.object(handler, "FACTORY_ROOT", self.root), patch.object(handler, "legacy_consumer", return_value=self.legacy)]
        for replacement in self.patches:
            replacement.start()

    def tearDown(self):
        for replacement in reversed(self.patches):
            replacement.stop()
        self.temporary.cleanup()

    def configure_storage(self, storage):
        config_path = self.root / "config/launcher.local.json"
        config_path.parent.mkdir(exist_ok=True)
        config_path.write_text(json.dumps({"environment": "acceptance", "storage_root": str(storage)}))

    def move_to_acceptance_storage(self):
        destination = self.root / "storage/windows-acceptance"
        destination.mkdir()
        shutil.move(str(self.root / "storage/rpa"), str(destination / "rpa"))
        self.directory = destination / "rpa/backgrounds/unit/BG01"
        self.queue = destination / "rpa/rpa_queue"
        self.request_path = self.directory / "background_image_request.json"
        self.request.update(download_dir=str(self.directory), response_path=str(self.directory / "rpa_response.json"))
        self.request_path.write_text(json.dumps(self.request, ensure_ascii=False))
        for suffix in (".json", ".claim.json"):
            path = self.queue / (self.request["execution_id"] + suffix)
            ticket = json.loads(path.read_text())
            path.write_text(json.dumps({**ticket, "request_path": str(self.request_path)}))
        self.configure_storage("storage/windows-acceptance")
        return destination

    def test_launcher_storage_root_reads_isolated_request_and_original_recovery_queue(self):
        destination = self.move_to_acceptance_storage()
        self.assertEqual(handler.rpa_storage_root(), destination / "rpa")
        self.assertEqual(handler.read_request(self.request_path), (self.request_path, self.request))
        self.assertTrue(handler.download_recovery(self.request_path, self.request))
        original = self.request_path.read_bytes()
        self.assertEqual(handler.next_request(self.queue), str(self.request_path))
        self.assertEqual(handler.next_request(self.queue), "")
        self.assertEqual(self.request_path.read_bytes(), original)
        claim = json.loads((self.queue / (self.request["execution_id"] + ".download-recovery.claim.json")).read_text())
        self.assertEqual(claim["paid_requests"], 0)
        self.assertEqual(claim["identity"], self.request["identity"])

    def test_acceptance_root_rejects_old_formal_request_and_queue_before_legacy(self):
        self.configure_storage("storage/windows-acceptance")
        with self.assertRaisesRegex(ValueError, "FACTORY_RPA_REQUEST_OUTSIDE_STORAGE"):
            handler.read_request(self.request_path)
        with patch.object(handler, "legacy_consumer") as legacy:
            with self.assertRaisesRegex(ValueError, "FACTORY_RPA_QUEUE_OUTSIDE_STORAGE"):
                handler.next_request(self.queue)
        legacy.assert_not_called()
        self.assertFalse(list(self.queue.glob("*.download-recovery*.json")))

    def test_launcher_root_takes_precedence_over_shadowbot_environment(self):
        self.configure_storage("storage/windows-acceptance")
        with patch.dict(handler.os.environ, {"STORAGE_ROOT": str(self.root / "unrelated")}, clear=False):
            self.assertEqual(handler.rpa_storage_root(), self.root / "storage/windows-acceptance/rpa")

    def test_explicit_storage_environment_without_launcher_and_mac_default(self):
        with patch.dict(handler.os.environ, {"STORAGE_ROOT": str(self.root / "environment-storage")}, clear=False):
            self.assertEqual(handler.rpa_storage_root(), self.root / "environment-storage/rpa")
        with patch.dict(handler.os.environ, {}, clear=True):
            self.assertEqual(handler.rpa_storage_root(), self.root / "storage/rpa")

    def test_acceptance_default_and_invalid_local_config_do_not_fall_back_to_formal(self):
        self.configure_storage("storage/windows-acceptance")
        config_path = self.root / "config/launcher.local.json"
        config_path.write_text(json.dumps({"environment": "acceptance"}))
        self.assertEqual(handler.rpa_storage_root(), self.root / "storage/windows-acceptance/rpa")
        for value in ("", None, 1):
            config_path.write_text(json.dumps({"storage_root": value}))
            with self.assertRaisesRegex(ValueError, "FACTORY_RPA_STORAGE_ROOT_INVALID"):
                handler.read_request(self.request_path)
        config_path.write_text("[]")
        with self.assertRaisesRegex(ValueError, "FACTORY_RPA_LAUNCHER_CONFIG_INVALID"):
            handler.next_request(self.queue)

    def test_isolated_storage_symlink_cannot_import_another_root_request(self):
        destination = self.move_to_acceptance_storage()
        link = destination / "rpa/other-root"
        link.symlink_to(self.root / "storage", target_is_directory=True)
        outside = self.root / "storage/outside/background_image_request.json"
        with self.assertRaisesRegex(ValueError, "FACTORY_RPA_REQUEST_OUTSIDE_STORAGE"):
            handler.read_request(link / "outside/background_image_request.json")
        self.assertFalse(outside.exists())

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the actual DOM binding function")
    def test_actual_binding_supports_unique_historical_prompt_and_adjacent_image(self):
        script = """const bind=FUNCTION;
        const img=()=>({naturalWidth:941,naturalHeight:1672,currentSrc:'blob:latest',setAttribute(){}});
        const turn=(key,text,images=[],role=null)=>({textContent:text,getAttribute(name){return name==='data-turn-key'?key:role},
            querySelectorAll(selector){return selector==='[data-testid="generated-image-preview"] img'?images:[]}});
        let turns=[];
        global.document={querySelectorAll(selector){
            if(selector==='main [data-turn-key]') return turns;
            return [];
        }};
        const inspect=()=>JSON.parse(bind(null,JSON.stringify({instruction:'完整提示词\\nMexico背景'})));
        turns=[turn('history','旧场景',[img()]),turn('latest','完整提示词\\nMexico背景 已生成图像1',[img()])];
        const combined=inspect();
        turns=[turn('request','完整提示词\\nMexico背景'),turn('image','已生成图像1',[img()])];
        const separate=inspect();
        turns=[turn('history','完整提示词\\nMexico背景',[img()]),turn('other','另一场景',[img()])];
        const historical=inspect();
        turns=[turn('request','完整提示词\\nMexico背景'),turn('image','已生成图像1',[img()]),
            turn('other-request','另一场景'),turn('other-image','已生成图像1',[img()])];
        const historicalSeparate=inspect();
        turns=[turn('request','完整提示词\\nMexico背景'),turn('other','另一用户场景',[img()],'user')];
        const unrelatedUser=inspect();
        turns=[turn('request','完整提示词\\nMexico背景'),turn('other','另一背景完整提示词 已生成图像1',[img()])];
        const unrelatedCombined=inspect();
        turns=[turn('one','完整提示词\\nMexico背景',[img()]),turn('two','完整提示词\\nMexico背景',[img()])];
        const duplicate=inspect();
        turns=[turn('other','Mexico背景',[img()])];
        const partial=inspect();
        turns=[turn('latest','完整提示词\\nMexico背景',[img(),img()])];
        const ambiguous=inspect();
        process.stdout.write(JSON.stringify({combined,separate,historical,historicalSeparate,unrelatedUser,unrelatedCombined,duplicate,partial,ambiguous}));""".replace("FUNCTION", handler.BIND_IMAGE)
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=10, check=True)
        cases = json.loads(result.stdout)
        self.assertTrue(cases["combined"]["prompt_matches"])
        self.assertEqual(cases["combined"]["prompt_turn_key"], "latest")
        self.assertEqual(cases["combined"]["count"], 1)
        self.assertTrue(cases["separate"]["prompt_matches"])
        self.assertEqual(cases["separate"]["prompt_turn_key"], "request")
        self.assertTrue(cases["historical"]["prompt_matches"])
        self.assertEqual(cases["historical"]["turn_key"], "history")
        self.assertTrue(cases["historicalSeparate"]["prompt_matches"])
        self.assertEqual(cases["historicalSeparate"]["turn_key"], "image")
        for case in ("unrelatedUser", "unrelatedCombined", "duplicate", "partial"):
            self.assertFalse(cases[case]["prompt_matches"])
        self.assertEqual(cases["duplicate"]["prompt_count"], 2)
        self.assertEqual(cases["ambiguous"]["count"], 2)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the actual viewer identity function")
    def test_actual_viewer_requires_unique_image_with_exact_bound_source(self):
        script = """const verify=FUNCTION;
        const img=(src,thumbnail=false)=>({naturalWidth:941,naturalHeight:1672,currentSrc:src,offsetWidth:941,
            closest(){return thumbnail?{}:null},getAttribute(){return null},
            getBoundingClientRect(){return {width:thumbnail?42:551,height:thumbnail?42:977}}});
        const dialog=images=>({offsetWidth:941,querySelectorAll(){return images}});
        let dialogs=[];
        global.document={querySelectorAll(){return dialogs}};
        const inspect=()=>JSON.parse(verify(null,JSON.stringify({src:'blob:bg11'})));
        const absent=inspect();
        dialogs=[dialog([img('blob:bg12')])]; const wrong=inspect();
        dialogs=[dialog([img('blob:bg11')])]; const exact=inspect();
        dialogs=[dialog([img('blob:bg11'),img('blob:bg12')])]; const ambiguous=inspect();
        dialogs=[dialog([img('blob:bg10',true),img('blob:bg11',true),img('blob:bg11')])]; const gallery=inspect();
        dialogs=[dialog([img('blob:bg11',true),img('blob:bg12')])]; const wrongWithExpectedThumbnail=inspect();
        dialogs=[dialog([img('blob:bg11',true)])]; const onlyThumbnail=inspect();
        process.stdout.write(JSON.stringify({absent,wrong,exact,ambiguous,gallery,wrongWithExpectedThumbnail,onlyThumbnail}));""".replace("FUNCTION", handler.VERIFY_VIEWER)
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=10, check=True)
        cases = json.loads(result.stdout)
        self.assertTrue(cases["exact"]["matches"])
        self.assertTrue(cases["gallery"]["matches"])
        self.assertEqual(cases["gallery"]["count"], 1)
        for case in ("absent", "wrong", "ambiguous", "wrongWithExpectedThumbnail", "onlyThumbnail"):
            self.assertFalse(cases[case]["matches"])

    def test_initial_identity_failure_can_resume_only_with_bound_generated_image(self):
        self.response["error"] = handler.IDENTITY_UNCONFIRMED
        Path(self.request["response_path"]).write_text(json.dumps(self.response))
        self.assertFalse(handler.download_recovery(self.request_path, self.request))
        generated_path = self.directory / "rpa_generated_image.json"
        generated = {"identity": self.request["identity"], "execution_id": self.request["execution_id"],
            "conversation_url": self.request["chatgpt_url"], "prompt_matches": True, "prompt_count": 1,
            "count": 1, "turn_key": "original-generated-turn", "src": "blob:original"}
        generated_path.write_text(json.dumps(generated))
        original = self.request_path.read_bytes()
        self.assertTrue(handler.download_recovery(self.request_path, self.request))
        self.assertEqual(handler.next_request(self.queue), str(self.request_path))
        self.assertEqual(handler.next_request(self.queue), "")
        self.assertEqual(self.request_path.read_bytes(), original)
        claim = json.loads((self.queue / ("a" * 32 + ".download-recovery-" + handler.HANDLER_FINGERPRINT + ".claim.json")).read_text())
        self.assertEqual(claim["action"], "resume_download_only")
        self.assertEqual(claim["paid_requests"], 0)
        for field, value in (("identity", "b" * 64), ("execution_id", "b" * 32), ("prompt_count", 2),
                             ("count", 2), ("turn_key", ""), ("src", ""), ("conversation_url", "https://chatgpt.com/")):
            generated_path.write_text(json.dumps({**generated, field: value}))
            self.assertFalse(handler.download_recovery(self.request_path, self.request), field)

    def test_same_submitted_ticket_gets_only_one_download_recovery_claim(self):
        original = self.request_path.read_bytes()
        self.assertEqual(handler.next_request(self.queue), str(self.request_path))
        self.assertEqual(handler.next_request(self.queue), "")
        claim = json.loads((self.queue / ("a" * 32 + ".download-recovery.claim.json")).read_text())
        self.assertEqual(claim["action"], "resume_download_only")
        self.assertEqual(claim["paid_requests"], 0)
        self.assertEqual(claim["handler_fingerprint"], handler.HANDLER_FINGERPRINT)
        self.assertEqual(self.request_path.read_bytes(), original)
        self.assertEqual(len(list(self.queue.glob("*.json"))), 3)

    def test_download_viewer_wait_preserves_history_and_never_sends_prompt(self):
        self.response["error"] = handler.IMAGE_TIMEOUT + ": 对话已保留，不自动再次发送提示词"
        Path(self.request["response_path"]).write_text(json.dumps(self.response))
        self.assertEqual(handler.next_request(self.queue), str(self.request_path))
        owner = self
        class Page:
            checks, clicks, preview_clicks = 0, 0, 0
            def get_url(self):
                return owner.request["chatgpt_url"]
            def execute_javascript(self, script, argument=None):
                if script == handler.BIND_IMAGE:
                    return json.dumps({"prompt_matches": True, "count": 1, "viewer_open": False, "turn_key": "actual-new-turn", "src": "blob:actual-new-image"})
                if script == handler.DIAGNOSE_IMAGE:
                    return json.dumps({"turn_count": 2, "main_turn_count": 2, "turns": [], "images": []})
                if script == handler.VERIFY_VIEWER:
                    owner.assertEqual(json.loads(argument), {"src": "blob:actual-new-image"})
                    return json.dumps({"viewer_open": True, "count": 1, "matches": self.preview_clicks == 1})
                if script == owner.legacy.SNAPSHOT:
                    return json.dumps({"text": "", "editors": [{"input_text": ""}], "buttons": []})
                if script == owner.legacy.MARK_CONTROL:
                    self.checks += 1
                    return json.dumps({"count": 1 if self.checks >= 3 else 0})
                raise AssertionError("不能输入、发送或生成新图片")
            def find_by_css(self, selector, timeout):
                if selector == '[data-factory-background-image="background"]':
                    return SimpleNamespace(click=lambda: setattr(self, "preview_clicks", self.preview_clicks + 1))
                return self
            def click(self):
                self.clicks += 1
                (owner.downloads / "actual-download.png").write_bytes(b"isolated-downloaded-image")
        page = Page()
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        original = Path(self.request["response_path"]).read_bytes()
        xbot = SimpleNamespace(web=SimpleNamespace(get=lambda **kwargs: page))
        with patch.dict("sys.modules", {"xbot": xbot}), patch.object(self.legacy, "main") as generate, \
                patch.object(handler.time, "monotonic", side_effect=lambda: clock[0]), patch.object(handler.time, "sleep", side_effect=sleep):
            handler.main({"request_path": str(self.request_path), "download_only": True})
        generate.assert_not_called()
        response = json.loads(Path(self.request["response_path"]).read_text())
        self.assertEqual(page.checks, 3)
        self.assertEqual(page.clicks, 1)
        self.assertEqual(page.preview_clicks, 1)
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["recovery"], "download_only")
        self.assertEqual(response["paid_requests"], 0)
        self.assertEqual(response["generated_turn_key"], "actual-new-turn")
        self.assertEqual(response["handler_fingerprint"], handler.HANDLER_FINGERPRINT)
        self.assertEqual(Path(response["image"]).read_bytes(), b"isolated-downloaded-image")
        archive = self.directory / ("rpa_response_" + "a" * 32 + "_download_control_failed.json")
        self.assertEqual(archive.read_bytes(), original)
        diagnostic = json.loads(next(self.directory.glob("rpa_dom_diagnostic_*.json")).read_text())
        self.assertEqual(diagnostic["bound_image_count"], 1)
        self.assertEqual(diagnostic["paid_requests"], 0)
        self.assertEqual(diagnostic["handler_fingerprint"], handler.HANDLER_FINGERPRINT)

    def test_generation_timeout_can_only_claim_original_download_once(self):
        self.response["error"] = handler.IMAGE_TIMEOUT + ": 对话已保留，不自动再次发送提示词"
        Path(self.request["response_path"]).write_text(json.dumps(self.response))
        original = self.request_path.read_bytes()
        self.assertTrue(handler.download_recovery(self.request_path, self.request))
        self.assertEqual(handler.next_request(self.queue), str(self.request_path))
        self.assertEqual(handler.next_request(self.queue), "")
        claim = json.loads((self.queue / ("a" * 32 + ".download-recovery.claim.json")).read_text())
        self.assertEqual(claim["action"], "resume_download_only")
        self.assertEqual(claim["paid_requests"], 0)
        self.assertEqual(self.request_path.read_bytes(), original)

    def test_timeout_recovery_rejects_missing_claim_wrong_ticket_and_ui_execution(self):
        self.response["error"] = handler.IMAGE_TIMEOUT
        Path(self.request["response_path"]).write_text(json.dumps(self.response))
        ticket_path = self.queue / ("a" * 32 + ".json")
        ticket = json.loads(ticket_path.read_text())
        ticket_path.write_text(json.dumps({**ticket, "identity": "b" * 64}))
        self.assertFalse(handler.download_recovery(self.request_path, self.request))
        ticket_path.write_text(json.dumps(ticket))
        ui_path = self.directory / "rpa_ui_state.json"
        ui = json.loads(ui_path.read_text())
        ui_path.write_text(json.dumps({**ui, "execution_id": "b" * 32}))
        self.assertFalse(handler.download_recovery(self.request_path, self.request))
        ui_path.write_text(json.dumps(ui))
        (self.queue / ("a" * 32 + ".claim.json")).unlink()
        self.assertFalse(handler.download_recovery(self.request_path, self.request))

    def test_timeout_recovery_with_other_viewer_never_clicks_or_sends(self):
        self.response["error"] = handler.IMAGE_TIMEOUT
        Path(self.request["response_path"]).write_text(json.dumps(self.response))
        owner = self
        class Page:
            def get_url(self):
                return owner.request["chatgpt_url"]
            def execute_javascript(self, script, argument=None):
                if script == handler.BIND_IMAGE:
                    return json.dumps({"prompt_matches": True, "count": 1, "viewer_open": True,
                        "turn_key": "bg11-historical", "src": "blob:bg11"})
                if script == handler.DIAGNOSE_IMAGE:
                    return json.dumps({"turn_count": 4})
                if script == handler.VERIFY_VIEWER:
                    return json.dumps({"viewer_open": True, "count": 1, "matches": False})
                raise AssertionError("不能输入或发送提示词")
            def find_by_css(self, *args, **kwargs):
                raise AssertionError("其它viewer未确认前不能点击或下载")
        xbot = SimpleNamespace(web=SimpleNamespace(get=lambda **kwargs: Page()))
        original = Path(self.request["response_path"]).read_bytes()
        self.assertEqual(handler.next_request(self.queue), str(self.request_path))
        with patch.dict("sys.modules", {"xbot": xbot}), patch.object(self.legacy, "main") as generate, \
                patch.object(self.legacy, "download_image") as download:
            result = handler.main({"request_path": str(self.request_path)})
        generate.assert_not_called()
        download.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["paid_requests"], 0)
        self.assertTrue(result["error"].startswith(handler.IDENTITY_UNCONFIRMED))
        archive = self.directory / ("rpa_response_" + "a" * 32 + "_download_control_failed.json")
        self.assertEqual(archive.read_bytes(), original)
        self.assertTrue(handler.download_recovery(self.request_path, self.request))

    def prepare_identity_failure(self):
        original = Path(self.request["response_path"]).read_bytes()
        archive = self.directory / ("rpa_response_" + "a" * 32 + "_download_control_failed.json")
        archive.write_bytes(original)
        self.assertEqual(handler.next_request(self.queue), str(self.request_path))
        self.response.update(error=handler.IDENTITY_UNCONFIRMED, recovery="download_only", paid_requests=0)
        Path(self.request["response_path"]).write_text(json.dumps(self.response))
        return archive, original

    def test_identity_failure_has_one_safe_readonly_retry_per_handler_version(self):
        archive, original = self.prepare_identity_failure()
        self.assertTrue(handler.download_recovery(self.request_path, self.request))
        self.assertEqual(handler.next_request(self.queue), str(self.request_path))
        self.assertEqual(handler.next_request(self.queue), "")
        self.assertEqual(archive.read_bytes(), original)
        self.assertEqual(len(list(self.queue.glob("*.json"))), 4)

    def test_identity_failure_needs_original_archive_and_claim_identity(self):
        archive, _ = self.prepare_identity_failure()
        claim = self.queue / ("a" * 32 + ".download-recovery.claim.json")
        record = json.loads(claim.read_text())
        record["identity"] = "b" * 64
        claim.write_text(json.dumps(record))
        self.assertFalse(handler.download_recovery(self.request_path, self.request))
        self.assertEqual(handler.next_request(self.queue), "")
        archive.unlink()
        self.assertFalse(handler.download_recovery(self.request_path, self.request))

    def test_unconfirmed_dom_is_saved_without_clicks_or_new_generation(self):
        owner = self
        class Page:
            def get_url(self):
                return owner.request["chatgpt_url"]
            def execute_javascript(self, script, argument=None):
                if script == handler.BIND_IMAGE:
                    return json.dumps({"prompt_matches": False, "count": 0, "turn_key": None})
                if script == handler.DIAGNOSE_IMAGE:
                    return json.dumps({"main_count": 0, "turn_count": 2, "turns": [{"prompt_matches": True}], "images": []})
                raise AssertionError("身份未确认前不进行其他网页操作")
            def find_by_css(self, *args, **kwargs):
                raise AssertionError("不点击或下载图片")
        self.prepare_identity_failure()
        xbot = SimpleNamespace(web=SimpleNamespace(get=lambda **kwargs: Page()))
        with patch.dict("sys.modules", {"xbot": xbot}), patch.object(self.legacy, "main") as generate:
            response = handler.main({"request_path": str(self.request_path)})
        generate.assert_not_called()
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["paid_requests"], 0)
        self.assertTrue(response["error"].startswith(handler.IDENTITY_UNCONFIRMED))
        diagnostic = json.loads(next(self.directory.glob("rpa_dom_diagnostic_*.json")).read_text())
        self.assertEqual(diagnostic["main_count"], 0)
        self.assertFalse(diagnostic["prompt_matches"])
        self.assertTrue(list(self.directory.glob("rpa_response_download_recovery_*.json")))

    def test_download_started_or_unknown_response_is_never_auto_replayed(self):
        (self.directory / "rpa_ui_state.json").write_text(json.dumps({"identity": self.request["identity"], "status": "download_started"}))
        self.assertEqual(handler.next_request(self.queue), "")
        with self.assertRaisesRegex(RuntimeError, "UNKNOWN_EXTERNAL_REQUEST"), patch.object(self.legacy, "main") as generate:
            handler.main({"request_path": str(self.request_path)})
        generate.assert_not_called()

    def test_explicit_download_only_entry_never_falls_back_to_generation(self):
        (self.directory / "rpa_ui_state.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "UNKNOWN_EXTERNAL_REQUEST"), patch.object(self.legacy, "main") as generate:
            handler.main({"request_path": str(self.request_path), "download_only": True})
        generate.assert_not_called()

    def test_edited_instruction_fails_before_claim_or_browser(self):
        self.request["instruction"] = "另一个背景"
        self.request_path.write_text(json.dumps(self.request))
        with self.assertRaisesRegex(ValueError, "GENERATION_IDENTITY_CHANGED"):
            handler.next_request(self.queue)

    def test_submitted_generation_waiting_for_download_is_not_generic_failure(self):
        context, result = self.root / "context.json", self.root / "result.json"
        context.write_text(json.dumps({"task_id": "isolated-task"}))
        arguments = ["factory-worker", "--legacy-root", str(self.root), "--stage", "background", "--context", str(context),
                     "--result", str(result), "--directory", str(self.directory)]
        with patch("sys.argv", arguments), patch.object(video_worker, "load_legacy", return_value={}), \
                patch.object(video_worker, "execute_stage", side_effect=RuntimeError(handler.DOWNLOAD_NOT_READY + "; browser preserved")):
            self.assertEqual(video_worker.main(), 2)
        response = json.loads(result.read_text())
        self.assertEqual(response["status"], "WAITING_REVIEW")
        self.assertEqual(response["error_code"], "BACKGROUND_DOWNLOAD_PENDING")
        self.assertFalse(response["retryable"])
        self.assertIn("仅恢复原票据下载，不重发", response["error_message"])

    def test_unconfirmed_existing_image_requires_review_not_paid_retry(self):
        context, result = self.root / "context.json", self.root / "result.json"
        context.write_text(json.dumps({"task_id": "isolated-task"}))
        arguments = ["factory-worker", "--legacy-root", str(self.root), "--stage", "background", "--context", str(context),
                     "--result", str(result), "--directory", str(self.directory)]
        with patch("sys.argv", arguments), patch.object(video_worker, "load_legacy", return_value={}), \
                patch.object(video_worker, "execute_stage", side_effect=RuntimeError(handler.IDENTITY_UNCONFIRMED)):
            self.assertEqual(video_worker.main(), 2)
        response = json.loads(result.read_text())
        self.assertEqual(response["status"], "WAITING_REVIEW")
        self.assertEqual(response["error_code"], "BACKGROUND_IMAGE_IDENTITY_UNCONFIRMED")
        self.assertFalse(response["retryable"])
        self.assertIn("仅恢复原票据下载", response["error_message"])


if __name__ == "__main__":
    unittest.main()
