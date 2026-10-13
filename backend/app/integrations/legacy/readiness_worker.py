"""Inspect an existing TikTok page without opening or navigating a tab."""
import json
import logging
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from browser_prepare_worker import inspect_tiktok_page


def main():
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    from src.config import load_config
    from src.browser import BrowserClient, VerificationRequired
    from src.cdp import CDPPage
    config = load_config(root / "config.yaml")
    endpoint = str(config["cdp_endpoint"]).rstrip("/")
    url = urlsplit(endpoint)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password:
        raise ValueError("EXISTING_LOCAL_CDP_REQUIRED")
    with urllib.request.urlopen(endpoint + "/json/list", timeout=3) as response:
        pages = json.load(response)
    page = next((item for item in pages if item.get("type") == "page" and ((urlsplit(item.get("url", "")).hostname or "") == "tiktok.com"
        or (urlsplit(item.get("url", "")).hostname or "").endswith(".tiktok.com"))), None)
    if not page:
        return {"status": "WAITING_MANUAL", "error_code": "TIKTOK_PAGE_MISSING", "page_count": sum(item.get("type") == "page" for item in pages),
                "message": "原 Chrome 已连接，但没有 TikTok 页面；点击恢复采集环境，复用原 Profile 打开页面后检查登录"}
    current = CDPPage(page, timeout_ms=5000)
    client = BrowserClient(config, root, logging.getLogger("readiness"))
    try:
        client._check_verification(current)
        diagnostic = inspect_tiktok_page(current)
        if diagnostic["login_page"] or client._needs_tiktok_login(current):
            return {"status": "WAITING_MANUAL", "error_code": "LOGIN_REQUIRED", "message": "TikTok 显示登录页或按钮，请在原 Chrome 窗口登录", **diagnostic}
        if not diagnostic["positive_tiktok_page"]:
            return {"status": "WAITING_MANUAL", "error_code": "TIKTOK_PAGE_NOT_CONFIRMED", "message": "当前标签页未提供真实可用 TikTok 页面证据，请检查原 Chrome 页面", **diagnostic}
        return {"status": "READY", "message": "当前 TikTok 真实页面通过登录与验证码检查", "search_executed": False, **diagnostic}
    except VerificationRequired:
        return {"status": "WAITING_MANUAL", "error_code": "CAPTCHA_REQUIRED", "message": "TikTok 需要人工完成验证码，现有页面已保留"}
    finally:
        current._transport.close()


if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:
        result = {"status": "ERROR", "message": "现有浏览器检查失败：" + type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))
