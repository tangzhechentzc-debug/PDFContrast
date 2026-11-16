from pathlib import Path

import pytest
from openpyxl import Workbook
from reportlab.pdfgen import canvas

from backend.excel import extract_workbook
from backend.pdf import extract_pdf


ROOT = Path(__file__).resolve().parents[1]


def scanned_pdf(path, pages=2):
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader
    image = Image.new("RGB", (400, 150), "white")
    ImageDraw.Draw(image).text((15, 20), "Visible scan words 123456", fill="black")
    document = canvas.Canvas(str(path))
    for _ in range(pages):
        document.drawImage(ImageReader(image), 30, 500, width=400, height=150)
        document.showPage()
    document.save()


def test_excel_reports_actual_sheets_and_preserves_output_without_callback(tmp_path):
    path = tmp_path / "sheets.xlsx"
    workbook = Workbook()
    workbook.active.title = "费用明细"
    workbook.active.append(["项目编码", "金额"])
    workbook.active.append(["001", 10])
    workbook.create_sheet("汇总").append(["合计", 10])
    workbook.create_sheet("空表")
    workbook.save(path)
    events = []
    extracted = extract_workbook(path, progress=events.append)
    assert extracted == extract_workbook(path)
    assert events[0] == {"stage": "opening", "unit": "sheet", "current": 0, "total": None, "completed": 0}
    reading = [event for event in events if event["stage"] == "reading_sheet"]
    assert [(event["current"], event["completed"], event["item_name"]) for event in reading] == [
        (1, 0, "费用明细"), (1, 1, "费用明细"), (2, 1, "汇总"), (2, 2, "汇总"), (3, 2, "空表"), (3, 3, "空表")]
    assert all(event["total"] == 3 and event["unit"] == "sheet" for event in reading)
    assert events[-1] == {"stage": "file_completed", "unit": "sheet", "current": 3, "total": 3, "completed": 3}


def test_pdf_reports_render_text_and_table_stages_for_each_real_page(tmp_path):
    path = ROOT / "examples/pdf/text/old.pdf"
    events = []
    extracted = extract_pdf(path, tmp_path / "images", progress=events.append)
    assert extracted == extract_pdf(path, tmp_path / "plain")
    assert events[0] == {"stage": "opening", "unit": "page", "current": 0, "total": 2, "completed": 0}
    assert all(event["unit"] == "page" and event["total"] == 2 for event in events)
    for number in (1, 2):
        page_events = [event for event in events if event["current"] == number and event["stage"] != "file_completed"]
        assert [(event["stage"], event["completed"]) for event in page_events] == [
            ("rendering", number - 1), ("text_extract", number - 1), ("table_extract", number - 1), ("table_extract", number)]
        assert all(event["item_name"] == f"第 {number} 页" for event in page_events)
    assert events[-1]["stage"] == "file_completed" and events[-1]["current"] == events[-1]["completed"] == 2


@pytest.mark.parametrize("failure", ["exception", "empty"])
def test_failed_ocr_page_counts_as_processed_and_next_page_continues(tmp_path, monkeypatch, failure):
    path = tmp_path / "scan.pdf"
    scanned_pdf(path)
    calls = []

    def recognize(image):
        calls.append(image.size)
        if len(calls) == 1:
            if failure == "exception":
                raise RuntimeError("fixture OCR error")
            return []
        return [{"text": "Second page text", "bbox": [10, 10, 300, 40], "confidence": .9}]

    monkeypatch.setattr("backend.pdf._recognize", recognize)
    events = []
    book = extract_pdf(path, tmp_path / "images", progress=events.append)
    assert len(calls) == 2
    assert book["pages"][0]["status"] == "unavailable" and book["pages"][1]["status"] == "ok"
    first = [event for event in events if event["current"] == 1]
    assert [(event["stage"], event["completed"]) for event in first] == [
        ("rendering", 0), ("text_extract", 0), ("ocr", 0), ("table_extract", 0), ("table_extract", 1)]
    second = [event for event in events if event["current"] == 2 and event["stage"] != "file_completed"]
    assert [(event["stage"], event["completed"]) for event in second] == [
        ("rendering", 1), ("text_extract", 1), ("ocr", 1), ("table_extract", 1), ("table_extract", 2)]
    assert events[-1]["stage"] == "file_completed" and events[-1]["completed"] == 2
    assert any("第 1 页" in warning and ("未能完成" in warning or "未识别" in warning or "未可靠恢复" in warning) for warning in book["warnings"])


@pytest.mark.parametrize("stage,completed", [("rendering", 0), ("ocr", 0), ("table_extract", 1)])
def test_pdf_callback_errors_propagate_instead_of_becoming_render_or_ocr_warnings(tmp_path, monkeypatch, stage, completed):
    path = tmp_path / "scan.pdf"
    scanned_pdf(path, pages=1)
    ocr_calls = []

    def failed_ocr(image):
        ocr_calls.append(True)
        raise ValueError("fixture OCR error")

    monkeypatch.setattr("backend.pdf._recognize", failed_ocr)
    callback_error = RuntimeError("progress storage unavailable")
    events = []

    def report(event):
        events.append(event)
        if event["stage"] == stage and event["completed"] == completed:
            raise callback_error

    with pytest.raises(RuntimeError) as error:
        extract_pdf(path, tmp_path / "images", progress=report)
    assert error.value is callback_error
    assert len(ocr_calls) == (1 if completed else 0)
    assert not any(event["stage"] == "file_completed" for event in events)


def test_excel_callback_error_does_not_report_file_completion(tmp_path):
    path = tmp_path / "book.xlsx"
    workbook = Workbook()
    workbook.save(path)
    callback_error = RuntimeError("progress storage unavailable")
    events = []

    def report(event):
        events.append(event)
        if event["stage"] == "reading_sheet":
            raise callback_error

    with pytest.raises(RuntimeError) as error:
        extract_workbook(path, progress=report)
    assert error.value is callback_error
    assert not any(event["stage"] == "file_completed" for event in events)
