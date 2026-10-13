"""Fill actual Tencent description columns using the existing safe cell writer."""
import argparse
import hashlib
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--video-auto-root", type=Path, required=True)
    args = parser.parse_args()
    directory = args.job_dir.resolve()
    request = json.loads((directory / "sync-request.json").read_text())
    sys.path.insert(0, str(args.video_auto_root.resolve()))
    from tencent_sync import _prompt_sheet, read_workbook, _column, _unliteral, _write_cell, verify_write
    target = request["source"]
    config = {"tencent_docs_url": target["source_document"], "tencent_sheet_name": target["worksheet"],
              "output_dir": directory, "tiktok_project_root": request["tiktok_auto_root"],
              "tencent_export_timeout_s": request["tencent_export_timeout_s"]}
    result = {"status": "WAITING_MANUAL", "error_code": "TENCENT_WRITE_NOT_VERIFIED"}
    stage = "CONNECT"
    try:
        with _prompt_sheet(config) as (page, sheet_name, _url, browser_config, export_xlsx):
            stage = "READ_BEFORE_WRITE"
            exported = export_xlsx(page, browser_config)
            before = read_workbook(exported)
            cells, formulas = before[sheet_name]["cells"], before[sheet_name]["formulas"]
            row_number, header_row = target["source_row"], target["header_row"]
            for key, column in target["column_map"].items():
                address = _column(column) + str(row_number)
                if _unliteral(cells.get(address, "")).strip() != target.get(key, ""):
                    raise RuntimeError("SOURCE_ROW_CHANGED:" + key)
            expected, preserved = {}, {}
            for description in request["descriptions"]:
                ordinal = description["id"]
                column = target["description_columns"].get(ordinal)
                if column is None:
                    raise RuntimeError("DESCRIPTION_COLUMN_MISSING:" + ordinal)
                header_address = _column(column) + str(header_row)
                if cells.get(header_address, "") != target["headers"][column - 1]:
                    raise RuntimeError("DESCRIPTION_HEADER_CHANGED:" + ordinal)
                address = _column(column) + str(row_number)
                current = _unliteral(cells.get(address, ""))
                if address in formulas and not current:
                    raise RuntimeError("DESCRIPTION_CELL_HAS_EMPTY_FORMULA:" + ordinal)
                if current:
                    preserved[ordinal] = current
                else:
                    expected[address] = description["text"]
            plan_path = directory / "sync-plan.json"
            journal = json.loads(plan_path.read_text()) if plan_path.exists() else {"status": "PLANNED", "source": target, "expected": expected, "preserved": preserved}
            if journal["source"] != target:
                raise RuntimeError("SYNC_PLAN_SOURCE_CHANGED")
            journal["expected"].update(expected)
            plan_path.write_text(json.dumps(journal, ensure_ascii=False), encoding="utf-8")
            backup = directory / "before-write.xlsx"
            if not backup.exists():
                backup.write_bytes(exported.read_bytes())
            for address, text in expected.items():
                stage = "WRITE:" + address
                _write_cell(page, address, text)
                journal["last_written_cell"] = address
                plan_path.write_text(json.dumps(journal, ensure_ascii=False), encoding="utf-8")
            # Export waits for the real document save state; readback is the success gate.
            stage = "EXPORT_READBACK"
            after_path = export_xlsx(page, browser_config)
            after = read_workbook(after_path)
            stage = "VERIFY_READBACK"
            verify_write(read_workbook(backup), after, sheet_name, journal["expected"])
            descriptions = []
            for ordinal, column in sorted(target["description_columns"].items()):
                text = _unliteral(after[sheet_name]["cells"].get(_column(column) + str(row_number), ""))
                if text:
                    descriptions.append({"id": ordinal, "text": text, "weight": 1, "enabled": True})
            result = {"status": "READY", "written_cells": list(journal["expected"]), "resumed_missing_cells": list(expected), "preserved_ordinals": list(preserved),
                      "descriptions": descriptions, "readback_sha256": hashlib.sha256(after_path.read_bytes()).hexdigest(),
                      "readback_path": str(after_path), "worksheet": sheet_name, "source_row": row_number}
    except (OSError, RuntimeError, ValueError) as exc:
        result.update(error_message=str(exc)[:800], stage=stage, exception_type=type(exc).__name__)
    (directory / "sync-result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
