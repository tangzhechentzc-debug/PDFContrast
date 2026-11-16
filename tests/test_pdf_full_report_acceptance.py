"""Independent full-report acceptance fixtures; no personal sample paths."""
import copy

import pdfplumber
import pytest
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from backend.pdf import compare_pdfs, extract_pdf, suggest_pdf_pairs
from scripts.make_pdf_examples import register_font


def document(path, *, size=(400, 600), text='Same explanatory text', font='Helvetica',
             font_size=12, line_width=.3, color=(0, 0, 0), title=True, grid=False):
    c = canvas.Canvas(str(path), pagesize=size)
    chinese, _ = register_font(None)
    if title:
        c.setFont(chinese, 12)
        c.drawString(30, size[1] - 30, 'A.4 工程量清单计算规则说明')
        c.drawString(30, size[1] - 50, '工程名称：')
    c.setStrokeColorRGB(*color)
    c.setLineWidth(line_width)
    c.rect(30, 50, 300, size[1] - 120, fill=0)
    if grid in (True, 'rows'):
        c.line(30, 150, 330, 150)
    if grid in (True, 'columns'):
        c.line(130, 50, 130, size[1] - 70)
    if text:
        c.setFont(font, font_size)
        c.drawString(40, size[1] - 90, text)
    c.save()
    return path


def read_pair(tmp_path, old_options=None, new_options=None):
    old = extract_pdf(document(tmp_path / 'old.pdf', **(old_options or {})), tmp_path / 'old-images')
    new = extract_pdf(document(tmp_path / 'new.pdf', **(new_options or {})), tmp_path / 'new-images')
    assert old['report_inventory_version'] == new['report_inventory_version'] == 2
    return old, new


def compare(old, new, pairs=None):
    return compare_pdfs(old, new, {'pairs': suggest_pdf_pairs(old, new) if pairs is None else pairs,
                                  'absolute_tolerance': '0', 'ignore_outer_whitespace': True,
                                  'compare_formatting': False, 'compare_formulas': False})


def changed(result, category):
    return [d for d in result['diffs'] if d.get('category') == category and d['severity'] == 'change']


def test_same_text_portrait_landscape_is_layout_not_content_change(tmp_path):
    old, new = read_pair(tmp_path, new_options={'size': (700, 450)})
    result = compare(old, new)
    orientation = next(d for d in changed(result, 'layout') if d['kind'] == 'page_orientation_changed')
    assert '400.00 × 600.00 pt' in orientation['old_value'] and '纵向' in orientation['old_value']
    assert '700.00 × 450.00 pt' in orientation['new_value'] and '横向' in orientation['new_value']
    assert not changed(result, 'content')
    assert old['pages'][0]['rotation'] == new['pages'][0]['rotation'] == 0
    assert old['pages'][0]['width'] < old['pages'][0]['height']
    assert new['pages'][0]['width'] > new['pages'][0]['height']
    assert all(d['old_source']['page'] == d['new_source']['page'] == 1 for d in changed(result, 'layout'))


def test_empty_template_frame_filling_is_actual_content_change(tmp_path):
    old, new = read_pair(tmp_path, old_options={'text': ''}, new_options={'text': 'Added explanation 2026'})
    result = compare(old, new)
    content = changed(result, 'content')
    assert content and any('Added explanation 2026' in str(d['new_value']) for d in content)
    assert all(d['old_source'] and d['new_source'] for d in content)
    assert not any(d['kind'] in ('report_added', 'report_removed') for d in result['diffs'])


@pytest.mark.parametrize('attribute,value', [('font', 'Times-Roman'), ('font_size', 18),
                                            ('line_width', 1.2), ('color', (1, 0, 0))])
def test_single_native_style_change_is_format_evidence(tmp_path, attribute, value):
    old, new = read_pair(tmp_path, new_options={attribute: value})
    result = compare(old, new)
    formats = changed(result, 'format')
    expected = {'font': 'font_changed', 'font_size': 'font_size_changed', 'line_width': 'border_style_changed', 'color': 'border_style_changed'}[attribute]
    assert any(d['kind'] == expected for d in formats), (attribute, result)
    assert not changed(result, 'content')
    assert all(d['old_value'] != d['new_value'] and d['old_source'] and d['new_source'] for d in formats)
    assert all(d['old_source']['bbox'] and d['new_source']['bbox'] for d in formats)


def test_identical_source_and_repeat_extraction_have_no_changes(tmp_path):
    path = document(tmp_path / 'same.pdf')
    old, new = extract_pdf(path, tmp_path / 'a'), extract_pdf(path, tmp_path / 'b')
    result = compare(old, new)
    assert result['summary']['changes'] == 0
    assert not any(changed(result, category) for category in ('content', 'structure', 'format', 'layout'))
    assert result['reports'] and all(r['confirmation'] == 'confirmed' for r in result['reports'])


def test_unknown_pure_frame_needs_manual_report_pair(tmp_path):
    old, new = read_pair(tmp_path, old_options={'text': '', 'title': False}, new_options={'text': '', 'title': False, 'line_width': 1.2})
    initial = compare(old, new)
    assert old['reports'] and new['reports']
    assert all(r['confirmation'] == 'pending' for r in initial['reports'])
    assert not changed(initial, 'format')
    pair = {'id': 'manual-frame', 'old_report_id': old['reports'][0]['id'], 'new_report_id': new['reports'][0]['id'],
            'pairing_confirmed': True, 'confirmation_reason': '人工对照确认同一空白说明框', 'columns': [], 'mode': 'key'}
    confirmed = compare(old, new, [pair])
    assert changed(confirmed, 'format')
    assert confirmed['reports'][0]['match_method'] == 'manual'


def test_grid_geometry_change_on_same_paper_is_structure_not_orientation(tmp_path):
    old, new = read_pair(tmp_path, old_options={'grid': 'rows'}, new_options={'grid': 'columns'})
    result = compare(old, new)
    assert old['pages'][0]['width'] == new['pages'][0]['width']
    assert old['pages'][0]['height'] == new['pages'][0]['height']
    assert any(d['kind'] in ('table_structure_changed', 'form_structure_changed') for d in changed(result, 'structure'))
    assert not any('方向' in d['field'] or 'orientation' in d['field'].lower() for d in changed(result, 'layout'))


def test_failed_visible_scan_is_unknown_and_never_treated_as_blank(tmp_path, monkeypatch):
    image = Image.new('RGB', (300, 200), 'white')
    ImageDraw.Draw(image).text((20, 40), 'Visible source content', fill='black')
    path = tmp_path / 'failed.pdf'
    c = canvas.Canvas(str(path), pagesize=(400, 600)); c.drawImage(ImageReader(image), 20, 300, width=300, height=200); c.save()
    def fail(_image):
        raise RuntimeError('controlled OCR failure')
    monkeypatch.setattr('backend.pdf._recognize', fail)
    failed = extract_pdf(path, tmp_path / 'failed-images')
    blank = extract_pdf(document(tmp_path / 'blank.pdf', title=False, text=''), tmp_path / 'blank-images')
    result = compare(failed, blank)
    assert failed['pages'][0]['status'] == 'unavailable'
    assert not failed['pages'][0].get('blank')
    assert result['summary']['issues'] > 0 and result['summary']['changes'] == 0
    assert not any(r['confirmation'] == 'confirmed' and r['presence'] in ('old_only', 'new_only') for r in result['reports'])


def test_font_subset_prefix_alone_does_not_change_format(tmp_path):
    from backend.pdf_appearance import extract_page_appearance
    path = document(tmp_path / 'font-prefix.pdf')
    old = extract_pdf(path, tmp_path / 'images')
    new = copy.deepcopy(old)
    # Same native glyph metrics/font; only the arbitrary six-letter subset tag changes.
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        for char in page.chars:
            char['fontname'] = 'ABCDEF+' + char['fontname'].split('+')[-1]
        first = extract_page_appearance(page, page.to_image(resolution=96, force_mediabox=True))
        for char in page.chars:
            char['fontname'] = 'UVWXYZ+' + char['fontname'].split('+')[-1]
        second = extract_page_appearance(page, page.to_image(resolution=96, force_mediabox=True))
    old['pages'][0]['appearance'] = first
    new['pages'][0]['appearance'] = second
    new['document_sha256'] = 'separate-artifact-same-glyph-metrics'
    assert not changed(compare(old, new), 'format')


def test_absent_whole_report_is_one_completeness_event(tmp_path):
    chinese, _ = register_font(None)
    books = []
    for side, codes in [('old', ['A.3', 'A.4']), ('new', ['A.3'])]:
        path = tmp_path / f'{side}-inventory.pdf'
        c = canvas.Canvas(str(path), pagesize=(400, 600))
        for code in codes:
            c.setFont(chinese, 12)
            c.drawString(30, 570, code + (' 招标工程量清单编制说明' if code == 'A.3' else '工程量清单计算规则说明'))
            c.drawString(30, 550, '工程名称：测试项目')
            c.rect(30, 50, 300, 480)
            c.showPage()
        c.save()
        books.append(extract_pdf(path, tmp_path / f'{side}-inventory-images'))
    result = compare(*books)
    removed = [d for d in result['diffs'] if d['kind'] == 'report_removed']
    assert len(removed) == 1 and removed[0]['category'] == 'completeness'
    assert removed[0]['old_source']['page'] == 2 and removed[0]['new_source'] is None
    assert not any(d['kind'] in ('row_removed', 'column_removed') for d in result['diffs'])
