"""Explicit browser preparation through the preserved legacy profile; no collection."""
import argparse
import json
import logging
import sys
from pathlib import Path
from urllib.parse import urlsplit

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from app.integrations.legacy.browser_client import native_browser_class


def inspect_tiktok_page(page):
    url = urlsplit(page.url)
    host = (url.hostname or "").casefold()
    title = page.title()
    body = page.locator("body").inner_text(timeout=5000).strip()
    navigation = page.evaluate("""() => {
        const routes = new Set();
        for (const anchor of document.querySelectorAll('a[href]')) {
            if (!anchor.getClientRects().length) continue;
            const url = new URL(anchor.href, location.href);
            if (url.hostname === location.hostname && ['/foryou', '/following', '/explore', '/live'].includes(url.pathname)) routes.add(url.pathname);
        }
        return {navigation_routes: [...routes], visible_video: [...document.querySelectorAll('video')].some(video => video.getClientRects().length && video.readyState > 0)};
    }""")
    diagnostic = {"page_url": url._replace(query="", fragment="", netloc=host).geturl(), "page_host": host,
        "page_title": " ".join(title.split())[:160], "readable_character_count": len(body),
        "navigation_routes": navigation.get("navigation_routes", []), "visible_video": bool(navigation.get("visible_video"))}
    valid_host = url.scheme == "https" and (host == "tiktok.com" or host.endswith(".tiktok.com"))
    error = any(marker in body.casefold() for marker in ("this site can’t be reached", "this site can't be reached", "err_", "access denied", "无法访问此网站", "site is unavailable"))
    positive = valid_host and "tiktok" in title.casefold() and len(body) >= 40 and not error and (
        len(diagnostic["navigation_routes"]) >= 2 or diagnostic["visible_video"])
    return {**diagnostic, "positive_tiktok_page": positive, "login_page": valid_host and url.path.startswith("/login")}


def prepare(root):
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    from src.config import load_config
    from src.browser import BrowserClient, LoginRequired, VerificationRequired
    BrowserClient = native_browser_class(BrowserClient)

    config = load_config(root / "config.yaml")
    endpoint = urlsplit(str(config["cdp_endpoint"]))
    profile = Path(config["profile_path"]).resolve()
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.username or endpoint.password:
        raise ValueError("EXISTING_LOCAL_CDP_REQUIRED")
    if profile != (root / "chrome-profile").resolve() or not profile.is_dir():
        raise ValueError("EXISTING_CHROME_PROFILE_REQUIRED")
    config = {**config, "keep_chrome_running": True}
    browser = BrowserClient(config, root, logging.getLogger("factory.browser.prepare"))
    result = {"action": "prepare_tiktok", "profile_path": str(profile), "search_executed": False, "paid_requests": 0}
    page = None
    try:
        browser.connect()
        page = browser.open("https://www.tiktok.com/")
        diagnostic = inspect_tiktok_page(page)
        result.update(diagnostic)
        if diagnostic["login_page"] or browser._needs_tiktok_login(page):
            page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded", timeout=int(config.get("search_timeout_ms", 60000)))
            page.wait_for_timeout(int(config.get("page_wait_ms", 7000)))
            browser.preserve_manual_page(page)
            browser.check_for_verification(page)
            result.update(inspect_tiktok_page(page))
            result.update(status="WAITING_MANUAL", error_code="LOGIN_REQUIRED",
                          message="请在原 Chrome Profile 的 TikTok 登录页完成登录；该页面已保留，未搜索或提交下载")
        elif diagnostic["positive_tiktok_page"]:
            result.update(status="READY", message="TikTok 原 Profile 的真实页面与登录检查通过；主页已保留，未启动采集")
        else:
            browser.preserve_manual_page(page)
            result.update(status="WAITING_MANUAL", error_code="TIKTOK_PAGE_NOT_CONFIRMED",
                          message="未确认真实 TikTok 可用页面；请检查原 Profile 中保留的页面，未启动采集")
    except VerificationRequired:
        result.update(status="WAITING_MANUAL", error_code="CAPTCHA_REQUIRED",
                      message="请在原 Chrome Profile 的 TikTok 页面手动完成验证码；现有页面已保留，不绕过验证")
    except LoginRequired:
        result.update(status="WAITING_MANUAL", error_code="LOGIN_REQUIRED",
                      message="请在保留的 TikTok 页面手动登录，未启动采集")
    except Exception as exc:
        result.update(status="FAILED", error_code="BROWSER_PREPARE_" + type(exc).__name__.upper(),
                      message="原 Chrome Profile 准备失败；错误类型=" + type(exc).__name__)
    finally:
        browser.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.legacy_root)
    except Exception as exc:
        result = {"action": "prepare_tiktok", "status": "FAILED", "error_code": "BROWSER_PREPARE_" + type(exc).__name__.upper(),
                  "message": "原环境准备失败；错误类型=" + type(exc).__name__, "search_executed": False, "paid_requests": 0}
    temporary = args.result.with_suffix(".part")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(args.result)
    print(json.dumps({key: result[key] for key in ("action", "status", "search_executed", "paid_requests")}, ensure_ascii=False), flush=True)
    return 0 if result["status"] in {"READY", "WAITING_MANUAL"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
