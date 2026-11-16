"""Build deterministic Chinese text/scanned PDF comparison fixtures."""

import argparse
import io
import json
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
PAGE_WIDTH, PAGE_HEIGHT = A4
X_EDGES = (48, 152, 392, 547)
TABLE_TOP = 160
ROW_HEIGHT = 44
HEADERS = ("编号", "名称", "金额")
PAGES = {
    "old": [
        [("A001", "设备采购", "12000.00"), ("A002", "精度校准", "1.231"), ("A003", "咨询服务", "500.00")],
        [("A004", "安装工程", "3000.00"), ("A005", "日常耗材", "250.00"), ("A006", "设备维护", "900.00")],
    ],
    "new": [
        [("A004", "安装工程", "3000.00"), ("A002", "精度校准", "1.234"), ("A007", "新增备件", "750.00")],
        [("A006", "设备维护", "900.00"), ("A001", "设备采购", "12800.00"), ("A005", "日常耗材", "250.00")],
    ],
}


def register_font(font_path):
    if font_path:
        pdfmetrics.registerFont(TTFont("FixtureCJK", str(font_path)))
        return "FixtureCJK", str(font_path)
    candidate = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
    if candidate.is_file():
        pdfmetrics.registerFont(TTFont("FixtureCJK", str(candidate)))
        return "FixtureCJK", str(candidate)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light", "STSong-Light (renderer must supply CJK glyphs)"


def draw_page(pdf, rows, page_number, font):
    pdf.setFillColorRGB(0.16, 0.20, 0.18)
    pdf.setFont(font, 22)
    pdf.drawString(48, PAGE_HEIGHT - 78, "项目费用明细报表")
    pdf.setFont(font, 11)
    pdf.drawString(48, PAGE_HEIGHT - 108, "报表编号：PDF-FIXTURE-001    金额单位：元")
    pdf.setFillColorRGB(0.92, 0.94, 0.93)
    pdf.rect(X_EDGES[0], PAGE_HEIGHT - TABLE_TOP - ROW_HEIGHT,
             X_EDGES[-1] - X_EDGES[0], ROW_HEIGHT, stroke=0, fill=1)
    bottom = TABLE_TOP + ROW_HEIGHT * (len(rows) + 1)
    pdf.setStrokeColorRGB(0.30, 0.35, 0.32)
    pdf.setLineWidth(0.75)
    for x in X_EDGES:
        pdf.line(x, PAGE_HEIGHT - TABLE_TOP, x, PAGE_HEIGHT - bottom)
    for index in range(len(rows) + 2):
        y = PAGE_HEIGHT - TABLE_TOP - index * ROW_HEIGHT
        pdf.line(X_EDGES[0], y, X_EDGES[-1], y)
    pdf.setFillColorRGB(0.10, 0.14, 0.12)
    pdf.setFont(font, 13)
    for row_index, row in enumerate([HEADERS, *rows]):
        baseline = PAGE_HEIGHT - TABLE_TOP - row_index * ROW_HEIGHT - 28
        for column, value in enumerate(row):
            if column == 2 and row_index:
                pdf.drawRightString(X_EDGES[column + 1] - 14, baseline, value)
            else:
                pdf.drawString(X_EDGES[column] + 14, baseline, value)
    pdf.setFont(font, 10)
    pdf.setFillColorRGB(0.40, 0.44, 0.42)
    pdf.drawString(48, PAGE_HEIGHT - bottom - 32, "说明：保留原始小数位；本报表仅用于软件验证。")
    pdf.drawString(48, 42, "项目费用明细报表")
    pdf.drawRightString(547, 42, f"第 {page_number} 页 / 共 2 页")
    pdf.showPage()


def make_text_pdf(path, side, font):
    pdf = canvas.Canvas(str(path), pagesize=A4, invariant=1, pageCompression=1)
    pdf.setTitle("项目费用明细报表")
    for number, rows in enumerate(PAGES[side], 1):
        draw_page(pdf, rows, number, font)
    pdf.save()


def make_scanned_pdf(source, destination, preview_dir, side):
    pdf = canvas.Canvas(str(destination), pagesize=A4, invariant=1, pageCompression=1)
    pdf.setTitle("项目费用明细报表 - 扫描测试")
    document = pdfium.PdfDocument(str(source))
    try:
        for index in range(len(document)):
            page = document[index]
            bitmap = page.render(scale=3)
            image = bitmap.to_pil().convert("RGB")
            png = io.BytesIO()
            image.save(png, format="PNG")
            pdf.drawImage(ImageReader(png), 0, 0, width=PAGE_WIDTH, height=PAGE_HEIGHT)
            pdf.showPage()
            image.thumbnail((894, 1264))
            image.save(preview_dir / f"{side}-page-{index + 1}.png")
            bitmap.close()
            page.close()
    finally:
        document.close()
    pdf.save()


def expected_manifest(font_description):
    sources = {}
    for side, pages in PAGES.items():
        sources[side] = {}
        for page_index, rows in enumerate(pages):
            for row_index, values in enumerate(rows, 1):
                cells = {}
                for column, (field, value) in enumerate(zip(HEADERS, values)):
                    top = TABLE_TOP + row_index * ROW_HEIGHT
                    bottom = top + ROW_HEIGHT
                    cells[field] = {
                        "value": value,
                        "bbox_top_origin": [X_EDGES[column], top, X_EDGES[column + 1], bottom],
                        "bbox_pdf_user_space": [X_EDGES[column], PAGE_HEIGHT - bottom,
                                                X_EDGES[column + 1], PAGE_HEIGHT - top],
                    }
                sources[side][values[0]] = {"page_index": page_index, "page_number": page_index + 1,
                                           "table_row_index": row_index, "cells": cells}
    return {
        "description": "Text and scan variants contain identical visible data. Scan PDFs contain no text objects.",
        "font": font_description,
        "page_size_points": [PAGE_WIDTH, PAGE_HEIGHT],
        "rotation": 0,
        "scan_render_scale": 3,
        "header": list(HEADERS),
        "match_key": "编号",
        "expected_content_changes": [
            {"kind": "value_changed", "key": "A001", "field": "金额", "old": "12000.00", "new": "12800.00", "delta": "800.00"},
            {"kind": "value_changed", "key": "A002", "field": "金额", "old": "1.231", "new": "1.234", "delta": "0.003"},
            {"kind": "row_removed", "key": "A003"},
            {"kind": "row_added", "key": "A007"},
        ],
        "matched_keys": ["A001", "A002", "A004", "A005", "A006"],
        "unchanged_keys": ["A004", "A005", "A006"],
        "cross_page_keys": ["A001", "A004"],
        "source_truth": sources,
    }


def verify_text_and_scan(output):
    for side in PAGES:
        with pdfplumber.open(output / "text" / f"{side}.pdf") as pdf:
            assert len(pdf.pages) == 2
            for index, page in enumerate(pdf.pages):
                tables = page.extract_tables()
                expected = [list(HEADERS), *[list(row) for row in PAGES[side][index]]]
                assert tables == [expected], (side, index, tables)
                assert "项目费用明细报表" in page.extract_text()
        with pdfplumber.open(output / "scan" / f"{side}.pdf") as pdf:
            assert len(pdf.pages) == 2
            assert all(not page.chars and len(page.images) == 1 for page in pdf.pages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, help="Optional embeddable CJK TrueType font")
    args = parser.parse_args()
    output = ROOT / "examples" / "pdf"
    for folder in ("text", "scan", "previews"):
        (output / folder).mkdir(parents=True, exist_ok=True)
    font, description = register_font(args.font)
    for side in PAGES:
        source = output / "text" / f"{side}.pdf"
        make_text_pdf(source, side, font)
        make_scanned_pdf(source, output / "scan" / f"{side}.pdf", output / "previews", side)
    verify_text_and_scan(output)
    (output / "expected.json").write_text(
        json.dumps(expected_manifest(description), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Created and structurally verified 4 PDFs in {output}")
    print("Expected: 2 value changes, 1 row removed, 1 row added; A001/A004 change pages.")


if __name__ == "__main__":
    main()
