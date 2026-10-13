"""Unpaid ticket handshake with the existing Shadowbot desktop consumer."""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from uuid import uuid4

CONSUMER_SHA256 = "65136b117031afe119e249ad3ee3d2101f2693474ec7a98675f95cfc46747187"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part.json")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def create_probe(storage_root: Path, chatgpt_url: str, consumer_path: Path) -> dict:
    # This exact consumer's main has an explicit action == 'probe' branch at lines 294-298.
    # It only reads SNAPSHOT/NEW_IMAGE; a changed consumer cannot receive a probe ticket.
    if hashlib.sha256(Path(consumer_path).read_bytes()).hexdigest() != CONSUMER_SHA256:
        raise ValueError("RPA_UNPAID_PROBE_CONSUMER_CHANGED")
    if not re.fullmatch(r"https://chatgpt\.com/c/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}/?", chatgpt_url):
        raise ValueError("RPA_FIXED_CHAT_REQUIRED")
    storage_root = Path(storage_root).resolve()
    output = (storage_root / "rpa").resolve()
    if not output.is_relative_to(storage_root):
        raise ValueError("RPA_OUTPUT_OUTSIDE_FACTORY")
    execution_id = uuid4().hex
    directory = output / "probes" / execution_id
    identity = hashlib.sha256(json.dumps({"action": "probe", "execution_id": execution_id,
        "chatgpt_url": chatgpt_url.rstrip("/")}, sort_keys=True).encode()).hexdigest()
    request_path, response_path = directory / "background_image_request.json", directory / "rpa_response.json"
    request = {"action": "probe", "identity": identity, "execution_id": execution_id,
        "chatgpt_url": chatgpt_url.rstrip("/"), "response_path": str(response_path), "download_dir": str(directory)}
    write_json(request_path, request)
    ticket = {"identity": identity, "execution_id": execution_id, "request_path": str(request_path)}
    write_json(output / "rpa_queue" / (execution_id + ".json"), ticket)
    state = {**ticket, "created_at": time.time(), "chatgpt_url": request["chatgpt_url"], "action": "probe",
             "consumer_sha256": CONSUMER_SHA256}
    write_json(output / "probe.json", state)
    return probe_status(storage_root)


def probe_status(storage_root: Path, *, max_age_s=120) -> dict:
    output = Path(storage_root).resolve() / "rpa"
    result = {"status": "WAITING_MANUAL", "ok": False, "desktop_loop": "UNKNOWN", "configured": True,
        "queue_dir": str(output / "rpa_queue"), "paid_requests": 0,
        "message": "等待现有影刀桌面循环领取 Factory 的无付费 probe；不会发送提示词"}
    try:
        state = json.loads((output / "probe.json").read_text())
        execution_id = state["execution_id"]
        if (state.get("action") != "probe" or not re.fullmatch(r"[a-f0-9]{32}", execution_id)
                or state.get("consumer_sha256") != CONSUMER_SHA256):
            raise ValueError("RPA_PROBE_IDENTITY_CONFLICT")
        directory = output / "probes" / execution_id
        request_path = directory / "background_image_request.json"
        request = json.loads(request_path.read_text())
        if (Path(state["request_path"]).resolve() != request_path or request.get("action") != "probe"
                or request.get("identity") != state["identity"] or request.get("execution_id") != execution_id
                or request.get("chatgpt_url") != state["chatgpt_url"]
                or Path(request["response_path"]).resolve() != directory / "rpa_response.json"
                or Path(request["download_dir"]).resolve() != directory):
            raise ValueError("RPA_PROBE_IDENTITY_CONFLICT")
        result.update(execution_id=execution_id, request_path=str(request_path), age_s=max(0, round(time.time()-state["created_at"], 1)))
        claim_path = output / "rpa_queue" / (execution_id + ".claim.json")
        if not claim_path.is_file():
            if time.time()-state["created_at"] > max_age_s:
                result.update(status="MISSING", message="无付费 probe 尚未领取且已过期；保留原票据等待桌面循环")
            return result
        claim = json.loads(claim_path.read_text())
        if (claim.get("identity") != state["identity"] or claim.get("execution_id") != execution_id
                or Path(claim["request_path"]).resolve() != request_path
                or claim.get("claimed_at", 0) < int(state["created_at"])-1):
            raise ValueError("RPA_PROBE_CLAIM_CONFLICT")
        result.update(desktop_loop="CLAIMED", claim_path=str(claim_path))
        response_path = directory / "rpa_response.json"
        if not response_path.is_file():
            return result
        response_age = time.time()-response_path.stat().st_mtime
        result["proof_age_s"] = max(0, round(response_age, 1))
        if response_age > max_age_s:
            result.update(status="MISSING", message="无付费 probe 回执已过期，不能使用历史回执声称影刀当前在线")
            return result
        response = json.loads(response_path.read_text())
        if response.get("identity") != state["identity"] or response.get("execution_id") != execution_id:
            raise ValueError("RPA_PROBE_RESPONSE_CONFLICT")
        result.update(response_path=str(response_path), desktop_loop="CONNECTED")
        if response.get("status") != "completed":
            error = str(response.get("error", "RPA_PROBE_FAILED")).partition(":")[0]
            if not re.fullmatch(r"[A-Za-z_]{1,80}", error):
                error = "RPA_PROBE_FAILED"
            result.update(status="MISSING", error_code=error, message="影刀无付费探针失败；浏览器状态保留，请检查固定对话/登录/权限")
            return result
        snapshot = response.get("snapshot") or {}
        if str(snapshot.get("url", "")).rstrip("/") != state["chatgpt_url"]:
            raise ValueError("RPA_FIXED_CHAT_MISMATCH")
        text = str(snapshot.get("text", "")).casefold()
        if any(word in text for word in ("verify you are human", "验证您是人类", "captcha", "安全验证")):
            result.update(error_code="CHATGPT_VERIFICATION_REQUIRED", message="请在现有 Chrome 固定对话人工完成验证")
        elif any(word in text for word in ("you've reached", "达到图片生成", "达到图像生成", "达到使用上限", "try again later")):
            result.update(error_code="CHATGPT_LIMIT_REACHED", message="当前 ChatGPT 页面显示使用限制，未发送提示词")
        elif not snapshot.get("editors"):
            result.update(error_code="CHATGPT_LOGIN_OR_EDITOR_REQUIRED", message="固定对话输入框不可用，请检查 ChatGPT 登录或页面加载")
        elif all(editor.get("disabled") for editor in snapshot["editors"]):
            result.update(error_code="CHATGPT_EDITOR_DISABLED", message="固定对话输入框当前不可用；未发送提示词")
        elif any(str(editor.get("input_text", "")).strip() for editor in snapshot["editors"]):
            result.update(error_code="CHATGPT_EXISTING_DRAFT", draft_present=True,
                          message="固定对话存在未发送草稿，内容已保留；需人工确认后才能自动生成")
        else:
            result.update(status="READY", ok=True, draft_present=False, message="现有影刀已领取无付费 probe，固定对话和输入框已真实确认")
        return result
    except FileNotFoundError:
        return result
    except (KeyError, OSError, ValueError, TypeError):
        result.update(status="ERROR", error_code="RPA_PROBE_EVIDENCE_INVALID",
                      message="Factory probe 身份或回执证据不一致；未确认就绪")
        return result


def refresh_probe(storage_root: Path, chatgpt_url: str, consumer_path: Path, *, wait_s=5) -> dict:
    if hashlib.sha256(Path(consumer_path).read_bytes()).hexdigest() != CONSUMER_SHA256:
        raise ValueError("RPA_UNPAID_PROBE_CONSUMER_CHANGED")
    output = Path(storage_root).resolve() / "rpa"
    reuse = False
    try:
        state = json.loads((output / "probe.json").read_text())
        execution_id = state["execution_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", execution_id):
            raise ValueError("RPA_PROBE_IDENTITY_CONFLICT")
        response_path = output / "probes" / execution_id / "rpa_response.json"
        same = state.get("action") == "probe" and state.get("chatgpt_url") == chatgpt_url.rstrip("/")
        reuse = same and (not response_path.is_file() or time.time()-response_path.stat().st_mtime < 2)
    except FileNotFoundError:
        pass
    if not reuse:
        create_probe(storage_root, chatgpt_url, consumer_path)
    deadline = time.monotonic()+min(5, max(0, wait_s))
    while True:
        result = probe_status(storage_root)
        if result.get("desktop_loop") == "CONNECTED" or result["status"] == "ERROR" or time.monotonic() >= deadline:
            return result
        time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    if args.create:
        sys.path.insert(0, str(args.legacy_root.resolve()))
        from common import load_config
        config = load_config(args.legacy_root / "config.yaml")
        result = create_probe(args.storage_root, config["rpa_chatgpt_url"], args.legacy_root / "shadowbot_chatgpt.py")
    else:
        result = probe_status(args.storage_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
