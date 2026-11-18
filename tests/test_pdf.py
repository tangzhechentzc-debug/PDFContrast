import copy
from pathlib import Path

import pytest
import pypdfium2 as pdfium
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfdoc
from reportlab.pdfgen import canvas

from backend.pdf import compare_pdfs, extract_pdf, suggest_pdf_pairs


ROOT = Path(__file__).resolve().parents[1]


def config(old, new):
    return {"pairs": suggest_pdf_pairs(old, new), "absolute_tolerance": "0",
            "ignore_outer_whitespace": True, "compare_formulas": False, "compare_formatting": False}



def identified_fixture(tmp_path, side="old"):
    from scripts.make_pdf_examples import register_font, PAGES
    font, _ = register_font(None)
    path = tmp_path / f"identified-{side}.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(595, 842))
    for number, rows in enumerate(PAGES[side], 1):
        pdf.setFont(font, 12)
        pdf.drawString(40, 800, "A.1 项目费用明细报表")
        pdf.drawString(40, 780, "工程名称：验证工程")
        pdf.drawString(40, 760, f"第{number}页 共2页")
        for x in (40, 150, 390, 550):
            pdf.line(x, 540, x, 740)
        for y in (540, 590, 640, 690, 740):
            pdf.line(40, y, 550, y)
        for row, values in enumerate([("编号", "名称", "金额"), *rows]):
            for x, value in zip((50, 160, 400), values):
                pdf.drawString(x, 710 - row * 50, value)
        pdf.showPage()
    pdf.save()
    return path


def test_native_cross_page_values_and_no_reorder_noise(tmp_path):
    old = extract_pdf(identified_fixture(tmp_path), tmp_path / "old")
    new = extract_pdf(identified_fixture(tmp_path, "new"), tmp_path / "new")
    result = compare_pdfs(old, new, config(old, new))
    from collections import Counter
    assert Counter(d["kind"] for d in result["diffs"] if d.get("category") == "content") == {"row_removed": 1, "row_added": 1, "value_changed": 2}
    amount = next(d for d in result["diffs"] if d["row_key"] == "A001")
    assert amount["delta"] == "800"
    assert amount["old_source"]["page"] == 1 and amount["new_source"]["page"] == 2
    assert next(d for d in result["diffs"] if d["row_key"] == "A002")["delta"] == "0.003"
    assert not any(d["row_key"] == "A004" for d in result["diffs"])
    assert all(all(name.endswith("正文") for name in names) for names in result["coverage"]["excluded_sheets"].values())
    assert result["reports"][0]["content_status"] == "reliable"
    for source in (amount["old_source"], amount["new_source"]):
        x0, y0, x1, y1 = source["bbox"]
        assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
    assert (tmp_path / "old/page-1.png").is_file()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotation_cropbox_coordinates_match_rendered_geometry(tmp_path, rotation):
    original = ROOT / "examples/pdf/text/old.pdf"
    rotated = tmp_path / f"rotated-{rotation}.pdf"
    doc = pdfium.PdfDocument(original)
    page = doc[0]
    page.set_rotation(rotation)
    page.set_cropbox(10, 20, 580, 820)
    doc.save(rotated)
    page.close()
    doc.close()
    extracted = extract_pdf(rotated, tmp_path / "images")
    first = extracted["pages"][0]
    assert first["rotation"] == rotation
    with Image.open(tmp_path / "images/page-1.png") as image:
        assert image.size == (first["image_width"], first["image_height"])
        cells = [cell for sheet in extracted["sheets"] if sheet.get("structure") == "table"
                 for cell in sheet["cells"].values() if cell["pdf_source"]["page"] == 1 and cell["value"]]
        assert cells
        for cell in cells:
            x0, y0, x1, y1 = cell["pdf_source"]["bbox"]
            assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
            box = (int(x0 * image.width), int(y0 * image.height), max(int(x1 * image.width), int(x0 * image.width) + 1),
                   max(int(y1 * image.height), int(y0 * image.height) + 1))
            gray = image.crop(box).convert("L")
            assert gray.getextrema()[0] < 200


def test_real_local_ocr_same_document_still_requires_review(tmp_path):
    old = extract_pdf(ROOT / "examples/pdf/scan/old.pdf", tmp_path / "old")
    assert all(page["method"] == "ocr" and page["status"] == "ok" for page in old["pages"])
    assert any(cell["value"] == "12000.00" and cell["value_type"] == "number"
               for sheet in old["sheets"] for cell in sheet["cells"].values())
    result = compare_pdfs(old, old, config(old, old))
    assert result["summary"]["changes"] == 0 and result["summary"]["issues"] > 0
    assert not any(r["content_status"] == "reliable" for r in result["reports"])
    assert all(d["severity"] == "issue" for d in result["diffs"])


def test_ocr_changed_values_remain_pending_without_verified_identity(tmp_path):
    old = extract_pdf(ROOT / "examples/pdf/scan/old.pdf", tmp_path / "old")
    new = extract_pdf(ROOT / "examples/pdf/scan/new.pdf", tmp_path / "new")
    result = compare_pdfs(old, new, config(old, new))
    assert result["summary"]["changes"] == 0 and result["summary"]["issues"] > 0
    assert not any(d.get("layer") == "content" for d in result["diffs"])
    assert any(c["display"] == "12800.00" for sheet in new["sheets"] for c in sheet["cells"].values())


def test_native_and_scanned_same_content_preserve_evidence_without_forced_pairing(tmp_path):
    old = extract_pdf(ROOT / "examples/pdf/text/old.pdf", tmp_path / "old")
    new = extract_pdf(ROOT / "examples/pdf/scan/old.pdf", tmp_path / "new")
    original = copy.deepcopy(new)
    result = compare_pdfs(old, new, config(old, new))
    assert result["summary"]["changes"] == 0 and result["summary"]["issues"] > 0
    assert new == original
    assert any(c.get("raw_text", "").startswith("项目费用明细报表") for sheet in new["sheets"] for c in sheet["cells"].values())
    assert not any(r["content_status"] == "reliable" for r in result["reports"])


def test_blank_pages_and_real_ocr_failure_are_distinguished(tmp_path, monkeypatch):
    blank = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(blank)); c.showPage(); c.save()
    calls = []
    def broken(image):
        calls.append(image.size)
        raise RuntimeError("fixture failure")
    monkeypatch.setattr("backend.pdf._recognize", broken)
    empty = extract_pdf(blank, tmp_path / "images")
    assert not calls and empty["pages"][0]["blank"]
    result = compare_pdfs(empty, empty, {"pairs": []})
    assert result["summary"]["changes"] == 0 and not result["reports"]
    from test_extraction_progress import scanned_pdf
    scan = tmp_path / "scan.pdf"; scanned_pdf(scan, pages=1)
    failed = extract_pdf(scan, tmp_path / "failed")
    assert calls and failed["pages"][0]["status"] == "unavailable"
    failure = compare_pdfs(failed, failed, {"pairs": []})
    assert failure["summary"]["changes"] == 0 and failure["summary"]["issues"] > 0
    assert (tmp_path / "failed/page-1.png").exists()


def test_no_grid_ocr_is_single_column_and_structure_unconfirmed(tmp_path, monkeypatch):
    path = tmp_path / "empty.pdf"
    from test_extraction_progress import scanned_pdf
    scanned_pdf(path, pages=1)
    monkeypatch.setattr("backend.pdf._recognize", lambda image: [
        {"text": "A001 12000.00", "bbox": [20, 20, 250, 50], "confidence": .8},
        {"text": "A002 500.00", "bbox": [20, 70, 250, 100], "confidence": .8}])
    book = extract_pdf(path, tmp_path / "images")
    assert len(book["sheets"]) == 1
    assert book["sheets"][0]["structure"] == "text_blocks"
    assert book["sheets"][0]["max_column"] == 1
    assert book["pages"][0]["structure_unconfirmed"] is True
    assert suggest_pdf_pairs(book, book) == []
    assert not any(r["content_status"] == "reliable" for r in compare_pdfs(book, book, {"pairs": []})["reports"])


def test_logical_and_physical_alias_cannot_be_compared_twice(tmp_path):
    book = extract_pdf(identified_fixture(tmp_path), tmp_path / "images")
    settings = config(book, book)
    logical = next(s for s in book["sheets"] if s.get("member_sheets"))
    duplicated = copy.deepcopy(next(p for p in settings["pairs"] if p["old_sheet"] == logical["name"]))
    duplicated.update(id="duplicate", old_sheet=logical["member_sheets"][0], new_sheet=logical["member_sheets"][0])
    settings["pairs"].append(duplicated)
    result = compare_pdfs(book, book, settings)
    assert any("重复配对" in issue["message"] for issue in result["issues"])
    assert result["summary"]["changes"] == 0
    assert sum(r["content_status"] == "reliable" for r in result["reports"]) == 1


def test_corrupt_pdf_rejected(tmp_path):
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"%PDF-broken")
    with pytest.raises(ValueError, match="无法打开"):
        extract_pdf(path, tmp_path / "images")


def test_original_members_cover_unselected_logical_candidate(tmp_path):
    book = extract_pdf(identified_fixture(tmp_path), tmp_path / "images")
    physical = copy.deepcopy(book)
    physical["sheets"] = [s for s in book["sheets"] if not s.get("member_sheets")]
    from backend.excel import suggest_pairs
    settings = config(book, book)
    settings["pairs"] = suggest_pairs(physical, physical)
    result = compare_pdfs(book, book, settings)
    assert all(all(name.endswith("正文") for name in names) for names in result["coverage"]["excluded_sheets"].values())


def test_missing_cell_source_falls_back_to_its_actual_page(tmp_path):
    old = extract_pdf(identified_fixture(tmp_path), tmp_path / "images")
    new = copy.deepcopy(old)
    logical = next(s for s in new["sheets"] if s.get("member_sheets"))
    cell = next(c for c in logical["cells"].values() if c["column"] == 2 and c["row"] > 1
                and c["pdf_source"]["page"] == 2)
    del logical["cells"][cell["address"]]
    result = compare_pdfs(old, new, config(old, new))
    changed = next(d for d in result["diffs"] if d["new_source"] and d["new_source"]["cell"] == cell["address"])
    assert changed["new_source"]["page"] == 2
    assert changed["new_source"]["bbox"] == [0, 0, 1, 1]
    assert changed["new_source"]["location_precision"] == "page"


def test_small_scanned_body_with_native_header_is_not_silently_skipped(tmp_path, monkeypatch):
    path = tmp_path / "mixed.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(40, 790, "A native header containing more than forty five characters")
    c.drawImage(ImageReader(Image.new("RGB", (400, 100), "white")), 40, 500, width=400, height=100)
    c.showPage()
    c.save()
    amount = ["100"]
    monkeypatch.setattr("backend.pdf._recognize", lambda image: [
        {"text": "A001 " + amount[0], "bbox": [80, 500, 500, 600], "confidence": .95}])
    old = extract_pdf(path, tmp_path / "old")
    amount[0] = "200"
    new = extract_pdf(path, tmp_path / "new")
    assert old["pages"][0]["method"] == "ocr"
    assert any(c["pdf_source"]["method"] == "text" and "native header" in c["display"] for sh in old["sheets"] for c in sh["cells"].values())
    for book, expected in ((old, "A001 100"), (new, "A001 200")):
        assert any(c["display"] == expected and c["pdf_source"]["method"] == "ocr" for sh in book["sheets"] for c in sh["cells"].values())
    result = compare_pdfs(old, new, config(old, new))
    assert result["summary"]["changes"] == 0 and result["summary"]["issues"] > 0
    assert not any(d.get("layer") == "content" for d in result["diffs"])


def test_userunit_and_cropbox_preserve_normalized_cell_geometry(tmp_path, monkeypatch):
    monkeypatch.setattr(pdfdoc.PDFPage, "__NoDefault__", pdfdoc.PDFPage.__NoDefault__ + ["UserUnit"])
    path = tmp_path / "userunit.pdf"
    c = canvas.Canvas(str(path), pagesize=(300, 400), pdfVersion=(1, 7))
    for x in (50, 150, 250):
        c.line(x, 300, x, 380)
    for y in (300, 340, 380):
        c.line(50, y, 250, y)
    for x, y, text in ((60, 355, "Code"), (160, 355, "Amount"), (60, 315, "A001"), (160, 315, "12000")):
        c.drawString(x, y, text)
    c.showPage()
    page = c._doc.Pages.pages[0]
    page.UserUnit = 2
    page.CropBox = pdfdoc.PDFArray([20, 30, 280, 390])
    c.save()
    assert b"/UserUnit 2" in path.read_bytes()
    book = extract_pdf(path, tmp_path / "images")
    assert book["pages"][0]["method"] == "text"
    table = next(s for s in book["sheets"] if s["structure"] == "table")
    source = table["cells"]["B2"]["pdf_source"]
    assert source["bbox"] == pytest.approx([.5, .15, 250 / 300, .25], abs=.002)
    with Image.open(tmp_path / "images/page-1.png") as image:
        box = [round(v * (image.width if i % 2 == 0 else image.height)) for i, v in enumerate(source["bbox"])]
        assert image.crop(box).convert("L").getextrema()[0] < 100


def test_hundred_a4_pages_read_and_compare_and_101_pages_use_page_assets(tmp_path):
    from reportlab.lib.pagesizes import A4
    from backend.models import CompareConfig

    path = tmp_path / 'hundred-pages.pdf'
    writer = canvas.Canvas(str(path), pagesize=A4)
    for number in range(1, 101):
        writer.drawString(40, 780, f'Sample report page {number:03d}')
        for x in (40, 350, 550):
            writer.line(x, 650, x, 730)
        for y in (650, 690, 730):
            writer.line(40, y, 550, y)
        for x, y, text in ((60, 705, 'Code'), (370, 705, 'Amount'),
                           (60, 665, f'A{number:03d}'), (370, 665, '123')):
            writer.drawString(x, y, text)
        writer.showPage()
    writer.save()
    book = extract_pdf(path, tmp_path / 'hundred-images')
    assert len(book['pages']) == 100
    assert all(page['status'] == 'ok' and page['method'] == 'text' for page in book['pages'])
    assert sum(page['image_width'] * page['image_height'] for page in book['pages']) > 60000000
    assert (tmp_path / 'hundred-images/page-100.png').is_file()
    settings = CompareConfig.model_validate(config(book, book)).model_dump()
    assert settings["pairs"] == []  # Unknown business identity must not merge equal headers.
    result = compare_pdfs(book, book, settings)
    assert result["summary"]["changes"] == 0 and result["summary"]["issues"] > 0

    too_long = tmp_path / 'too-long.pdf'
    writer = canvas.Canvas(str(too_long), pagesize=A4)
    for _ in range(101):
        writer.showPage()
    writer.save()
    complete = extract_pdf(too_long, tmp_path / 'large-assets')
    assert len(complete['pages']) == 101
    assert complete['asset_storage'] == 'page_files_v1'
    assert not list((tmp_path / 'large-assets').glob('page-*.png'))
    assert len(list((tmp_path / 'large-assets').glob('appearance-*.json.gz'))) == 101
    assert all('appearance' not in page and page['render_deferred'] for page in complete['pages'])
