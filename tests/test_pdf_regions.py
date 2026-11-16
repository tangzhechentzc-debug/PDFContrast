from pathlib import Path

import pdfplumber
import pytest
from openpyxl.utils import get_column_letter
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from backend.pdf import compare_pdfs, extract_pdf, suggest_pdf_pairs
from backend.review import apply_reviews, review_catalog, structure_tables


pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))


def report_pdf(path, value='10', badge=False):
    doc = canvas.Canvas(str(path), pagesize=(600, 800))
    doc.setFont('STSong-Light', 12)
    doc.drawString(50, 740, '表 A.1 项目费用明细表')
    doc.drawString(50, 710, '工程名称：工程甲')
    for x in (50, 200, 350):
        doc.line(x, 500, x, 620)
    for y in (500, 540, 580, 620):
        doc.line(50, y, 350, y)
    for x, y, text in ((60, 595, '项目编码'), (210, 595, '金额'), (60, 555, '001'), (210, 555, value),
                       (60, 515, '002'), (210, 515, '20')):
        doc.drawString(x, y, text)
    if badge:
        image = Image.new('RGB', (40, 40), 'white')
        ImageDraw.Draw(image).rectangle((5, 5, 35, 35), fill='black')
        doc.drawImage(ImageReader(image), 500, 700, width=20, height=20)
    doc.showPage()
    doc.save()


def test_small_image_ocr_cannot_replace_native_table_or_native_identity(tmp_path, monkeypatch):
    path = tmp_path / 'native.pdf'
    report_pdf(path, badge=True)
    crops = []

    def recognize(image):
        crops.append(image.size)
        return [{'text': '标记', 'bbox': [3, 3, image.width - 3, image.height - 3], 'confidence': .9}]

    monkeypatch.setattr('backend.pdf._recognize', recognize)
    book = extract_pdf(path, tmp_path / 'images')
    table = next(s for s in book['sheets'] if s['structure'] == 'table')
    assert crops == [(40, 40)]
    assert book['report_inventory_version'] == 2
    assert '工程名称：工程甲' in book['pages'][0]['native_text']
    assert table['structure_source']['method'] == 'pdf_lines'
    assert all(c['pdf_source']['method'] == 'text' for c in table['cells'].values())
    assert table['cells']['B2']['value'] == '10'
    assert book['reports'][0]['identity_reliable']
    assert any(line['text'] == '标记' and line['method'] == 'ocr' for line in book['pages'][0]['evidence_lines'])


def test_outside_failed_region_is_structured_issue_without_blocking_native_value_comparison(tmp_path, monkeypatch):
    old_path, new_path = tmp_path / 'old.pdf', tmp_path / 'new.pdf'
    report_pdf(old_path, '10', badge=True)
    report_pdf(new_path, '11', badge=True)

    def failed(image):
        raise RuntimeError('fixture region failure')

    monkeypatch.setattr('backend.pdf._recognize', failed)
    old = extract_pdf(old_path, tmp_path / 'old')
    new = extract_pdf(new_path, tmp_path / 'new')
    pairs = suggest_pdf_pairs(old, new)
    assert len(pairs) == 1
    result = compare_pdfs(old, new, {'pairs': pairs})
    values = [d for d in result['diffs'] if d['layer'] == 'content']
    assert len(values) == 1 and values[0]['delta'] == '1'
    assert values[0]['old_source']['method'] == values[0]['new_source']['method'] == 'text'
    region_issues = [i for i in result['issues'] if i['type'] == 'region_unresolved']
    assert len(region_issues) == 2
    assert {i['side'] for i in region_issues} == {'old', 'new'}
    assert all(i['sources'][0]['bbox'][0] > .8 and 'reprocess' in i['actions'] for i in region_issues)


def test_invalid_native_body_remains_editable_when_regional_ocr_fails(tmp_path, monkeypatch):
    path = tmp_path / 'bad-map.pdf'
    doc = canvas.Canvas(str(path), pagesize=(600, 800))
    doc.drawString(40, 700, 'BrokenRegion')
    doc.showPage()
    doc.save()
    original = pdfplumber.page.Page.extract_words

    def bad_mapping(page, **kwargs):
        return [{**word, 'text': '(cid:0)' if word['text'] == 'BrokenRegion' else word['text']} for word in original(page, **kwargs)]

    monkeypatch.setattr(pdfplumber.page.Page, 'extract_words', bad_mapping)
    monkeypatch.setattr('backend.pdf._recognize', lambda image: [])
    book = extract_pdf(path, tmp_path / 'images')
    body = next(s for s in book['sheets'] if s['structure'] == 'text_blocks')
    assert body['method'] == 'ocr'
    cell = body['cells']['A2']
    assert cell['value'] == '(cid:0)' and cell['requires_review']
    assert cell['pdf_source']['method'] == 'text' and cell['pdf_source']['unresolved_text']
    assert cell['pdf_source']['region_ids'] == ['p1-ocr-1']
    applied = apply_reviews(book, [])
    assert structure_tables(applied)[0]['editable']
    assert review_catalog(applied)[0]['current_text'] == '(cid:0)'
    result = compare_pdfs(applied, applied, {'pairs': []})
    assert any(i['type'] == 'region_unresolved' for i in result['issues'])
    assert result['summary']['changes'] == 0


def test_native_merged_header_preserves_spans_and_normalized_geometry(tmp_path):
    path = tmp_path / 'merged.pdf'
    doc = canvas.Canvas(str(path), pagesize=(400, 500))
    doc.setFont('STSong-Light', 12)
    for x in (50, 150, 350):
        doc.line(x, 300, x, 450)
    doc.line(250, 300, 250, 400)
    for y in (300, 350, 450):
        doc.line(50, y, 350, y)
    doc.line(150, 400, 350, 400)
    for x, y, text in ((60, 395, '项目编码'), (200, 425, '金额'), (160, 375, '单价'), (260, 375, '合价'),
                       (60, 325, '001'), (160, 325, '10'), (260, 325, '20')):
        doc.drawString(x, y, text)
    doc.showPage()
    doc.save()
    book = extract_pdf(path, tmp_path / 'images')
    table = next(s for s in book['sheets'] if s['structure'] == 'table')
    assert set(table['merged_ranges']) == {'A1:A2', 'B1:C1'}
    assert table['cells']['A2']['merged_range'] == 'A1:A2'
    geometry = next(item for item in table['cell_geometry'] if item.get('merged_range') == 'B1:C1')
    assert (geometry['row_start'], geometry['row_end'], geometry['column_start'], geometry['column_end']) == (1, 1, 2, 3)
    assert geometry['bbox'] == pytest.approx([.375, .1, .875, .2])


def test_titleless_new_import_cannot_fall_back_to_legacy_content_comparison(tmp_path):
    path = tmp_path / 'plain.pdf'
    doc = canvas.Canvas(str(path))
    doc.drawString(40, 700, 'Unclassified body without report identity')
    doc.showPage()
    doc.save()
    book = extract_pdf(path, tmp_path / 'images')
    result = compare_pdfs(book, book, {'pairs': []})
    assert result['report_inventory_version'] == 2
    assert not any(diff['layer'] == 'content' for diff in result['diffs'])
    assert result['summary']['issues'] > 0
    assert result['non_report_regions']
