from __future__ import annotations

import ctypes
import math
from collections import defaultdict
from contextlib import nullcontext

import pypdfium2 as pdfium
from pdfminer.pdftypes import resolve1


class PdfGlyphGeometry:
    """Read renderer glyph bounds without changing extraction text or layout bounds."""

    def __init__(self, source, password=None, lock=None):
        if hasattr(source, "read"):
            stream = source
            position = stream.tell()
            try:
                stream.seek(0)
                source = stream.read()
            finally:
                # Keep pdfminer's input position independent of PDFium's reader.
                stream.seek(position)
        self.lock = lock if lock is not None else nullcontext()
        with self.lock:
            self.document = pdfium.PdfDocument(source, password=password)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self.lock:
            self.document.close()

    def resolve(self, page, chars):
        result = {"page": page.page_number, "renderer": "PDFium tight character bounds",
                  "origin_tolerance_canvas_units": .05,
                  "origin_tolerance_pt": .05 * float(resolve1(page.page_obj.attrs.get("UserUnit", 1))), "chars": {}}
        x0, y0, x1, y1 = map(float, page.page_obj.mediabox)
        transforms = {
            0: (1, 0, 0, 1, -x0, -y0),
            90: (0, -1, 1, 0, -y0, x1),
            180: (-1, 0, 0, -1, x1, y1),
            270: (0, 1, -1, 0, y1, -x0),
        }
        a, b, c, d, e, f = transforms[page.rotation]

        def point(x, y):
            return a * x + c * y + e, b * x + d * y + f

        media_left, media_top = page.mediabox[:2]
        left, top = page.bbox[:2]
        height = page.root_page.height

        def normalized_box(box):
            points = [point(x, y) for x in (box[0], box[2]) for y in (box[1], box[3])]
            xs = [(x + media_left - left) / page.width for x, y in points]
            ys = [(height - y + media_top - top) / page.height for x, y in points]
            return [min(xs), min(ys), max(xs), max(ys)]

        rendered = defaultdict(list)
        with self.lock:
            renderer_page = self.document[page.page_number - 1]
            text_page = renderer_page.get_textpage()
            try:
                for index in range(text_page.count_chars()):
                    code = pdfium.raw.FPDFText_GetUnicode(text_page, index)
                    if not code or chr(code).isspace():
                        continue
                    x, y = ctypes.c_double(), ctypes.c_double()
                    if not pdfium.raw.FPDFText_GetCharOrigin(text_page, index, ctypes.byref(x), ctypes.byref(y)):
                        continue
                    box = text_page.get_charbox(index)
                    if not all(math.isfinite(v) for v in box) or box[0] >= box[2] or box[1] >= box[3]:
                        continue
                    rendered[chr(code)].append({"origin": point(x.value, y.value), "bbox": normalized_box(box)})
            finally:
                text_page.close()
                renderer_page.close()

        source_by_text = defaultdict(list)
        for char in chars:
            source_by_text[char["text"]].append(char)
        for char in chars:
            key, text = char["id"], char["text"]
            record = {"status": "unverified", "reason": "未找到唯一的同字符、同绘制原点字形"}
            result["chars"][key] = record
            if not text.strip():
                record.update(status="not_applicable", reason="空白字符没有可见字形紧框")
                continue
            matrix = char.get("matrix")
            if len(text) != 1 or not matrix or len(matrix) != 6:
                continue
            same_origin = [other for other in source_by_text[text] if other.get("matrix") and
                           max(abs(other["matrix"][4] - matrix[4]), abs(other["matrix"][5] - matrix[5])) <= .05]
            if len(same_origin) != 1:
                record["reason"] = "同字符在同一绘制原点重复叠印，无法确认单个字形对应"
                continue
            candidates = [item for item in rendered[text]
                          if max(abs(item["origin"][0] - matrix[4]), abs(item["origin"][1] - matrix[5])) <= .05]
            boxes = {tuple(round(v, 7) for v in item["bbox"]) for item in candidates}
            if len(boxes) != 1:
                continue
            box = list(next(iter(boxes)))
            visible = [max(0, min(1, v)) for v in box]
            if visible[0] >= visible[2] or visible[1] >= visible[3]:
                record["reason"] = "渲染字形不在当前可见页面范围内"
                continue
            metric = char["bbox"]
            area = (box[2] - box[0]) * (box[3] - box[1])
            overlap = max(0, min(box[2], metric[2]) - max(box[0], metric[0])) * max(0, min(box[3], metric[3]) - max(box[1], metric[1]))
            coverage = overlap / area
            record.update(status="verified", reason="同字符与绘制原点唯一对应到渲染字形紧框",
                          glyph_bbox=visible, metric_coverage=round(coverage, 6),
                          metric_bbox_conflict=coverage < .75, clipped=visible != box)
        statuses = defaultdict(int)
        for value in result["chars"].values():
            statuses[value["status"]] += 1
        result["summary"] = {**statuses, "metric_conflicts": sum(value.get("metric_bbox_conflict", False) for value in result["chars"].values())}
        return result
