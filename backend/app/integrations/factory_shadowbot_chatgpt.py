"""Factory desktop entry point; the verified legacy consumer stays read-only."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import time

FACTORY_ROOT = Path(__file__).resolve().parents[3]
LEGACY_CONSUMER = FACTORY_ROOT.parent / "video_auto/shadowbot_chatgpt.py"
LEGACY_SHA256 = "65136b117031afe119e249ad3ee3d2101f2693474ec7a98675f95cfc46747187"
DOWNLOAD_NOT_READY = "chatgpt_control_not_unique:download:0"
IMAGE_TIMEOUT = "chatgpt_image_timeout"
IDENTITY_UNCONFIRMED = "FACTORY_RPA_IMAGE_IDENTITY_UNCONFIRMED"
HANDLER_FINGERPRINT = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]

DIAGNOSE_IMAGE = """function(element, argument) {
    const request=JSON.parse(argument), normalize=s=>s.replace(/\\s+/g,' ').trim();
    const wanted=normalize(request.instruction), visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
    const describe=e=>({tag:e.tagName,testid:e.getAttribute('data-testid'),
        turn_key:e.getAttribute('data-turn-key'),role:e.getAttribute('data-message-author-role'),visible:visible(e)});
    const turns=[...document.querySelectorAll('[data-turn-key]')];
    const images=[...document.querySelectorAll('img')].filter(visible);
    return JSON.stringify({main_count:document.querySelectorAll('main').length,
        main_turn_count:document.querySelectorAll('main [data-turn-key]').length,
        turn_count:turns.length,instruction_length:wanted.length,
        conversation_turn_count:document.querySelectorAll('[data-testid^="conversation-turn-"]').length,
        message_role_count:document.querySelectorAll('[data-message-author-role]').length,
        generated_preview_count:document.querySelectorAll('[data-testid="generated-image-preview"]').length,
        turns:turns.slice(-6).map(e=>({...describe(e),text_length:normalize(e.textContent||'').length,
            prompt_matches:normalize(e.textContent||'').includes(wanted),
            roles:[...e.querySelectorAll('[data-message-author-role]')].map(n=>n.getAttribute('data-message-author-role')),
            testids:[...e.querySelectorAll('[data-testid]')].map(n=>n.getAttribute('data-testid')).slice(-24)})),
        messages:[...document.querySelectorAll('[data-message-author-role]')].slice(-6).map(e=>({...describe(e),
            text_length:normalize(e.textContent||'').length,prompt_matches:normalize(e.textContent||'').includes(wanted)})),
        images:images.slice(-12).map(e=>({...describe(e),alt_length:(e.alt||'').length,
            width:e.naturalWidth,height:e.naturalHeight,source_scheme:(e.currentSrc||'').split(':')[0],
            ancestors:[e.parentElement,e.parentElement?.parentElement,e.parentElement?.parentElement?.parentElement]
                .filter(Boolean).map(describe)}))});
}"""

BIND_IMAGE = """function(element, argument) {
    const request=JSON.parse(argument), normalize=s=>s.replace(/\\s+/g,' ').trim();
    const turns=[...document.querySelectorAll('main [data-turn-key]')];
    const wanted=normalize(request.instruction);
    const matches=wanted ? turns.filter(e=>normalize(e.textContent||'').includes(wanted)) : [];
    const promptTurn=matches.length===1 ? matches[0] : null;
    const previews=turn=>turn ? [...turn.querySelectorAll('[data-testid="generated-image-preview"] img')] : [];
    let imageTurn=promptTurn;
    if(promptTurn && !previews(promptTurn).length) {
        const next=turns[turns.indexOf(promptTurn)+1];
        const nextIsUser=next && (next.getAttribute('data-message-author-role')==='user'
            || next.querySelectorAll('[data-message-author-role="user"]').length);
        const nextIsAssistant=next && (next.getAttribute('data-message-author-role')==='assistant'
            || next.querySelectorAll('[data-message-author-role="assistant"]').length
            || /^已生成图像\\s*\\d*$/.test(normalize(next.textContent||'')));
        imageTurn=next && !nextIsUser && nextIsAssistant && previews(next).length ? next : null;
    }
    const images=previews(imageTurn).filter(e=>e.naturalWidth>=512 && e.naturalHeight>=512 && e.currentSrc);
    const promptMatches=!!promptTurn && !!imageTurn;
    document.querySelectorAll('[data-factory-background-image]').forEach(e=>e.removeAttribute('data-factory-background-image'));
    if(promptMatches && images.length===1) {
        images[0].setAttribute('data-factory-background-image','background');
    }
    const viewerOpen=[...document.querySelectorAll('[role="dialog"]')]
        .some(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length));
    return JSON.stringify({prompt_matches:promptMatches,prompt_count:matches.length,count:images.length,
        viewer_open:viewerOpen,
        prompt_turn_key:promptMatches ? promptTurn.getAttribute('data-turn-key') : null,
        turn_key:imageTurn?.getAttribute('data-turn-key')||null,
        src:images.length===1 ? images[0].currentSrc : null});
}"""

VERIFY_VIEWER = """function(element, argument) {
    const expected=JSON.parse(argument), visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
    const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
    const images=dialogs.flatMap(e=>[...e.querySelectorAll('img')]).filter(e=>visible(e)
        && !e.closest('button') && e.naturalWidth>=512 && e.naturalHeight>=512 && e.currentSrc);
    return JSON.stringify({viewer_open:dialogs.length>0,count:images.length,
        matches:images.length===1 && images[0].currentSrc===expected.src,
        images:images.map(e=>({src:e.currentSrc,alt:e.alt,
            width:e.naturalWidth,height:e.naturalHeight,
            rendered_width:e.getBoundingClientRect().width,rendered_height:e.getBoundingClientRect().height,
            testid:e.getAttribute('data-testid'),in_button:!!e.closest('button'),
            ancestors:[e.parentElement,e.parentElement?.parentElement,e.parentElement?.parentElement?.parentElement]
                .filter(Boolean).map(n=>({tag:n.tagName,testid:n.getAttribute('data-testid'),role:n.getAttribute('role')}))}))});
}"""


def legacy_consumer():
    if hashlib.sha256(LEGACY_CONSUMER.read_bytes()).hexdigest() != LEGACY_SHA256:
        raise RuntimeError("FACTORY_RPA_LEGACY_CONSUMER_CHANGED")
    spec = importlib.util.spec_from_file_location("factory_readonly_legacy_chatgpt", LEGACY_CONSUMER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rpa_storage_root():
    launcher = FACTORY_ROOT / "config/launcher.local.json"
    if launcher.is_file():
        config = json.loads(launcher.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("FACTORY_RPA_LAUNCHER_CONFIG_INVALID")
        acceptance = config.get("environment", "acceptance" if os.name == "nt" else "production") == "acceptance"
        value = config.get("storage_root", "storage/windows-acceptance" if acceptance else "storage")
    else:
        value = os.environ.get("STORAGE_ROOT", "storage")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("FACTORY_RPA_STORAGE_ROOT_INVALID")
    storage = Path(value).expanduser()
    if not storage.is_absolute():
        storage = FACTORY_ROOT / storage
    return (storage / "rpa").resolve()


def read_request(request_path):
    path = Path(request_path).resolve()
    if not path.is_relative_to(rpa_storage_root()) or path.name != "background_image_request.json":
        raise ValueError("FACTORY_RPA_REQUEST_OUTSIDE_STORAGE")
    request = json.loads(path.read_text(encoding="utf-8"))
    if (not re.fullmatch(r"[a-f0-9]{32}", request.get("execution_id", ""))
            or not re.fullmatch(r"[a-f0-9]{64}", request.get("identity", ""))
            or Path(request["response_path"]).resolve() != path.parent / "rpa_response.json"
            or Path(request["download_dir"]).resolve() != path.parent):
        raise ValueError("FACTORY_RPA_REQUEST_IDENTITY_INVALID")
    if request.get("action") == "generate":
        state = json.loads((path.parent / "background_image.json").read_text())
        identity = hashlib.sha256(json.dumps({"instruction": request["instruction"], "app_id": state["app_id"]},
                                             ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if (identity != request["identity"] or state.get("identity") != identity
                or state.get("execution_id") != request["execution_id"] or state.get("instruction") != request["instruction"]):
            raise ValueError("FACTORY_RPA_GENERATION_IDENTITY_CHANGED")
    return path, request


def download_recovery(request_path, request):
    directory = request_path.parent
    response_path, ui_path = directory / "rpa_response.json", directory / "rpa_ui_state.json"
    if not response_path.is_file() or not ui_path.is_file() or request.get("action") != "generate":
        return False
    response, ui = json.loads(response_path.read_text()), json.loads(ui_path.read_text())
    submitted = (response.get("identity") == request["identity"] and response.get("execution_id") == request["execution_id"]
        and response.get("status") == "failed"
        and ui.get("identity") == request["identity"] and ui.get("status") in {"submitted", "image_verified"}
        and ui.get("execution_id") in {None, request["execution_id"]}
        and ui.get("conversation_url", "").rstrip("/") == request["chatgpt_url"].rstrip("/"))
    if not submitted:
        return False
    queue = rpa_storage_root() / "rpa_queue"
    for suffix in (".json", ".claim.json"):
        path = queue / (request["execution_id"] + suffix)
        if not path.is_file():
            return False
        record = json.loads(path.read_text())
        if (record.get("identity") != request["identity"] or record.get("execution_id") != request["execution_id"]
                or Path(record.get("request_path", "")).resolve() != request_path):
            return False
    if str(response.get("error", "")).startswith((DOWNLOAD_NOT_READY, IMAGE_TIMEOUT)):
        return True
    generated_path = directory / "rpa_generated_image.json"
    if (str(response.get("error", "")).startswith(IDENTITY_UNCONFIRMED)
            and generated_path.is_file() and response.get("recovery") in {None, "download_only"}
            and response.get("paid_requests", 0) == 0):
        generated = json.loads(generated_path.read_text())
        if (generated.get("identity") == request["identity"] and generated.get("execution_id") == request["execution_id"]
                and generated.get("conversation_url", "").rstrip("/") == request["chatgpt_url"].rstrip("/")
                and generated.get("prompt_matches") is True and generated.get("prompt_count") == 1
                and generated.get("count") == 1 and generated.get("turn_key") and generated.get("src")):
            return True
    if (not str(response.get("error", "")).startswith(IDENTITY_UNCONFIRMED)
            or response.get("recovery") != "download_only" or response.get("paid_requests") != 0):
        return False
    archive = directory / ("rpa_response_" + request["execution_id"] + "_download_control_failed.json")
    claims = [queue / (request["execution_id"] + suffix) for suffix in (".claim.json", ".download-recovery.claim.json")]
    if not archive.is_file() or not all(path.is_file() for path in claims):
        return False
    original = json.loads(archive.read_text())
    if (original.get("identity") != request["identity"] or original.get("execution_id") != request["execution_id"]
            or original.get("status") != "failed" or not str(original.get("error", "")).startswith((DOWNLOAD_NOT_READY, IMAGE_TIMEOUT))):
        return False
    for path in claims:
        claim = json.loads(path.read_text())
        if (claim.get("identity") != request["identity"] or claim.get("execution_id") != request["execution_id"]
                or Path(claim.get("request_path", "")).resolve() != request_path):
            return False
    recovery_claim = json.loads(claims[1].read_text())
    return recovery_claim.get("action") == "resume_download_only" and recovery_claim.get("paid_requests") == 0


def next_request(queue_dir):
    queue = Path(queue_dir).resolve()
    if not queue.is_relative_to(rpa_storage_root()):
        raise ValueError("FACTORY_RPA_QUEUE_OUTSIDE_STORAGE")
    legacy = legacy_consumer()
    for ticket_path in sorted(queue.glob("*.json")):
        if ticket_path.name.endswith(".claim.json"):
            continue
        ticket = json.loads(ticket_path.read_text())
        request_path, request = read_request(ticket["request_path"])
        claim_path = queue / (request["execution_id"] + ".claim.json")
        if (ticket_path.stem != request["execution_id"] or ticket.get("identity") != request["identity"]
                or not claim_path.is_file() or not download_recovery(request_path, request)):
            continue
        claim = json.loads(claim_path.read_text())
        if any(claim.get(key) != ticket.get(key) for key in ("identity", "execution_id", "request_path")):
            raise ValueError("FACTORY_RPA_RECOVERY_CLAIM_MISMATCH")
        response = json.loads(Path(request["response_path"]).read_text())
        suffix = ".download-recovery.claim.json"
        if str(response.get("error", "")).startswith(IDENTITY_UNCONFIRMED):
            suffix = ".download-recovery-" + HANDLER_FINGERPRINT + ".claim.json"
        recovery_path = queue / (request["execution_id"] + suffix)
        try:
            descriptor = os.open(recovery_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({**ticket, "action": "resume_download_only", "claimed_at": int(time.time()),
                       "handler_fingerprint": HANDLER_FINGERPRINT, "paid_requests": 0}, handle)
        print("[RPA RECOVERY] 继续已提交请求的图片下载；不重新发送提示词", flush=True)
        return str(request_path)
    return legacy.next_request(str(queue))


def main(args):
    request_path, request = read_request(args["request_path"])
    legacy = legacy_consumer()
    original_control, original_download = legacy.control, legacy.download_image
    bound_image = None

    def control(page, marker, labels=None, editor=False):
        if marker != "download":
            return original_control(page, marker, labels, editor)
        deadline = time.monotonic() + min(120, float(request.get("download_timeout_s", 120)))
        while True:
            legacy.check_conversation(page, request)
            legacy.check_blocker(json.loads(page.execute_javascript(legacy.SNAPSHOT)))
            viewer = json.loads(page.execute_javascript(VERIFY_VIEWER, argument=json.dumps(bound_image)))
            if not viewer.get("matches"):
                raise RuntimeError(IDENTITY_UNCONFIRMED + ": 下载前图片查看器与原提示词图片不一致；未下载或重新生成")
            try:
                return original_control(page, marker, labels, editor)
            except RuntimeError as exc:
                if not str(exc).startswith(DOWNLOAD_NOT_READY) or time.monotonic() >= deadline:
                    raise
                time.sleep(.5)

    def download(page, value):
        nonlocal bound_image
        evidence = json.loads(page.execute_javascript(BIND_IMAGE, argument=json.dumps(value, ensure_ascii=False)))
        diagnostics = json.loads(page.execute_javascript(DIAGNOSE_IMAGE, argument=json.dumps(value, ensure_ascii=False)))
        diagnostics.update(identity=value["identity"], execution_id=value["execution_id"], inspected_at=time.time(),
                           handler_fingerprint=HANDLER_FINGERPRINT, prompt_matches=evidence.get("prompt_matches"),
                           prompt_turn_key=evidence.get("prompt_turn_key"), bound_turn_key=evidence.get("turn_key"),
                           bound_image_count=evidence.get("count"), paid_requests=0)
        diagnostic_path = request_path.parent / ("rpa_dom_diagnostic_" + str(time.time_ns()) + ".json")
        legacy.save_response(diagnostic_path, diagnostics)
        if not evidence.get("prompt_matches") or evidence.get("count") != 1 or not evidence.get("turn_key"):
            raise RuntimeError(IDENTITY_UNCONFIRMED + ": 保留页面，不下载历史图或重新生成；只读诊断=" + str(diagnostic_path))
        evidence.update(identity=value["identity"], execution_id=value["execution_id"], conversation_url=page.get_url(),
                        verified_at=time.time(), paid_requests=0)
        legacy.save_response(request_path.parent / "rpa_generated_image.json", evidence)
        bound_image = {"src": evidence["src"]}
        if evidence.get("viewer_open"):
            viewer = json.loads(page.execute_javascript(VERIFY_VIEWER, argument=json.dumps(bound_image)))
            legacy.save_response(request_path.parent / ("rpa_viewer_diagnostic_" + str(time.time_ns()) + ".json"),
                                 {**viewer, "identity": value["identity"], "execution_id": value["execution_id"],
                                  "bound_src": evidence["src"], "handler_fingerprint": HANDLER_FINGERPRINT, "paid_requests": 0})
            if not viewer.get("matches"):
                raise RuntimeError(IDENTITY_UNCONFIRMED + ": 已打开其它图片查看器；保留页面，不下载或重新生成")
        else:
            page.find_by_css('[data-factory-background-image="background"]', timeout=10).click()
            deadline = time.monotonic() + min(30, float(value.get("download_timeout_s", 120)))
            while True:
                legacy.check_conversation(page, value)
                viewer = json.loads(page.execute_javascript(VERIFY_VIEWER, argument=json.dumps(bound_image)))
                if viewer.get("matches"):
                    break
                if viewer.get("count", 0) or time.monotonic() >= deadline:
                    raise RuntimeError(IDENTITY_UNCONFIRMED + ": 原提示词图片查看器身份未确认；未下载或重新生成")
                time.sleep(.5)
        return {**original_download(page, value), "generated_turn_key": evidence["turn_key"], "generated_src": evidence["src"]}

    legacy.control, legacy.download_image = control, download
    try:
        if download_recovery(request_path, request):
            import xbot
            previous = Path(request["response_path"]).read_bytes()
            archive = request_path.parent / ("rpa_response_" + request["execution_id"] + "_download_control_failed.json")
            if not archive.is_file():
                with archive.open("xb") as handle:
                    handle.write(previous)
            elif archive.read_bytes() != previous:
                prior = request_path.parent / ("rpa_response_download_recovery_" + hashlib.sha256(previous).hexdigest()[:16] + ".json")
                if not prior.exists():
                    with prior.open("xb") as handle:
                        handle.write(previous)
            response = {"identity": request["identity"], "execution_id": request["execution_id"], "status": "failed",
                        "recovery": "download_only", "handler_fingerprint": HANDLER_FINGERPRINT, "paid_requests": 0}
            try:
                page = xbot.web.get(url=legacy.fixed_chat_url(request["chatgpt_url"]), mode="chrome", use_wildcard=False,
                                    open_page=False, load_timeout=30, silent_running=True)
                legacy.check_conversation(page, request)
                response.update(download(page, request), status="completed")
            except Exception as exc:
                response["error"] = str(exc)[:1200]
            legacy.save_response(request["response_path"], response)
            if response["status"] != "completed":
                if response["error"].startswith(IDENTITY_UNCONFIRMED):
                    print("[RPA WAITING] 图片身份待确认；只读诊断已保存，未下载或重新生成", flush=True)
                    return response
                raise RuntimeError(response["error"])
            return response
        else:
            if args.get("download_only") or (request.get("action") == "generate" and (request_path.parent / "rpa_ui_state.json").exists()):
                raise RuntimeError("UNKNOWN_EXTERNAL_REQUEST: 旧提交不满足安全仅下载恢复条件，未再次发送提示词")
            legacy.main(args)
    finally:
        legacy.control, legacy.download_image = original_control, original_download
