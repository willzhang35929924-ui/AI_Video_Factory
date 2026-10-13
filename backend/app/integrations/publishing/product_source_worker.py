"""Export Tencent Docs with the existing reader, then interpret publishing columns."""
import argparse
import hashlib
import json
import logging
import posixpath
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_workbook(exported, source, shared_strings, cell_text, column_number, *, product_selection_mode="runtime_match"):
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    relns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    fields = {"国家": "country", "country": "country", "手机序号": "telephone_number", "telephone_number": "telephone_number",
              "账号名称": "account_name", "account_name": "account_name", "keyword": "keyword", "关键词": "keyword",
              "产品链接id": "product_link_id", "product_link_id": "product_link_id"}
    identity_fields = ("country", "telephone_number", "account_name", "keyword", "product_link_id")
    with zipfile.ZipFile(exported) as archive:
        shared = shared_strings(archive)
        relationships = {rel.attrib["Id"]: posixpath.normpath(posixpath.join("xl", rel.attrib["Target"].lstrip("/")))
                         for rel in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        for sheet in ET.fromstring(archive.read("xl/workbook.xml")).findall(ns + "sheets/" + ns + "sheet"):
            member = relationships[sheet.attrib[relns + "id"]]
            if member.startswith("xl/xl/"):
                member = member[3:]
            rows, columns, description_columns, header = [], {}, {}, None
            header_row = None
            for row in ET.fromstring(archive.read(member)).findall(".//" + ns + "row"):
                cells = {column_number(cell.attrib["r"]): cell_text(cell, shared).strip() for cell in row.findall(ns + "c")}
                if header is None:
                    candidate, descriptions = {}, {}
                    ambiguous = False
                    for column, value in cells.items():
                        name = "".join(value.casefold().split())
                        if name in fields:
                            key = fields[name]
                            ambiguous |= key in candidate
                            candidate[key] = column
                        match = re.fullmatch(r"(?:产品描述(?:文案)?|description_?)0?([1-9]|1[0-9]|20)", name)
                        if match:
                            number = int(match.group(1))
                            ambiguous |= number in descriptions
                            descriptions[number] = column
                    if {"country", "keyword"}.issubset(candidate) and descriptions:
                        if ambiguous:
                            raise ValueError("PUBLISHING_COLUMNS_AMBIGUOUS")
                        columns, description_columns = candidate, descriptions
                        header = [cells.get(i, "") for i in range(1, max(cells) + 1)]
                        header_row = int(row.attrib["r"])
                    continue
                values = {key: cells.get(columns.get(key), "") for key in identity_fields}
                if not any(values.values()):
                    continue
                descriptions = [{"id": str(number).zfill(2), "text": cells[column], "weight": 1, "enabled": True}
                                for number, column in sorted(description_columns.items()) if cells.get(column)]
                required = [("国家", "country"), ("keyword", "keyword")]
                if product_selection_mode == "exact_id":
                    required.append(("产品链接ID", "product_link_id"))
                errors = [label + " 缺失" for label, key in required if not values[key]]
                if not descriptions:
                    errors.append("产品描述文案缺失")
                row_number = int(row.attrib["r"])
                identity = [source, sheet.attrib["name"], row_number, *[values[key] for key in identity_fields]]
                context_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
                used_columns = set(columns.values()) | set(description_columns.values())
                rows.append({**values, "descriptions": descriptions, "source_row": row_number,
                             "source_document": source, "worksheet": sheet.attrib["name"], "context_id": context_id,
                             "header_row": header_row, "column_map": columns,
                             "description_columns": {str(number).zfill(2): column for number, column in description_columns.items()},
                             "protected_description_ordinals": [str(number).zfill(2) for number, column in description_columns.items()
                                if any(column_number(cell.attrib["r"]) == column and cell.find(ns + "f") is not None for cell in row.findall(ns + "c"))],
                             "status": "DATA_INCOMPLETE" if errors else "READY", "errors": errors,
                             "extra_description_column": any(value for column, value in cells.items() if column not in used_columns)})
            if header is not None:
                return {"status": "READY", "source_document": source, "rows": rows, "worksheet": sheet.attrib["name"], "headers": header,
                        "source_path": str(exported), "source_sha256": hashlib.sha256(exported.read_bytes()).hexdigest()}
    return {"status": "FAILED", "source_document": source, "rows": [], "error_code": "PUBLISHING_COLUMNS_NOT_FOUND",
            "message": "未找到国家/country、keyword/关键词和产品描述/description编号表头；以实际列名映射，未猜测列位置"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--product-selection-mode", choices=("runtime_match", "exact_id"), default="runtime_match")
    args = parser.parse_args()
    directory = args.job_dir.resolve()
    sys.path.insert(0, str(args.legacy_root.resolve()))
    from src.browser import BrowserClient
    from src.cdp import CDPBrowser
    from src.config import load_config
    from src.tencent_docs import _export_xlsx, _LOGIN_MARKERS, TencentDocsLoginRequired
    from src.excel_input import _shared_strings, _cell_text, _column_number

    source = json.loads((directory / "request.json").read_text())["source_document"]
    config = load_config(args.legacy_root / "config.yaml")
    config.update(tencent_docs_cache_path=directory / "source.xlsx", keep_chrome_running=True)
    browser = BrowserClient(config, args.legacy_root, logging.getLogger("publishing.product-source"))
    page = None
    result = {"status": "FAILED", "source_document": source, "rows": []}
    try:
        browser.browser = CDPBrowser(str(config["cdp_endpoint"]))
        browser.browser.connect()
        browser.context = browser.browser.contexts[0]
        page = browser.context.new_page()
        page.goto(source, timeout=60000)
        page.wait_for_timeout(int(config["page_wait_ms"]))
        body = page.locator("body").inner_text()
        if any(value in body for value in _LOGIN_MARKERS):
            browser.preserve_manual_page(page)
            raise TencentDocsLoginRequired("TENCENT_LOGIN_REQUIRED")
        exported = _export_xlsx(page, config)
        result = parse_workbook(exported, source, _shared_strings, _cell_text, _column_number, product_selection_mode=args.product_selection_mode)
    except TencentDocsLoginRequired:
        result.update(status="WAITING_MANUAL", error_code="TENCENT_LOGIN_REQUIRED", message="请在保留页面登录腾讯文档后重新读取")
    except Exception as exc:
        result.update(error_code=type(exc).__name__, message=str(exc)[:1000])
    finally:
        if page:
            browser.close_page(page)
        browser.close()
    temporary = directory / "result.part"
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(directory / "result.json")
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
