from __future__ import annotations

import copy
import hashlib
import math
import re
import threading
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber
from openpyxl.utils import get_column_letter

from .compare import compare_workbooks
from .excel import empty_cell, suggest_pairs


MAX_PAGES = 100
MAX_PAGE_PIXELS = 12_000_000
MAX_TOTAL_PIXELS = 240_000_000
MAX_CELLS = 100_000
_OCR_LOCK = threading.Lock()
_RENDER_LOCK = threading.Lock()
_OCR_ENGINE = None


def _recognize(image) -> list[dict]:
    global _OCR_ENGINE
    import cv2
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    with _OCR_LOCK:
        if _OCR_ENGINE is None:
            _OCR_ENGINE = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2,
                                   det_use_cuda=False, cls_use_cuda=False, rec_use_cuda=False)
        result, _ = _OCR_ENGINE(cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR))
    words = []
    for box, text, confidence in result or []:
        if not str(text).strip():
            continue
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
        words.append({"text": str(text).strip(), "bbox": [min(xs), min(ys), max(xs), max(ys)],
                      "confidence": float(confidence),
                      "angle": math.degrees(math.atan2(box[1][1] - box[0][1], box[1][0] - box[0][0]))})
    return words


def _source(page: int, bbox, width: float, height: float, method: str, confidence=None) -> dict:
    x0, y0, x1, y1 = bbox
    result = {"kind": "pdf", "page": page,
              "bbox": [max(0.0, min(1.0, v)) for v in (x0 / width, y0 / height, x1 / width, y1 / height)],
              "method": method}
    if confidence is not None:
        result["confidence"] = confidence
    return result


def _text_source(page_image, page_number: int, bbox) -> dict:
    x0, y0, x1, y1 = bbox
    bx, by = page_image.bbox[:2]
    scale = page_image.scale
    return _source(page_number, ((x0 - bx) * scale, (y0 - by) * scale,
                                  (x1 - bx) * scale, (y1 - by) * scale),
                   *page_image.original.size, "text")


def _decimal_text(text: str) -> str | None:
    value = text.strip().replace("−", "-")
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]
    if "," in value:
        if not re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", value):
            return None
        value = value.replace(",", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return str(number) if number.is_finite() else None


def _make_sheet(name: str, matrix: list[list[tuple[str | None, dict]]], *, structure: str,
                page_number: int, method: str) -> dict:
    width = max((len(row) for row in matrix), default=1)
    cells = {}
    for row_number, row in enumerate(matrix, 1):
        for column, (text, source) in enumerate(row, 1):
            cell = empty_cell(row_number, column)
            raw = "" if text is None else str(text).strip()
            cell.update(value=raw or None, display=raw, raw_text=raw, value_type="string" if raw else "blank",
                        pdf_source=source)
            header = str(matrix[0][column - 1][0] or "") if column <= len(matrix[0]) else ""
            is_identifier = column == 1 or any(part in header.lower() for part in ("编号", "编码", "代码", "id", "code"))
            if row_number > 1 and not is_identifier and structure == "table":
                numeric = _decimal_text(raw)
                if numeric is not None:
                    cell.update(value=numeric, value_type="number")
            cells[cell["address"]] = cell
    return {"name": name, "max_row": max(len(matrix), 1), "max_column": width,
            "hidden": False, "suggested_header_row": 1,
            "nonempty_count": sum(c["value"] is not None for c in cells.values()),
            "cells": cells, "merged_ranges": [], "hidden_rows": [], "hidden_columns": [],
            "structure": structure, "page_number": page_number, "method": method}


def _inside(bbox, outer) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def _native_lines(words: list[dict]) -> list[dict]:
    lines = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(word["top"] - lines[-1]["top"]) <= 3:
            lines[-1]["words"].append(word)
        else:
            lines.append({"top": word["top"], "words": [word]})
    result = []
    for line in lines:
        row = sorted(line["words"], key=lambda w: w["x0"])
        result.append({"text": " ".join(w["text"] for w in row),
                       "bbox": [min(w["x0"] for w in row), min(w["top"] for w in row),
                                max(w["x1"] for w in row), max(w["bottom"] for w in row)]})
    return result


def _line_centres(indices) -> list[int]:
    groups = []
    for value in indices:
        if groups and value - groups[-1][-1] <= 3:
            groups[-1].append(int(value))
        else:
            groups.append([int(value)])
    return [round(sum(group) / len(group)) for group in groups]


def _ocr_lines(words: list[dict]) -> list[dict]:
    lines = []
    for word in sorted(words, key=lambda w: (w["bbox"][1], w["bbox"][0])):
        box = word["bbox"]
        centre = (box[1] + box[3]) / 2
        if lines and abs(centre - lines[-1]["centre"]) <= min(box[3] - box[1], lines[-1]["height"]) / 2:
            lines[-1]["words"].append(word)
        else:
            lines.append({"centre": centre, "height": box[3] - box[1], "words": [word]})
    result = []
    for line in lines:
        row = sorted(line["words"], key=lambda w: w["bbox"][0])
        result.append({"text": " ".join(w["text"] for w in row),
                       "bbox": [min(w["bbox"][0] for w in row), min(w["bbox"][1] for w in row),
                                max(w["bbox"][2] for w in row), max(w["bbox"][3] for w in row)],
                       "confidence": min(w["confidence"] for w in row)})
    return result


def _ocr_tables(image, words: list[dict]) -> tuple[list[dict], set[int]]:
    import cv2
    import numpy as np

    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    height, width = gray.shape
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, width // 35), 1)))
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, height // 35))))
    contours, _ = cv2.findContours(cv2.bitwise_or(horizontal, vertical), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < 100 or h < 50:
            continue
        xs = _line_centres(np.flatnonzero(np.count_nonzero(vertical[y:y + h, x:x + w], axis=0) >= h * .6))
        ys = _line_centres(np.flatnonzero(np.count_nonzero(horizontal[y:y + h, x:x + w], axis=1) >= w * .6))
        if not (3 <= len(xs) <= 61 and 3 <= len(ys) <= 501):
            continue
        xs, ys = [x + v for v in xs], [y + v for v in ys]
        if min(b - a for a, b in zip(xs, xs[1:])) < 12 or min(b - a for a, b in zip(ys, ys[1:])) < 10:
            continue
        matrix = [[[] for _ in range(len(xs) - 1)] for _ in range(len(ys) - 1)]
        used = set()
        unreliable = False
        for index, word in enumerate(words):
            box = word["bbox"]
            if not _inside(box, (xs[0], ys[0], xs[-1], ys[-1])):
                continue
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            col = next((i for i in range(len(xs) - 1) if xs[i] <= cx <= xs[i + 1]), None)
            row = next((i for i in range(len(ys) - 1) if ys[i] <= cy <= ys[i + 1]), None)
            if col is None or row is None:
                continue
            if box[0] < xs[col] - 5 or box[2] > xs[col + 1] + 5 or box[1] < ys[row] - 5 or box[3] > ys[row + 1] + 5:
                unreliable = True
                break
            matrix[row][col].append(word)
            used.add(index)
        occupied = sum(bool(cell) for row in matrix for cell in row)
        if unreliable or occupied < 4 or not all(matrix[0]):
            continue
        candidates.append({"xs": xs, "ys": ys, "matrix": matrix, "used": used})
    candidates.sort(key=lambda c: (c["ys"][0], c["xs"][0]))
    used = set().union(*(c["used"] for c in candidates)) if candidates else set()
    return candidates, used


def _logical_tables(sheets: list[dict], warnings: list[str]) -> list[dict]:
    groups = defaultdict(list)
    for sheet in sheets:
        if sheet["structure"] != "table":
            continue
        signature = tuple(sheet["cells"].get(f"{get_column_letter(col)}1", {}).get("display", "")
                          for col in range(1, sheet["max_column"] + 1))
        groups[signature].append(sheet)
    logical = []
    for signature, members in groups.items():
        digest = hashlib.sha256("\x1f".join(signature).encode()).hexdigest()[:8]
        name = f"报表候选 · {' / '.join(signature)[:45]} · {digest}"
        sheet = copy.deepcopy(members[0])
        sheet.update(name=name, member_sheets=[m["name"] for m in members], candidate_merge=len(members) > 1)
        next_row = sheet["max_row"] + 1
        for member in members[1:]:
            for cell in member["cells"].values():
                if cell["row"] == 1:
                    continue
                copied = copy.deepcopy(cell)
                copied["row"] = next_row + cell["row"] - 2
                copied["address"] = f"{get_column_letter(cell['column'])}{copied['row']}"
                sheet["cells"][copied["address"]] = copied
            next_row += member["max_row"] - 1
        sheet["max_row"] = next_row - 1
        sheet["nonempty_count"] = sum(c["value"] is not None for c in sheet["cells"].values())
        sheet["method"] = "ocr" if any(m["method"] == "ocr" for m in members) else "text"
        if len(members) > 1:
            pages = "、".join(str(m["page_number"]) for m in members)
            warnings.append(f"第 {pages} 页相同表头已拼接为候选报表，请在配置预览确认；不同业务表可能具有相同表头。")
        logical.append(sheet)
    return logical


def _bad_native_text(text: str) -> bool:
    return bool(re.search(r"\(cid:\d+\)|\ufffd|\x00", text))


def _pixel_box(page_image, box) -> list[float]:
    return [(value - page_image.bbox[index % 2]) * page_image.scale for index, value in enumerate(box)]


def _overlap_fraction(box, outer) -> float:
    area = max(0, box[2] - box[0]) * max(0, box[3] - box[1])
    intersection = max(0, min(box[2], outer[2]) - max(box[0], outer[0])) * max(0, min(box[3], outer[3]) - max(box[1], outer[1]))
    return intersection / area if area else 0


def _combined_lines(words: list[dict]) -> list[dict]:
    groups = []
    for word in sorted(words, key=lambda w: (w["bbox"][1], w["bbox"][0])):
        box = word["bbox"]
        centre, height = (box[1] + box[3]) / 2, box[3] - box[1]
        if groups and abs(centre - groups[-1]["centre"]) <= min(height, groups[-1]["height"]) / 2:
            groups[-1]["words"].append(word)
        else:
            groups.append({"centre": centre, "height": height, "words": [word]})
    lines = []
    for group in groups:
        row = sorted(group["words"], key=lambda w: w["bbox"][0])
        lines.append({"text": " ".join(w["text"] for w in row), "words": row,
                      "bbox": [min(w["bbox"][0] for w in row), min(w["bbox"][1] for w in row),
                               max(w["bbox"][2] for w in row), max(w["bbox"][3] for w in row)]})
    return lines


def _preserve_table_geometry(sheet, table, page_image, page_number):
    boxes = [box for row in table.rows for box in row.cells if box is not None]
    xs = sorted({round(value, 4) for box in boxes for value in (box[0], box[2])})
    ys = sorted({round(value, 4) for box in boxes for value in (box[1], box[3])})
    geometry, merged = [], set()
    for row_number, row in enumerate(table.rows, 1):
        for column, box in enumerate(row.cells, 1):
            if box is None:
                continue
            c1, c2 = xs.index(round(box[0], 4)) + 1, xs.index(round(box[2], 4))
            r1, r2 = ys.index(round(box[1], 4)) + 1, ys.index(round(box[3], 4))
            entry = {"cell": f"{get_column_letter(column)}{row_number}", "bbox": _text_source(page_image, page_number, box)["bbox"],
                     "row_start": r1, "row_end": r2, "column_start": c1, "column_end": c2}
            if c2 <= sheet["max_column"] and r2 <= sheet["max_row"] and (c2 > c1 or r2 > r1):
                name = f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}"
                entry["merged_range"] = name
                merged.add(name)
                for r in range(r1, r2 + 1):
                    for c in range(c1, c2 + 1):
                        cell = sheet["cells"].get(f"{get_column_letter(c)}{r}")
                        if cell:
                            cell["merged_range"] = name
            elif c2 > sheet["max_column"] or r2 > sheet["max_row"]:
                entry["span_unresolved"] = True
            geometry.append(entry)
    sheet["cell_geometry"] = geometry
    sheet["merged_ranges"] = sorted(merged)


def _region_ocr(page, page_image, native_words: list[dict], report) -> tuple[list[dict], list[dict], list[dict]]:
    import numpy as np
    from PIL import ImageDraw

    image = page_image.original
    width, height = image.size
    words = [{"text": w["text"], "bbox": _pixel_box(page_image, (w["x0"], w["top"], w["x1"], w["bottom"])),
              "method": "text", "confidence": 1.0} for w in native_words if not _bad_native_text(w["text"])]
    residual = image.copy()
    draw = ImageDraw.Draw(residual)
    for word in words:
        x0, y0, x1, y1 = word["bbox"]
        draw.rectangle((x0 - 2, y0 - 2, x1 + 2, y1 + 2), fill="white")
    for line in page.lines:
        box = _pixel_box(page_image, (line["x0"], line["top"], line["x1"], line["bottom"]))
        padding = max(3, math.ceil(float(line.get("linewidth") or 0) * page_image.scale / 2) + 2)
        draw.rectangle((box[0] - padding, box[1] - padding, box[2] + padding, box[3] + padding), fill="white")
    for rect in page.rects:
        box = _pixel_box(page_image, (rect["x0"], rect["top"], rect["x1"], rect["bottom"]))
        padding = max(3, math.ceil(float(rect.get("linewidth") or 0) * page_image.scale / 2) + 2)
        draw.rectangle((box[0] - padding, box[1] - padding, box[2] + padding, box[3] + padding),
                       outline="white", fill="white" if rect.get("fill") else None, width=padding * 2 + 1)
    candidates = []
    for item in page.images:
        candidates.append({"bbox": _pixel_box(page_image, (item["x0"], item["top"], item["x1"], item["bottom"])), "reasons": {"image"}})
    for word in native_words:
        if _bad_native_text(word["text"]):
            box = _pixel_box(page_image, (word["x0"], word["top"], word["x1"], word["bottom"]))
            candidates.append({"bbox": [box[0] - 4, box[1] - 4, box[2] + 4, box[3] + 4], "reasons": {"invalid_text"}})
    if not candidates and sum(len(w["text"]) for w in words) < 12:
        ink = np.asarray(residual.convert("L")) < 180
        ys, xs = np.nonzero(ink)
        if len(xs) > 16:
            candidates.append({"bbox": [float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)], "reasons": {"unmapped_ink"}})
    regions = []
    for candidate in candidates:
        box = candidate["bbox"]
        candidate["bbox"] = [max(0, math.floor(box[0])), max(0, math.floor(box[1])), min(width, math.ceil(box[2])), min(height, math.ceil(box[3]))]
        if candidate["bbox"][2] <= candidate["bbox"][0] or candidate["bbox"][3] <= candidate["bbox"][1]:
            continue
        merged = True
        while merged:
            merged = False
            for prior in list(regions):
                a, b = candidate["bbox"], prior["bbox"]
                if a[0] <= b[2] + 3 and a[2] >= b[0] - 3 and a[1] <= b[3] + 3 and a[3] >= b[1] - 3:
                    candidate["bbox"] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    candidate["reasons"] |= prior["reasons"]
                    regions.remove(prior)
                    merged = True
        regions.append(candidate)
    diagnostics, annotations = [], []
    for index, region in enumerate(regions, 1):
        box = region["bbox"]
        crop = residual.crop(box)
        ink_pixels = int((np.asarray(crop.convert("L")) < 180).sum())
        reason = "invalid_text" if "invalid_text" in region["reasons"] else "image" if "image" in region["reasons"] else "unmapped_ink"
        source = _source(page.page_number, box, width, height, "ocr")
        detail = {"id": f"p{page.page_number}-ocr-{index}", "bbox": source["bbox"], "reason": reason,
                  "status": "empty", "ink_pixels": ink_pixels, "affects_content": reason == "invalid_text" or ink_pixels > 16}
        diagnostics.append(detail)
        report("ocr", page.page_number, page.page_number - 1)
        try:
            recognized = _recognize(crop)
        except Exception as exc:
            detail.update(status="failed", error=f"区域 OCR 未完成（{type(exc).__name__}）")
            continue
        accepted = []
        for item in recognized:
            mapped = {**item, "bbox": [item["bbox"][0] + box[0], item["bbox"][1] + box[1], item["bbox"][2] + box[0], item["bbox"][3] + box[1]],
                      "method": "ocr", "region_id": detail["id"]}
            if 12 < abs(mapped.get("angle", 0)) < 78:
                annotations.append({**mapped, "role": "oblique_graphic_text"})
                continue
            if any(_overlap_fraction(mapped["bbox"], prior["bbox"]) > .5 or
                   (_overlap_fraction(prior["bbox"], mapped["bbox"]) > .5 and re.sub(r"\s+", "", prior["text"]) in re.sub(r"\s+", "", mapped["text"]))
                   for prior in words):
                continue
            accepted.append(mapped)
            words.append(mapped)
        detail["text_count"] = len(accepted)
        if accepted:
            detail["status"] = "ok"
        elif not ink_pixels or (annotations and reason == "image"):
            detail["affects_content"] = False
    for native in native_words:
        if not _bad_native_text(native["text"]):
            continue
        box = _pixel_box(page_image, (native["x0"], native["top"], native["x1"], native["bottom"]))
        recovered = sum(_overlap_fraction(box, word["bbox"]) for word in words if word["method"] == "ocr") >= .65
        if recovered:
            continue
        region_ids = []
        for region in diagnostics:
            pixel_region = [v * (width if i % 2 == 0 else height) for i, v in enumerate(region["bbox"])]
            if _overlap_fraction(box, pixel_region) > .5:
                region.update(status="unresolved", affects_content=True)
                region_ids.append(region["id"])
        words.append({"text": native["text"], "bbox": box, "method": "text", "confidence": 1.0,
                      "unresolved_text": True, "region_ids": region_ids})
    return words, diagnostics, annotations


def extract_pdf(path: Path, asset_dir: Path, progress=None) -> dict:
    asset_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    sheets = []
    warnings = []
    total_pixels = 0
    try:
        pdf = pdfplumber.open(path)
    except Exception as exc:
        raise ValueError("无法打开 PDF；文件可能损坏或已加密。") from exc
    from .pdf_glyphs import PdfGlyphGeometry

    with pdf, PdfGlyphGeometry(path, lock=_RENDER_LOCK) as glyph_geometry:
        page_count = len(pdf.pages)

        def report(stage, current=0, completed=0):
            if progress is not None:
                progress({"stage": stage, "unit": "page", "current": current, "total": page_count, "completed": completed,
                          **({"item_name": f"第 {current} 页"} if current else {})})

        report("opening")
        if not page_count or page_count > MAX_PAGES:
            raise ValueError(f"PDF 必须包含 1 至 {MAX_PAGES} 页，请拆分后再比较。")
        for page in pdf.pages:
            number = page.page_number
            report("rendering", number, number - 1)
            area = float(page.width) * float(page.height)
            if not math.isfinite(area) or area <= 0:
                raise ValueError(f"第 {number} 页尺寸无效。")
            resolution = min(144.0, 72.0 * math.sqrt(MAX_PAGE_PIXELS / area))
            if resolution < 36:
                raise ValueError(f"第 {number} 页尺寸过大，无法可靠渲染和定位。")
            projected_pixels = math.ceil(page.width * resolution / 72) * math.ceil(page.height * resolution / 72)
            if total_pixels + projected_pixels > MAX_TOTAL_PIXELS:
                raise ValueError("PDF 累计渲染像素超过 2.4 亿，请拆分后比较。")
            try:
                with _RENDER_LOCK:
                    page_image = page.to_image(resolution=resolution, antialias=True, force_mediabox=True)
                image = page_image.original
                image.save(asset_dir / f"page-{number}.png")
            except Exception as exc:
                raise ValueError(f"第 {number} 页无法渲染，不能保证原文定位。") from exc
            iw, ih = image.size
            total_pixels += iw * ih
            if iw * ih > MAX_PAGE_PIXELS + 10000 or total_pixels > MAX_TOTAL_PIXELS + 10000:
                raise ValueError("PDF 渲染尺寸超过限制，请拆分后比较。")
            report("text_extract", number, number - 1)
            native_words = page.extract_words()
            if len(native_words) > 20000:
                raise ValueError(f"第 {number} 页文字对象过多，请缩小范围。")
            words, regions, annotations = _region_ocr(page, page_image, native_words, report)
            for index, word in enumerate(words):
                word["token_id"] = index
            native_lines = [{"text": line["text"], **_text_source(page_image, number, line["bbox"])} for line in _native_lines(native_words)]
            evidence_lines = []
            for line in _combined_lines(words):
                segments = []
                for word in line["words"]:
                    if segments and segments[-1][0]["method"] == word["method"]:
                        segments[-1].append(word)
                    else:
                        segments.append([word])
                for segment in segments:
                    box = [min(w["bbox"][0] for w in segment), min(w["bbox"][1] for w in segment),
                           max(w["bbox"][2] for w in segment), max(w["bbox"][3] for w in segment)]
                    evidence_lines.append({"text": " ".join(w["text"] for w in segment),
                                           **_source(number, box, iw, ih, segment[0]["method"])})
            has_ocr = any(w["method"] == "ocr" for w in words) or any(r["affects_content"] for r in regions)
            metadata = {"number": number, "width": float(page.width), "height": float(page.height),
                        "image_width": iw, "image_height": ih, "method": "ocr" if has_ocr else "text",
                        "status": "ok", "ocr_unverified": has_ocr, "ocr_regions": regions,
                        "native_lines": native_lines, "evidence_lines": evidence_lines,
                        "native_text": "\n".join(line["text"] for line in native_lines),
                        "ocr_annotations": [{**item, "bbox": _source(number, item["bbox"], iw, ih, "ocr")["bbox"]} for item in annotations],
                        "rotation": page.rotation, "media_box": list(page.mediabox), "crop_box": list(page.cropbox)}
            metadata["has_graphics"] = bool(page.lines or page.rects or page.curves or page.images)
            from .pdf_appearance import extract_page_appearance

            metadata["appearance"] = extract_page_appearance(page, page_image)
            appearance = metadata["appearance"]
            glyphs = glyph_geometry.resolve(page, appearance["chars"])
            corrections = []
            for char in appearance["chars"]:
                evidence = glyphs["chars"][char["id"]]
                if evidence["status"] == "verified" and evidence["metric_bbox_conflict"]:
                    char["glyph_bbox"] = evidence["glyph_bbox"]
                    corrections.append({"char_id": char["id"], "text": char["text"], "layout_bbox": char["bbox"], **evidence})
            appearance["glyph_geometry"] = {"renderer": glyphs["renderer"], "origin_tolerance_pt": glyphs["origin_tolerance_pt"],
                                            "summary": glyphs["summary"], "corrections": corrections,
                                            "unverified": [{"char_id": key, "reason": evidence["reason"]} for key, evidence in glyphs["chars"].items()
                                                           if evidence["status"] == "unverified"]}
            metadata["physical_width"] = metadata["appearance"]["width_pt"]
            metadata["physical_height"] = metadata["appearance"]["height_pt"]
            pages.append(metadata)
            page_sheets = []
            used = set()
            native_boxes = []
            report("table_extract", number, number - 1)

            def cell_source(box, local_words, method="text", unresolved=False):
                ocr = [word for word in local_words if word["method"] == "ocr"]
                source = _source(number, box, iw, ih, "ocr" if ocr else method,
                                 min((word["confidence"] for word in ocr), default=None))
                source["content_sources"] = [_source(number, word["bbox"], iw, ih, word["method"],
                                                       word.get("confidence") if word["method"] == "ocr" else None)
                                             for word in local_words]
                source["region_ids"] = list(dict.fromkeys(region_id for word in local_words
                                            for region_id in ([word["region_id"]] if word.get("region_id") else word.get("region_ids", []))))
                if unresolved or any(word.get("unresolved_text") for word in local_words):
                    source["unresolved_text"] = True
                return source

            for index, table in enumerate(page.find_tables(), 1):
                extracted = table.extract()
                if len(extracted) < 2 or max((len(row) for row in extracted), default=0) < 2:
                    continue
                matrix = []
                for row_index, row in enumerate(extracted):
                    output = []
                    for column, native_text in enumerate(row):
                        native_box = table.rows[row_index].cells[column]
                        box = _pixel_box(page_image, native_box or table.bbox)
                        local = [word for word in words if native_box and
                                 (_inside(word["bbox"], box) if word["method"] == "text" else _overlap_fraction(word["bbox"], box) >= .8)]
                        has_regional_text = any(word["method"] == "ocr" for word in local)
                        invalid = _bad_native_text(native_text or "")
                        value = native_text
                        if has_regional_text:
                            value = "\n".join(line["text"] for line in _combined_lines(local))
                        unresolved = invalid and not has_regional_text
                        used.update(word["token_id"] for word in local)
                        output.append((value, cell_source(box, local, unresolved=unresolved)))
                    matrix.append(output)
                if not any(value for row in matrix for value, _ in row):
                    continue
                sheet = _make_sheet(f"第 {number} 页 · 表 {index}", matrix, structure="table", page_number=number,
                                    method="ocr" if any(src["method"] == "ocr" or src.get("unresolved_text") for row in matrix for _, src in row) else "text")
                box = _source(number, _pixel_box(page_image, table.bbox), iw, ih, "text")["bbox"]
                sheet.update(bbox=box, structure_source={"method": "pdf_lines", "bbox": box})
                _preserve_table_geometry(sheet, table, page_image, number)
                for cell in sheet["cells"].values():
                    if cell["pdf_source"].get("unresolved_text"):
                        cell["requires_review"] = True
                page_sheets.append(sheet)
                native_boxes.append(_pixel_box(page_image, table.bbox))
                if any(box is None for row in table.rows for box in row.cells):
                    warnings.append(f"第 {number} 页表 {index} 含合并或不规则单元格，请确认表头和列映射。")
            if any(word["method"] == "ocr" for word in words):
                tables, _ = _ocr_tables(image, words)
                for table in tables:
                    table_box = [table["xs"][0], table["ys"][0], table["xs"][-1], table["ys"][-1]]
                    if any(_overlap_fraction(table_box, native) > .5 or _overlap_fraction(native, table_box) > .5 for native in native_boxes):
                        continue
                    matrix = []
                    for r, row in enumerate(table["matrix"]):
                        output = []
                        for c, local in enumerate(row):
                            box = (table["xs"][c], table["ys"][r], table["xs"][c + 1], table["ys"][r + 1])
                            value = "\n".join(line["text"] for line in _combined_lines(local))
                            output.append((value, cell_source(box, local, method="ocr")))
                            used.update(word["token_id"] for word in local)
                        matrix.append(output)
                    sheet = _make_sheet(f"第 {number} 页 · 图像表 {len(page_sheets) + 1}", matrix, structure="table", page_number=number, method="ocr")
                    box = _source(number, table_box, iw, ih, "ocr")["bbox"]
                    sheet.update(bbox=box, structure_source={"method": "image_lines", "bbox": box})
                    page_sheets.append(sheet)
            outside = [word for word in words if word["token_id"] not in used]
            if outside:
                matrix = [[("文本内容", _source(number, (0, 0, iw, ih), iw, ih, "text"))]]
                for line in _combined_lines(outside):
                    matrix.append([(line["text"], cell_source(line["bbox"], line["words"]))])
                body = _make_sheet(f"第 {number} 页 · 正文", matrix, structure="text_blocks", page_number=number,
                                   method="ocr" if any(word["method"] == "ocr" or word.get("unresolved_text") for word in outside) else "text")
                body.update(bbox=[0, 0, 1, 1], structure_source={"method": "text_lines", "reliable_table": False})
                page_sheets.append(body)
            if has_ocr and not any(sheet["structure"] == "table" for sheet in page_sheets):
                metadata["structure_unconfirmed"] = True
                warnings.append(f"第 {number} 页存在区域 OCR 内容；尚未确认其行列结构，仍保留完整原页、文字与视觉比较证据。")
            if not words and any(region["affects_content"] for region in regions):
                metadata.update(status="unavailable", error="区域识别未取得可比较文字")
            if not words and not regions and not metadata["has_graphics"]:
                metadata["blank"] = True
            for region in regions:
                if region["status"] != "ok" and region["affects_content"]:
                    warnings.append(f"第 {number} 页的 {region['id']} 区域文字未可靠恢复，已保留其余原生文字并标记区域待核对。")
            sheets.extend(page_sheets)
            for sheet in page_sheets:
                for cell in sheet["cells"].values():
                    if cell.get("pdf_source", {}).get("unresolved_text"):
                        cell["requires_review"] = True
            if sum(len(sheet["cells"]) for sheet in sheets) > MAX_CELLS // 2:
                raise ValueError("PDF 提取单元格过多，请拆分后比较。")
            page.close()
            report("table_extract", number, number)
    from .reports import attach_report_inventory

    result = attach_report_inventory({"sheets": sheets, "warnings": list(dict.fromkeys(warnings)), "pages": pages, "format": "pdf",
                                      "document_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "report_inventory_version": 2})
    report("file_completed", page_count, page_count)
    return result


def suggest_pdf_pairs(old: dict, new: dict) -> list[dict]:
    if old.get("report_inventory_version") or new.get("report_inventory_version"):
        from .reports import suggest_report_pairs

        return suggest_report_pairs(old, new)
    filtered = []
    for workbook in (old, new):
        filtered.append({"sheets": [sheet for sheet in workbook["sheets"]
                                    if sheet.get("member_sheets") or sheet.get("structure") != "table"]})
    pairs = suggest_pairs(*filtered)
    for pair in pairs:
        selected = [sheet for book in filtered for sheet in book["sheets"]
                    if sheet["name"] in (pair.get("old_sheet"), pair.get("new_sheet"))]
        if any(sheet.get("structure") == "text_blocks" for sheet in selected):
            pair["mode"] = "position"
            for column in pair["columns"]:
                column["is_key"] = False
    return pairs


def _page_verified(book: dict, number: int) -> bool:
    page = next((p for p in book.get("pages", []) if p["number"] == number), None)
    return bool(page and page["status"] == "ok" and not page.get("structure_unconfirmed") and
                (page["method"] != "ocr" or page.get("reviewed")))


def _cell_verified(book: dict, cell: dict | None) -> bool:
    if cell is None:
        return False
    source = cell.get("pdf_source", {})
    return (source.get("method") != "ocr" and not cell.get("requires_review")) or bool(cell.get("reviewed") and _page_verified(book, source["page"]))


def _compare_report_pdfs(old: dict, new: dict, config: dict) -> dict:
    from .reports import attach_report_inventory, compare_inventory

    books = {side: book if book.get("report_inventory_version") == 1 else attach_report_inventory(book)
             for side, book in (("old", old), ("new", new))}
    inventory = compare_inventory(books["old"], books["new"], config)
    if inventory["pairs"]:
        result = compare_workbooks(books["old"], books["new"], {**config, "pairs": inventory["pairs"]})
    else:
        result = {"diffs": [], "summary": {"matched_rows": 0, "unchanged_rows": 0, "compared_cells": 0},
                  "coverage": {"compared_sheets": {"old": [], "new": []},
                               "excluded_sheets": {side: [s["name"] for s in book["sheets"]] for side, book in books.items()},
                               "warnings": list(dict.fromkeys(books["old"].get("warnings", []) + books["new"].get("warnings", [])))}}
    for diff in result["diffs"]:
        diff.update(report_id=inventory["pair_report_ids"].get(diff["table_id"]),
                    layer="uncertainty" if diff["severity"] == "issue" else "content")
        for side, book in books.items():
            source = diff.get(f"{side}_source")
            if not source:
                continue
            sheet = next(sheet for sheet in book["sheets"] if sheet["name"] == source["sheet"])
            cell = sheet["cells"].get(source["cell"])
            if cell and cell.get("pdf_source"):
                source.update(copy.deepcopy(cell["pdf_source"]))
                if source.get("method") == "ocr" or cell.get("requires_review"):
                    source.update(reviewed=bool(cell.get("reviewed")), review_id=cell.get("review_id"))
            else:
                row_number = int(re.search(r"\d+$", source["cell"]).group())
                neighbor = next((c for c in sheet["cells"].values() if c["row"] == row_number and c.get("pdf_source")), None)
                source.update(kind="pdf", page=neighbor["pdf_source"]["page"] if neighbor else sheet["page_number"],
                              bbox=[0, 0, 1, 1], method=sheet["method"], location_precision="page")
        if diff["severity"] == "issue":
            sources = [{**diff[f"{side}_source"], "side": side} for side in ("old", "new") if diff.get(f"{side}_source")]
            inventory["issues"].append({"id": f"issue-content-{diff['id']}", "diff_id": diff["id"], "type": "content_matching",
                                         "message": diff["message"], "report_ids": [diff["report_id"]],
                                         "pages": sorted({source["page"] for source in sources}), "sources": sources, "actions": ["review_pairing"]})
            entry = next(row for row in inventory["reports"] if row["id"] == diff["report_id"])
            entry["content_status"] = "pending"
            entry["blocking_reasons"].append(diff["message"])
    for entry in inventory["reports"]:
        if entry["confirmation"] != "confirmed" or entry["presence"] not in ("old_only", "new_only"):
            continue
        removed = entry["presence"] == "old_only"
        report = entry["old"] if removed else entry["new"]
        result["diffs"].append({"id": "", "kind": "report_removed" if removed else "report_added", "table_id": entry["id"],
                                "table_name": entry["title"], "row_key": entry["object"] or entry["title"], "field": "报表完整性",
                                "old_value": entry["title"] if removed else None, "new_value": None if removed else entry["title"],
                                "delta": None, "message": entry["basis"], "severity": "change", "report_id": entry["id"], "layer": "completeness",
                                "old_source": report["source"] if removed else None, "new_source": None if removed else report["source"]})
    for side, book in books.items():
        for page in book.get("pages", []):
            for region in page.get("ocr_regions", []):
                unresolved = region.get("affects_content") and region.get("status") != "ok"
                unchecked = region.get("status") == "ok" and region.get("text_count", 0) > 0 and not page.get("reviewed")
                if not unresolved and not unchecked:
                    continue
                issue_id = f"issue-{side}-{region['id']}"
                if any(issue["id"] == issue_id for issue in inventory["issues"]):
                    continue
                inventory["issues"].append({"id": issue_id,
                    "type": "extraction_unavailable" if unresolved and page.get("status") == "unavailable" else "region_unresolved" if unresolved else "ocr_review",
                    "message": "此区域文字尚未可靠恢复；原生文字和原图已保留，请校正或重新提取。" if unresolved else "此区域包含尚未人工核验的 OCR 文字。",
                    "report_ids": [], "side": side, "pages": [page["number"]],
                    "sources": [{"kind": "pdf", "side": side, "page": page["number"], "bbox": region["bbox"],
                                 "method": "ocr", "region_id": region["id"]}],
                    "actions": ["reprocess", "review_structure"] if unresolved else ["review_ocr"]})
    if not inventory["reports"]:
        inventory["issues"].append({"id": "issue-no-reports", "type": "inventory", "message": "未识别出可比较的业务报表，不能据此判定报表一致。",
                                     "report_ids": [], "pages": [], "sources": [], "actions": ["review_identity"]})
    for issue in inventory["issues"]:
        if issue.get("diff_id"):
            continue
        sources = {side: next((source for source in issue.get("sources", []) if source.get("side", issue.get("side")) == side), None)
                   for side in ("old", "new")}
        report_id = next(iter(issue.get("report_ids", [])), None)
        entry = next((row for row in inventory["reports"] if row["id"] == report_id), None)
        result["diffs"].append({"id": "", "kind": "report_review", "table_id": report_id or issue["id"],
                                "table_name": entry["title"] if entry else "文档范围核对", "row_key": entry["object"] if entry else "报表识别",
                                "field": "待确认", "old_value": None, "new_value": None, "delta": None, "message": issue["message"],
                                "severity": "issue", "report_id": report_id, "layer": "uncertainty", "old_source": sources["old"], "new_source": sources["new"]})
    for side, book in books.items():
        selected = set(result["coverage"]["compared_sheets"][side])
        aliases = {name for sheet in book["sheets"] if sheet["name"] in selected for name in sheet.get("member_sheets", [])}
        aliases.update(sheet["name"] for sheet in book["sheets"] if sheet.get("member_sheets") and set(sheet["member_sheets"]).issubset(selected))
        result["coverage"]["excluded_sheets"][side] = [name for name in result["coverage"]["excluded_sheets"][side] if name not in aliases]
    for index, diff in enumerate(result["diffs"], 1):
        diff["id"] = f"diff-{index:07d}"
    for entry in inventory["reports"]:
        entry["change_count"] = sum(diff.get("report_id") == entry["id"] and diff["severity"] == "change" for diff in result["diffs"])
    issues = sum(diff["severity"] == "issue" for diff in result["diffs"])
    result["summary"].update(total=len(result["diffs"]), issues=issues, changes=len(result["diffs"]) - issues,
                             by_kind=dict(Counter(diff["kind"] for diff in result["diffs"])))
    result.update({key: inventory[key] for key in ("report_inventory_version", "reports", "issues", "type_inventory", "non_report_regions")})
    return result


def _compare_complete_reports(old: dict, new: dict, config: dict) -> dict:
    from .pdf_appearance import compare_report_appearance
    from .reports import _content_blockers, _key_blockers, _pair, attach_report_inventory, compare_inventory

    if old.get("report_inventory_version") != 2 or new.get("report_inventory_version") != 2:
        raise ValueError("两侧完整报表提取版本不一致，请重新处理为同一版本后比较")
    books = {side: book if "reports" in book else attach_report_inventory(book)
             for side, book in (("old", old), ("new", new))}
    inventory = compare_inventory(books["old"], books["new"], config)
    diffs = []
    categories = ("content", "structure", "format", "layout")
    labels = {"content": "内容", "structure": "结构", "format": "格式", "layout": "版式"}
    compared = {"old": set(), "new": set()}
    table_stats = Counter()
    for entry in inventory["reports"]:
        if entry["presence"] == "shared" and entry["confirmation"] == "confirmed":
            left, right = entry["old"], entry["new"]
            sheets = {side: next((s for s in books[side]["sheets"] if s["name"] == entry[side].get("sheet_name")), None)
                      for side in books}
            structured = None
            appearance_entry = entry
            if (left.get("header_signature") and left["header_signature"] == right.get("header_signature") and
                    all(sheet and sheet.get("structure") == "table" for sheet in sheets.values()) and
                    not _content_blockers(books["old"], left) and not _content_blockers(books["new"], right)):
                explicit_pair = next((pair for pair in config.get("pairs", []) if pair.get("old_report_id") == left["id"] and
                             pair.get("new_report_id") == right["id"] and any(c.get("is_key") for c in pair.get("columns", []))), None)
                pair = {**(explicit_pair or _pair(left, right, 1)), "id": entry["id"],
                        "old_sheet": left["sheet_name"], "new_sheet": right["sheet_name"]}
                if not _key_blockers(books["old"], books["new"], left, right, pair):
                    comparison_options = {**config, "pairs": [pair], "compare_formulas": False, "compare_formatting": False,
                                          "_pdf_key_whitespace": True}
                    numeric = compare_workbooks(books["old"], books["new"], comparison_options)
                    literal_books = {}
                    for side, sheet in sheets.items():
                        literal = copy.deepcopy(sheet)
                        for cell in literal["cells"].values():
                            value = cell.get("current_text", cell.get("raw_text", cell.get("display", "")))
                            cell.update(value=value or None, value_type="string" if value else "blank")
                        literal_books[side] = {"sheets": [literal], "warnings": []}
                    structured = compare_workbooks(literal_books["old"], literal_books["new"],
                                                   {**comparison_options, "ignore_outer_whitespace": False, "absolute_tolerance": "0"})
                    keys = [m for m in pair["columns"] if m.get("is_key")]
                    others = [m for m in pair["columns"] if not m.get("is_key") and m.get("old_column") and m.get("new_column")]
                    if (not explicit_pair and len(keys) == 1 and "名称" in keys[0]["name"] and
                            any(m["name"] in ("序号", "编号", "编码") for m in others)):
                        removed = [d for d in structured["diffs"] if d["kind"] == "row_removed"]
                        added = [d for d in structured["diffs"] if d["kind"] == "row_added"]
                        row_evidence = {}
                        for side, events in (("old", removed), ("new", added)):
                            for event in events:
                                row = int(re.search(r"\d+$", event[f"{side}_source"]["cell"]).group())
                                values = tuple(literal_books[side]["sheets"][0]["cells"].get(f"{m[f'{side}_column']}{row}", {}).get("value") for m in others)
                                if (sum(bool(v) for v in values) >= 2 and any(v and m["name"] in ("序号", "编号", "编码") for m, v in zip(others, values))):
                                    row_evidence[(side, event["id"])] = values
                        consumed = set()
                        replacements = []
                        for before in removed:
                            signature = row_evidence.get(("old", before["id"]))
                            matches = [after for after in added if signature and row_evidence.get(("new", after["id"])) == signature]
                            if len(matches) != 1 or sum(value == signature for (side, _), value in row_evidence.items() if side == "old") != 1:
                                continue
                            after = matches[0]
                            replacements.append({**before, "kind": "value_changed", "field": keys[0]["name"],
                                                 "new_value": after["new_value"], "new_source": after["new_source"],
                                                 "message": "序号及至少一项其他非空字段一致，且其他字段全部相同，唯一对应到同一项目；仅项目名称文字变化。"})
                            consumed.update((before["id"], after["id"]))
                        structured["diffs"] = [d for d in structured["diffs"] if d["id"] not in consumed] + replacements
                        structured["summary"]["matched_rows"] += len(replacements)
                        structured["summary"]["compared_cells"] += len(replacements) * len(pair["columns"])
                    for diff in structured["diffs"]:
                        number_diff = next((d for d in numeric["diffs"] if d["kind"] == diff["kind"] and
                                            d.get("old_source") == diff.get("old_source") and d.get("new_source") == diff.get("new_source")), None)
                        if number_diff and number_diff.get("delta") is not None:
                            diff.update(delta=number_diff["delta"], message="按业务键对应单元格；保留实际原文，差额为新版减旧版。")
                    if any(diff["severity"] == "issue" for diff in structured["diffs"]):
                        structured = None
                    else:
                        exclusions = {"old": [], "new": []}
                        for side, book in books.items():
                            for sheet in book["sheets"]:
                                if sheet["name"] not in entry[side].get("member_sheets", []) or sheet.get("structure") != "table":
                                    continue
                                box = list(sheet.get("bbox", [0, 0, 1, 1]))
                                box[1] = max((cell["pdf_source"]["bbox"][3] for cell in sheet["cells"].values()
                                              if cell["row"] <= sheet.get("suggested_header_row", 1) and cell.get("pdf_source")), default=box[1])
                                exclusions[side].append({"page": sheet["page_number"], "bbox": box})
                        appearance_entry = {**entry, "content_exclusions": exclusions}
            comparison = compare_report_appearance(books["old"], books["new"], appearance_entry)
            if structured is not None:
                emitted_structured = []
                for diff in structured["diffs"]:
                    whitespace_only = (diff["kind"] == "value_changed" and diff["old_value"] is not None and diff["new_value"] is not None and
                                       re.sub(r"\s+", "", diff["old_value"]) == re.sub(r"\s+", "", diff["new_value"]))
                    if (diff.get("row_key") == "表头" or whitespace_only) and any(
                            native["severity"] == "change" and native.get("measurements") and
                            (not whitespace_only or native["kind"] == "text_whitespace_changed") and all(
                                re.sub(r"\s+", "", (native["measurements"].get(side) or {}).get("fragment_text", "")) == re.sub(r"\s+", "", diff[f"{side}_value"] or "") and
                                f"{get_column_letter(((native['measurements'].get(side) or {}).get('cell') or {}).get('column', 1))}{((native['measurements'].get(side) or {}).get('cell') or {}).get('row', 0)}" == (diff.get(f"{side}_source") or {}).get("cell") and
                                (native.get(f"{side}_source") or {}).get("page") == (sheets[side]["cells"].get((diff.get(f"{side}_source") or {}).get("cell"), {}).get("pdf_source") or {}).get("page")
                                for side in ("old", "new")) for native in comparison["diffs"]):
                        continue
                    diff.update(category="content", layer="content", report_id=entry["id"])
                    if whitespace_only:
                        diff.update(kind="text_whitespace_changed", category="format", layer="format", delta=None,
                                    message="对应单元格的非空白文字相同，仅排字空白或换行变化；业务键配对保留原文，不作整行增删。")
                    for side in books:
                        source = diff.get(f"{side}_source")
                        if source:
                            cell = sheets[side]["cells"].get(source.get("cell"))
                            if cell and cell.get("pdf_source"):
                                source.update(copy.deepcopy(cell["pdf_source"]))
                                text_boxes = [s["bbox"] for s in source.get("content_sources", [])
                                              if s.get("page") == source.get("page") and s.get("bbox")]
                                if text_boxes:
                                    source["cell_bbox"] = source["bbox"]
                                    source["highlight_boxes"] = text_boxes
                                    source["bbox"] = [min(b[0] for b in text_boxes), min(b[1] for b in text_boxes),
                                                      max(b[2] for b in text_boxes), max(b[3] for b in text_boxes)]
                                    source["location_precision"] = "text"
                            else:
                                row_match = re.search(r"\d+$", source.get("cell", ""))
                                row = int(row_match.group()) if row_match else None
                                neighbor = next((c for c in sheets[side]["cells"].values()
                                                 if c["row"] == row and c.get("pdf_source")), None)
                                page_number = neighbor["pdf_source"]["page"] if neighbor else sheets[side].get("page_number", entry[side]["pages"][0])
                                source.update(kind="pdf", page=page_number, bbox=[0, 0, 1, 1],
                                              method=sheets[side].get("method", "text"), location_precision="page")
                        else:
                            opposite = "new" if side == "old" else "old"
                            opposite_page = (diff.get(f"{opposite}_source") or {}).get("page")
                            matched_page = next((p.get(f"{side}_page") for p in comparison["page_pairs"]
                                                 if p.get(f"{opposite}_page") == opposite_page and p.get(f"{side}_page")), None)
                            region = next((r for r in entry[side].get("regions", []) if r["page"] == matched_page), None)
                            if region:
                                diff[f"{side}_source"] = {"kind": "pdf", "page": region["page"], "bbox": [0, 0, 1, 1],
                                                           "method": "text", "location_precision": "page"}
                    comparison["diffs"].append(diff)
                    emitted_structured.append(diff)
                table_stats.update({key: structured["summary"].get(key, 0) for key in ("matched_rows", "unchanged_rows", "compared_cells")})
                for category in ("content", "format"):
                    dimension = comparison["dimensions"][category]
                    changes = sum(d["category"] == category for d in emitted_structured)
                    if changes and dimension["status"] == "unchanged":
                        dimension["status"] = "changed"
                    if category == "content" or changes:
                        dimension["summary"] += f"；兼容网格按业务键比较 {structured['summary'].get('compared_cells', 0)} 个字段，另有 {changes} 项{labels[category]}变化并保留原文"
            entry["dimensions"] = comparison["dimensions"]
            entry["page_pairs"] = comparison["page_pairs"]
            for field in ("paragraph_checks", "paragraph_coverage"):
                if field in comparison:
                    entry[field] = comparison[field]
            for side in compared:
                compared[side].update(entry[side].get("member_sheets", []))
            for diff in comparison["diffs"]:
                diff.update(report_id=entry["id"], table_id=entry["id"], table_name=entry["title"])
                diff.setdefault("layer", "uncertainty" if diff["severity"] == "issue" else diff["category"])
                diffs.append(diff)
            for category, dimension in entry["dimensions"].items():
                if dimension["status"] in ("partial", "unverified") and not any(
                        d.get("category") == category and d["severity"] == "issue" for d in comparison["diffs"]):
                    diffs.append({"id": "", "kind": "attribute_unverified", "category": category, "layer": "uncertainty",
                                  "report_id": entry["id"], "table_id": entry["id"], "table_name": entry["title"],
                                  "row_key": entry["title"], "field": f"{labels[category]}验证范围", "old_value": None,
                                  "new_value": None, "delta": None, "severity": "issue",
                                  "message": dimension["summary"] + "；" + "；".join(dimension.get("limitations", [])),
                                  "old_source": entry["old"]["source"], "new_source": entry["new"]["source"]})
            content = entry["dimensions"]["content"]
            entry["content_status"] = "reliable" if content["status"] in ("changed", "unchanged") else "pending"
        else:
            applicable = entry["confirmation"] == "confirmed"
            entry["dimensions"] = {category: {"status": "not_applicable" if applicable else "unverified",
                                               "changes": 0, "issues": 0 if applicable else 1,
                                               "summary": "单侧存在，无对应报表可比较" if applicable else "对应关系待确认，尚未执行此类比较",
                                               "limitations": []} for category in categories}
            entry["page_pairs"] = []
            if not applicable:
                entry["paragraph_checks"] = [
                    {"attribute": attribute, "status": "unverified", "old_value": None, "new_value": None,
                     "message": "报表对应关系待确认，未执行段落属性比较。",
                     "old_source": (entry.get("old") or {}).get("source"),
                     "new_source": (entry.get("new") or {}).get("source"), "measurements": {}}
                    for attribute in ("line_spacing", "alignment")]
            if applicable and entry["presence"] in ("old_only", "new_only"):
                removed = entry["presence"] == "old_only"
                report = entry["old"] if removed else entry["new"]
                diffs.append({"id": "", "kind": "report_removed" if removed else "report_added", "category": "completeness",
                              "layer": "completeness", "report_id": entry["id"], "table_id": entry["id"],
                              "table_name": entry["title"], "row_key": entry["object"] or entry["title"], "field": "报表完整性",
                              "old_value": entry["title"] if removed else None, "new_value": None if removed else entry["title"],
                              "delta": None, "message": entry["basis"], "severity": "change",
                              "old_source": report["source"] if removed else None, "new_source": None if removed else report["source"]})

    if not inventory["reports"]:
        inventory["issues"].append({"id": "issue-no-reports", "type": "inventory", "message": "未识别出对应业务报表；文档区域仍保留，不能判为一致。",
                                     "report_ids": [], "pages": [], "sources": [], "actions": ["review_identity"]})
    for issue in inventory["issues"]:
        if issue.get("diff_id"):
            continue
        sources = {side: next((source for source in issue.get("sources", []) if source.get("side", issue.get("side")) == side), None)
                   for side in ("old", "new")}
        report_id = next(iter(issue.get("report_ids", [])), None)
        entry = next((row for row in inventory["reports"] if row["id"] == report_id), None)
        if entry:
            sources = {side: source if source and source.get("page") in (entry.get(side) or {}).get("pages", []) else None
                       for side, source in sources.items()}
        diffs.append({"id": "", "kind": "report_review", "table_id": report_id or issue["id"],
                      "table_name": entry["title"] if entry else "文档范围核对", "row_key": entry["title"] if entry else "报表识别",
                      "field": "对应关系或提取待确认", "old_value": None, "new_value": None, "delta": None,
                      "message": issue["message"], "severity": "issue", "report_id": report_id, "category": "completeness",
                      "layer": "uncertainty", "old_source": sources["old"], "new_source": sources["new"]})

    for index, diff in enumerate(diffs, 1):
        diff["id"] = f"diff-{index:07d}"
        for side in ("old", "new"):
            if not diff.get(f"{side}_source"):
                continue
            source = copy.deepcopy(diff[f"{side}_source"])
            precision = source.get("location_precision", source.get("precision"))
            if precision == "page":
                grain = "unconfirmed"
            elif precision == "corresponding_region":
                grain = "reference"
            elif precision == "text":
                grain = "text"
            elif diff["kind"].startswith("paragraph_"):
                grain = "paragraph"
            else:
                grain = {"form_geometry_changed": "frame", "border_style_changed": "line",
                         "filling_line_geometry_changed": "line", "grid_geometry_changed": "table"}.get(diff["kind"])
                grain = grain or ("page" if source.get("bbox") == [0, 0, 1, 1] else "cell" if source.get("cell") else "region")
            reason = {"unconfirmed": "只能定位所属原页，对应文字位置未确认。",
                      "reference": "根据对应区域提供参照，不代表已确认的文字或空白位置。",
                      "text": "按本侧文字几何定位；多行内容分段高亮，识别可靠性见该条判定。",
                      "paragraph": "对应文字段，用于核对实际基线行距或几何对齐。",
                      "frame": "完整闭合框的中心线范围，用于比较框尺寸和页边位置。",
                      "line": "完整矢量线段，用于比较线宽、线型、颜色或位置。",
                      "table": "完整网格，用于比较表内行高和列宽。",
                      "page": "整页属性或整体结构的比较范围，不代表整页文字均发生变化。",
                      "cell": "对应单元格范围，不代表其中每个字符均发生变化。",
                      "region": "当前差异的区域范围，归属与判定依据见差异说明。"}[grain]
            if grain == "text" and diff["kind"] in ("font_changed", "font_size_changed", "font_weight_changed", "text_color_changed", "character_spacing_changed", "text_layout_changed"):
                reason = "本侧首处测量或样式差异样例；其余变化数量与依据见差异说明。"
            if source.get("geometry_basis"):
                reason += " " + source["geometry_basis"]
            source.update(location_granularity=grain, location_reason=reason)
            diff[f"{side}_source"] = source
        if diff["severity"] == "issue" and diff.get("category") in categories:
            inventory["issues"].append({"id": f"issue-{diff['id']}", "diff_id": diff["id"], "type": "attribute_unverified",
                                         "category": diff["category"], "message": diff["message"], "report_ids": [diff["report_id"]],
                                         "pages": sorted({source["page"] for side in ("old", "new")
                                                          if (source := diff.get(f"{side}_source")) and source.get("page")}),
                                         "sources": [{**diff[f"{side}_source"], "side": side} for side in ("old", "new") if diff.get(f"{side}_source")],
                                         "actions": ["review_ocr", "reprocess"]})
    for entry in inventory["reports"]:
        own = [diff for diff in diffs if diff.get("report_id") == entry["id"]]
        entry["change_count"] = sum(diff["severity"] == "change" for diff in own)
        if entry["presence"] == "shared" and entry["confirmation"] == "confirmed":
            for category, dimension in entry["dimensions"].items():
                dimension["changes"] = sum(diff.get("category") == category and diff["severity"] == "change" for diff in own)
                dimension["issues"] = sum(diff.get("category") == category and diff["severity"] == "issue" for diff in own)
        entry["comparison_status"] = "changed" if entry["change_count"] else "pending" if (
            entry["confirmation"] != "confirmed" or any(d["status"] in ("partial", "unverified") for d in entry["dimensions"].values())
            or any(diff["severity"] == "issue" for diff in own)) else "unchanged"
    issue_count = sum(diff["severity"] == "issue" for diff in diffs)
    return {**{key: inventory[key] for key in ("report_inventory_version", "reports", "issues", "type_inventory", "non_report_regions")},
            "diffs": diffs,
            "summary": {"total": len(diffs), "issues": issue_count, "changes": len(diffs) - issue_count,
                        "by_kind": dict(Counter(diff["kind"] for diff in diffs)),
                        "by_category": dict(Counter(diff["category"] for diff in diffs if diff["severity"] == "change")),
                        "matched_rows": table_stats["matched_rows"], "unchanged_rows": table_stats["unchanged_rows"], "compared_cells": table_stats["compared_cells"],
                        "compared_reports": sum(entry["presence"] == "shared" and entry["confirmation"] == "confirmed" for entry in inventory["reports"])},
            "coverage": {"compared_sheets": {side: sorted(names) for side, names in compared.items()},
                         "excluded_sheets": {"old": [], "new": []},
                         "compared_reports": {side: [entry[side]["id"] for entry in inventory["reports"]
                                                      if entry["presence"] == "shared" and entry["confirmation"] == "confirmed"] for side in books},
                         "excluded_reports": {side: [entry[side]["id"] for entry in inventory["reports"]
                                                     if entry.get(side) and entry["confirmation"] != "confirmed"] for side in books},
                         "warnings": []}}


def compare_pdfs(old: dict, new: dict, config: dict) -> dict:
    if old.get("report_inventory_version", 0) >= 2 or new.get("report_inventory_version", 0) >= 2:
        return _compare_complete_reports(old, new, config)
    if old.get("report_inventory_version") == 1 or new.get("report_inventory_version") == 1:
        return _compare_report_pdfs(old, new, config)
    books = {"old": old, "new": new}
    normalized = {"old": set(), "new": set()}
    for pair in config.get("pairs", []):
        chosen = {side: next((s for s in book["sheets"] if s["name"] == pair.get(f"{side}_sheet")), None)
                  for side, book in books.items()}
        if pair.get("mode") != "position" or not all(s and s.get("structure") == "text_blocks" for s in chosen.values()):
            continue
        if not any(s["method"] == "ocr" for s in chosen.values()):
            continue
        if any(p.get("mode") != "position" for side, sheet in chosen.items()
               for p in config["pairs"] if p.get(f"{side}_sheet") == sheet["name"]):
            continue
        for side, sheet in chosen.items():
            normalized[side].add(sheet["name"])
    for side, workbook in (("old", old), ("new", new)):
        selected = {pair.get(f"{side}_sheet") for pair in config.get("pairs", [])}
        for sheet in workbook["sheets"]:
            if sheet["name"] in selected and any(name in selected for name in sheet.get("member_sheets", [])):
                raise ValueError("同一侧不能同时选择逻辑报表候选和它的原始页表，请删除重复配对。")
    if config.get("pairs"):
        comparison_books = {}
        for side, book in books.items():
            sheets = []
            for sheet in book["sheets"]:
                if sheet["name"] in normalized[side]:
                    sheet = copy.deepcopy(sheet)
                    for cell in sheet["cells"].values():
                        if cell["row"] > 1 and cell["value_type"] == "string":
                            cell["value"] = re.sub(r"\s+", "", cell["value"])
                sheets.append(sheet)
            comparison_books[side] = {**book, "sheets": sheets}
        result = compare_workbooks(comparison_books["old"], comparison_books["new"], config)
    else:
        result = {"diffs": [], "summary": {"matched_rows": 0, "unchanged_rows": 0, "compared_cells": 0},
                  "coverage": {"compared_sheets": {"old": [], "new": []},
                               "excluded_sheets": {"old": [s["name"] for s in old["sheets"]], "new": [s["name"] for s in new["sheets"]]},
                               "warnings": old.get("warnings", []) + new.get("warnings", [])}}
    if any(normalized.values()):
        result["coverage"]["warnings"].append("含 OCR 的正文按水平行对齐并忽略空白差异；仅作用于按位置匹配的正文，保留原始识别文本供核验，不改变表格值或业务键规则。")
    any_ocr = any(page["method"] == "ocr" for book in books.values() for page in book.get("pages", []))
    for side, book in books.items():
        selected = set(result["coverage"]["compared_sheets"][side])
        aliases = {name for sheet in book["sheets"] if sheet["name"] in result["coverage"]["compared_sheets"][side]
                   for name in sheet.get("member_sheets", [])}
        aliases.update(sheet["name"] for sheet in book["sheets"] if sheet.get("member_sheets") and
                       set(sheet["member_sheets"]).issubset(selected))
        result["coverage"]["excluded_sheets"][side] = [name for name in result["coverage"]["excluded_sheets"][side] if name not in aliases]
    for diff in result["diffs"]:
        ocr_source = False
        verified = True
        pair = next((p for p in config.get("pairs", []) if p["id"] == diff["table_id"]), {})
        for side, book in books.items():
            source = diff.get(f"{side}_source")
            if not source:
                continue
            sheet = next(s for s in book["sheets"] if s["name"] == source["sheet"])
            cell = sheet["cells"].get(source["cell"])
            if sheet["name"] in normalized[side] and cell and diff["kind"] == "value_changed":
                diff[f"{side}_value"] = cell.get("current_text", cell.get("raw_text", cell["display"]))
            if cell and cell.get("pdf_source"):
                source.update(cell["pdf_source"])
                ocr_source |= source["method"] == "ocr"
                if source["method"] == "ocr":
                    source.update(reviewed=bool(cell.get("reviewed")), review_id=cell.get("review_id"))
                    verified &= _cell_verified(book, cell)
            else:
                row_match = re.search(r"\d+$", source["cell"])
                row = int(row_match.group()) if row_match else 1
                neighbour = next((c for c in sheet["cells"].values() if c["row"] == row and c.get("pdf_source")), None)
                page_number = neighbour["pdf_source"]["page"] if neighbour else sheet["page_number"]
                source.update(kind="pdf", page=page_number, bbox=[0, 0, 1, 1], method=sheet["method"], precision="page")
                diff["message"] += " 该空单元格缺少独立识别框，仅定位到对应整页。"
                ocr_source |= sheet["method"] == "ocr"
                verified &= sheet["method"] != "ocr"
            if pair.get("mode") == "key" and diff["row_key"] != "表头":
                row_match = re.search(r"\d+$", source["cell"])
                for mapping in pair.get("columns", []):
                    if not mapping.get("is_key") or not row_match:
                        continue
                    key_cell = sheet["cells"].get(f"{mapping.get(f'{side}_column')}{row_match.group()}")
                    if key_cell and key_cell.get("pdf_source", {}).get("method") == "ocr":
                        ocr_source = True
                        verified &= _cell_verified(book, key_cell)
        uncertain_structure = diff["kind"] in ("row_added", "row_removed", "sheet_added", "sheet_removed", "column_added", "column_removed") and any_ocr
        if uncertain_structure:
            verified &= all(_page_verified(book, page["number"]) for book in books.values()
                            for page in book.get("pages", []) if page["method"] == "ocr")
        if (ocr_source or uncertain_structure) and not verified:
            diff["original_kind"] = diff["kind"]
            diff["kind"] = "ocr_suspected"
            diff["severity"] = "issue"
            diff["message"] = "疑似差异：涉及 OCR 识别或对侧可能漏识别，须核对原图后确认。" + diff["message"]
    for side, book in books.items():
        for page in book.get("pages", []):
            unavailable = page["status"] == "unavailable"
            if page["method"] != "ocr" and not unavailable:
                continue
            if not unavailable and not page.get("structure_unconfirmed") and page.get("reviewed"):
                continue
            kind = "extraction_unavailable" if unavailable else "ocr_review"
            source = {"kind": "pdf", "page": page["number"], "bbox": [0, 0, 1, 1], "method": page["method"]}
            result["diffs"].append({"id": "", "kind": kind, "table_id": f"{side}-page-{page['number']}",
                                    "table_name": f"{'旧版' if side == 'old' else '新版'}第 {page['number']} 页",
                                    "row_key": f"{'旧版' if side == 'old' else '新版'}第 {page['number']} 页", "field": "提取完整性" if unavailable else "扫描内容核验",
                                    "old_value": None, "new_value": None, "delta": None, "severity": "issue",
                                    "message": "本页未提取到可靠内容，不能据此判为无差异。" if unavailable else "本页表格结构尚未可靠恢复，单元格文字核验不能排除错列或漏行。" if page.get("structure_unconfirmed") else "本页识别结果尚未人工核验；即使两侧识别值相同，仍须核对漏字、错列和漏行。",
                                    "old_source": source if side == "old" else None,
                                    "new_source": source if side == "new" else None})
    for index, diff in enumerate(result["diffs"], 1):
        diff["id"] = f"diff-{index:07d}"
    issues = sum(diff["severity"] == "issue" for diff in result["diffs"])
    result["summary"].update(total=len(result["diffs"]), issues=issues, changes=len(result["diffs"]) - issues,
                             by_kind=dict(Counter(diff["kind"] for diff in result["diffs"])))
    return result
