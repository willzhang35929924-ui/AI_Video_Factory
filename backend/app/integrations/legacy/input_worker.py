"""Use legacy source readers in their own Python environment, without a Pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def rows_from_specs(specs):
    rows = []
    for spec in specs:
        errors = []
        if not spec.country:
            errors.append("缺少国家；请明确填写")
        if spec.image_error:
            errors.append(spec.image_error)
        if not spec.reference_images:
            errors.append("缺少产品图片")
        if len(spec.reference_images) > 6:
            errors.append("每个任务最多支持六张产品图片")
        rows.append({"country": spec.country, "keyword": spec.keyword,
                     "product_images": [str(path.resolve()) for path in spec.reference_images],
                     "source_document": spec.source_document, "source_row": spec.source_row,
                     "context_id": spec.context_id, "status": "INVALID" if errors else "READY", "errors": errors})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--kind", choices=("excel", "tencent"), required=True)
    args = parser.parse_args()
    root, directory = args.legacy_root.resolve(), args.job_dir.resolve()
    sys.path.insert(0, str(root))
    sys.path.append(str(Path(__file__).parents[3]))
    # Imports belong here because this standalone bridge selects the old project's environment.
    from src.excel_input import read_keyword_specs
    from app.integrations.excel_mapping import mapped_workbook, worksheet_info

    request = json.loads((directory / "request.json").read_text())
    browser = None
    stage = "input_configuration"
    result = {"status": "FAILED", "rows": [], "source_document": request["source_document"], "worksheet": "首工作表"}
    try:
        if args.kind == "excel":
            metadata = mapped_workbook(directory / "source.xlsx", directory / "reader.xlsx",
                worksheet=request.get("worksheet"), mapping=request.get("column_mapping"))
            result.update(metadata)
            specs = read_keyword_specs(directory / "reader.xlsx", directory / "product_references",
                                       source_document=request["source_document"])
        else:
            from src.browser import BrowserClient
            from src.config import load_config
            from src.tencent_docs import read_tencent_keyword_specs
            from app.integrations.legacy.browser_client import native_browser_class

            config = load_config(root / "config.yaml")
            config.update(tencent_docs_url=request["source_document"],
                          tencent_docs_cache_path=directory / "source.xlsx", data_dir=directory,
                          keep_chrome_running=True)
            endpoint = urlsplit(str(config["cdp_endpoint"]))
            if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port
                    or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
                raise ValueError("EXISTING_LOCAL_CDP_REQUIRED")
            stage = "chrome_profile"
            profile = Path(config["profile_path"])
            profile = (root / profile).resolve() if not profile.is_absolute() else profile.resolve()
            if profile != (root / "chrome-profile").resolve() or not profile.is_dir():
                raise ValueError("EXISTING_CHROME_PROFILE_REQUIRED")
            config["profile_path"] = profile
            logger = logging.getLogger("factory.input")
            BrowserClient = native_browser_class(BrowserClient)
            browser = BrowserClient(config, root, logger)
            stage = "chrome_cdp"
            browser.connect()
            stage = "tencent_document"
            specs = read_tencent_keyword_specs(browser, config, logger)
            if (directory / "source.xlsx").is_file():
                result.update(worksheet_info(directory / "source.xlsx"))
            result["worksheet_tab"] = parse_qs(urlsplit(request["source_document"]).query).get("tab", ["首工作表"])[0]
        result.update(status="READY", rows=rows_from_specs(specs), message="数据已读取；尚未创建或启动任务")
        if result.get("worksheet_member"):
            for row in result["rows"]:
                identity = {"legacy_context_id": row["context_id"], "worksheet": result["worksheet"],
                    "column_mapping": result["column_mapping"]}
                row["context_id"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    except Exception as exc:
        name = type(exc).__name__
        manual = name in {"TencentDocsLoginRequired", "LoginRequired", "VerificationRequired"}
        code = name
        message = "请在保留的 Chrome 页面完成登录或验证，然后重新读取" if manual else "读取失败：" + name
        if stage == "chrome_profile":
            manual, code = True, "EXISTING_CHROME_PROFILE_REQUIRED"
            message = "原 Chrome 登录 Profile 不存在或路径不匹配；请核对旧项目 chrome-profile，未创建新 Profile"
        elif stage == "chrome_cdp":
            manual, code = True, "CHROME_CDP_UNAVAILABLE"
            message = "已有 Chrome Profile 的本地调试连接未就绪；请检查原 Chrome 窗口后重新读取，登录数据保留"
        elif str(exc) == "EXISTING_LOCAL_CDP_REQUIRED":
            code = "EXISTING_LOCAL_CDP_REQUIRED"
            message = "Chrome CDP 必须配置为本机 127.0.0.1 的 HTTP 调试端口"
        result.update(status="WAITING_MANUAL" if manual else "FAILED", error_code=code,
                      error_type=name, error_stage=stage, message=message)
    finally:
        if browser:
            browser.close()
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
