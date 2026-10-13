"""Inspect and normalize a Factory-owned workbook before using the legacy reader."""
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def worksheet_info(path, selected=None):
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = list(workbook.find(f"{{{NS}}}sheets"))
        relations = {row.attrib["Id"]: row.attrib["Target"] for row in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        names = [sheet.attrib["name"] for sheet in sheets]
        if not names or selected is not None and selected not in names:
            raise ValueError("EXCEL_WORKSHEET_NOT_FOUND")
        sheet = sheets[names.index(selected) if selected is not None else 0]
        member = relations[sheet.attrib[f"{{{REL}}}id"]]
        member = member.lstrip("/") if member.startswith("/") else posixpath.normpath(posixpath.join("xl", member))
        if not member.startswith("xl/") or member not in archive.namelist():
            raise ValueError("EXCEL_WORKSHEET_PATH_INVALID")
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(node.text or "" for node in row.iter(f"{{{NS}}}t")) for row in ET.fromstring(archive.read("xl/sharedStrings.xml"))]
        rows = []
        for row in ET.fromstring(archive.read(member)).findall(f".//{{{NS}}}sheetData/{{{NS}}}row"):
            cells = {}
            for cell in row.findall(f"{{{NS}}}c"):
                column = re.match(r"[A-Z]+", cell.attrib.get("r", ""))
                if not column:
                    continue
                value = cell.findtext(f"{{{NS}}}v") or ""
                if cell.attrib.get("t") == "s" and value:
                    value = shared[int(value)]
                elif cell.find(f"{{{NS}}}is") is not None:
                    value = "".join(node.text or "" for node in cell.iter(f"{{{NS}}}t"))
                cells[column.group()] = value
            if any(cells.values()):
                rows.append({"row": int(row.attrib.get("r", "0")), "cells": cells})
        normalize = lambda value: "".join(value.casefold().split())
        keyword_names = {"keyword", "keywords", "搜索词", "关键词", "search", "searchterm"}
        header = next((row for row in rows if any(normalize(value) in keyword_names for value in row["cells"].values())), rows[0] if rows else {"row": 0, "cells": {}})
        mapping = {"country": next((column for column, value in header["cells"].items() if normalize(value) in {"国家", "country"}), ""),
            "keyword": next((column for column, value in header["cells"].items() if normalize(value) in keyword_names), ""),
            "product_images": [column for column, value in header["cells"].items() if re.fullmatch(r"(?:photo|产品图)[1-6]", normalize(value))]}
        incompatible = []
        for key, label in (("country", "国家"), ("keyword", "keyword"), ("product_images", "产品图1–6")):
            if not mapping[key]:
                incompatible.append("未识别 " + label + " 列；请映射已有兼容列或在预览中补齐，不会自动导入为合格任务。")
        return {"worksheets": names, "worksheet": sheet.attrib["name"], "worksheet_member": member, "header_row": header["row"],
            "headers": [{"column": column, "label": label} for column, label in header["cells"].items()],
            "column_mapping": mapping, "raw_preview": [row for row in rows if row["row"] > header["row"]][:5],
            "compatibility_errors": incompatible, "row_count": sum(row["row"] > header["row"] for row in rows)}


def mapped_workbook(source, target, *, worksheet=None, mapping=None):
    metadata = worksheet_info(source, worksheet)
    if mapping is not None:
        if not isinstance(mapping, dict) or set(mapping) != {"country", "keyword", "product_images"}:
            raise ValueError("EXCEL_COLUMN_MAPPING_INVALID")
        images = mapping["product_images"]
        if not isinstance(images, list) or len(images) > 6 or not all(isinstance(value, str) for value in images):
            raise ValueError("EXCEL_IMAGE_MAPPING_INVALID")
        columns = [mapping["country"], mapping["keyword"], *images]
        available = {entry["column"] for entry in metadata["headers"]}
        if not all(isinstance(value, str) and value in available for value in columns) or len(columns) != len(set(columns)):
            raise ValueError("EXCEL_COLUMN_MAPPING_INVALID")
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for item in original.infolist():
            payload = original.read(item.filename)
            if item.filename == "xl/workbook.xml":
                root = ET.fromstring(payload)
                sheets = root.find(f"{{{NS}}}sheets")
                selected = next(sheet for sheet in sheets if sheet.attrib["name"] == metadata["worksheet"])
                sheets.remove(selected)
                sheets.insert(0, selected)
                payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            elif item.filename.endswith(".rels"):
                root = ET.fromstring(payload)
                owner = posixpath.dirname(item.filename).removesuffix("/_rels")
                for relation in root:
                    value = relation.attrib.get("Target", "")
                    if value.startswith("/") and relation.attrib.get("TargetMode") != "External":
                        relation.attrib["Target"] = posixpath.relpath(value.lstrip("/"), owner)
                payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            elif mapping is not None and item.filename == metadata["worksheet_member"]:
                root = ET.fromstring(payload)
                header = next(row for row in root.findall(f".//{{{NS}}}sheetData/{{{NS}}}row") if int(row.attrib.get("r", "0")) == metadata["header_row"])
                replacement = {mapping["country"]: "国家", mapping["keyword"]: "keyword",
                    **{column: "产品图" + str(index + 1) for index, column in enumerate(mapping["product_images"])}}
                for cell in list(header):
                    if cell.tag != f"{{{NS}}}c":
                        continue
                    column = re.match(r"[A-Z]+", cell.attrib.get("r", ""))
                    if column and column.group() in replacement:
                        for child in list(cell):
                            cell.remove(child)
                        cell.set("t", "inlineStr")
                        ET.SubElement(ET.SubElement(cell, f"{{{NS}}}is"), f"{{{NS}}}t").text = replacement[column.group()]
                    elif column:
                        # Unselected keyword/country/photo aliases must not win the legacy header lookup.
                        for child in list(cell):
                            cell.remove(child)
                        cell.set("t", "inlineStr")
                        ET.SubElement(ET.SubElement(cell, f"{{{NS}}}is"), f"{{{NS}}}t").text = "未映射 " + column.group()
                payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            output.writestr(item, payload)
    metadata["applied_mapping"] = mapping or metadata["column_mapping"]
    metadata["column_mapping"] = metadata["applied_mapping"]
    if mapping is not None:
        metadata["compatibility_errors"] = [] if mapping["product_images"] else ["此工作表没有映射产品图。需在行预览上传真实产品图片，文案/商品ID不能代替图片。"]
    return metadata
