import copy

from backend.pdf import compare_pdfs
from backend.reports import attach_report_inventory
from backend.review import apply_reviews, review_catalog
from test_report_inventory import book


def test_whole_report_keeps_literal_grid_values_and_decimal_delta(monkeypatch):
    calls = []

    def appearance(old, new, entry, *, overlays_by_side=None):
        calls.append(entry)
        return {'diffs': [], 'paragraph_checks': [{'attribute': 'line_spacing', 'status': 'unchanged', 'message': '相邻基线一致', 'measurements': {'unit': 'pt', 'old': [18], 'new': [18]}}], 'page_pairs': [{'old_page': 1, 'new_page': 1, 'basis': 'fixture', 'status': 'confirmed'}],
                'dimensions': {category: {'status': 'unchanged', 'changes': 0, 'issues': 0,
                                         'summary': 'outside grid unchanged', 'limitations': []}
                               for category in ('content', 'structure', 'format', 'layout')}}

    monkeypatch.setattr('backend.pdf_appearance.compare_report_appearance', appearance)
    old, new = book(obj=''), book(obj='已填写工程')
    for value, text in ((old, '1.231'), (new, '1.234')):
        value.update(report_inventory_version=2, warnings=[])
        value['sheets'][0]['cells']['B2'].update(value=text, raw_text=text, display=text, value_type='number')
    old, new = attach_report_inventory(old), attach_report_inventory(new)
    result = compare_pdfs(old, new, {'pairs': []})
    assert result['reports'][0]['presence'] == 'shared'
    assert result['reports'][0]['paragraph_checks'][0]['measurements']['old'] == [18]
    assert result['reports'][0]['comparison_status'] == 'changed'
    amount = next(d for d in result['diffs'] if d.get('row_key') == 'X')
    assert amount['old_value'] == '1.231' and amount['new_value'] == '1.234' and amount['delta'] == '0.003'
    assert amount['category'] == 'content' and amount['old_source']['page'] == amount['new_source']['page'] == 1
    assert calls[0]['content_exclusions']['old'][0]['bbox'][1] > .3
    assert result['reports'][0]['dimensions']['content']['changes'] == 1
    changed = copy.deepcopy(new)
    changed['sheets'][0]['cells']['B2'].update(value='1.231', raw_text='1.2310', display='1.2310')
    written = compare_pdfs(old, changed, {'pairs': []})
    literal = next(d for d in written['diffs'] if d.get('row_key') == 'X')
    assert literal['old_value'] == '1.231' and literal['new_value'] == '1.2310'
    assert written['summary']['changes'] == 1


def test_v2_text_review_does_not_require_inventing_a_table():
    value = book()
    value.update(report_inventory_version=2, warnings=[], review_namespace='full-report-text')
    value['pages'][0].update(method='ocr', structure_unconfirmed=True)
    sheet = value['sheets'][0]
    sheet.update(structure='text_blocks', method='ocr')
    cell = sheet['cells']['B2']
    cell.update(raw_text='识别说明', display='识别说明', value='识别说明')
    cell['pdf_source']['method'] = 'ocr'
    current = apply_reviews(attach_report_inventory(value), [])
    assert current['pages'][0]['reviewable']
    target = review_catalog(current)[0]
    verified = apply_reviews(current, [
        {'revision': 1, 'action': 'cell_update', 'target_id': target['id'], 'value': '校正说明', 'reason': '对照原件'},
        {'revision': 2, 'action': 'page_verify', 'page': 1, 'reason': '文字与漏项已核对'}])
    assert verified['pages'][0]['reviewed']
    assert verified['pages'][0]['structure_unconfirmed']
    assert review_catalog(verified)[0]['current_text'] == '校正说明'
    assert value['sheets'][0]['cells']['B2']['raw_text'] == '识别说明'


def test_pdf_key_spacing_does_not_invent_row_add_remove_and_preserves_text_boxes(monkeypatch):
    def appearance(old, new, entry, *, overlays_by_side=None):
        return {'diffs': [], 'page_pairs': [{'old_page': 1, 'new_page': 1, 'basis': 'fixture', 'status': 'confirmed'}],
                'dimensions': {category: {'status': 'unchanged', 'changes': 0, 'issues': 0, 'summary': 'fixture', 'limitations': []}
                               for category in ('content', 'structure', 'format', 'layout')}}

    monkeypatch.setattr('backend.pdf_appearance.compare_report_appearance', appearance)
    old, new = book(), book()
    for value, label in ((old, '合 计'), (new, '合计')):
        value.update(report_inventory_version=2, warnings=[])
        cell = value['sheets'][0]['cells']['A2']
        cell.update(value=label, raw_text=label, display=label, value_type='string')
        cell['pdf_source']['content_sources'] = [
            {'page': 1, 'bbox': [.2, .4, .3, .42]}, {'page': 1, 'bbox': [.32, .4, .4, .42]}]
    old, new = attach_report_inventory(old), attach_report_inventory(new)
    result = compare_pdfs(old, new, {'pairs': []})
    assert not any(d['kind'] in ('row_removed', 'row_added') for d in result['diffs'])
    spacing = next(d for d in result['diffs'] if d['kind'] == 'text_whitespace_changed')
    assert (spacing['old_value'], spacing['new_value']) == ('合 计', '合计')
    assert spacing['category'] == spacing['layer'] == 'format'
    assert spacing['old_source']['bbox'] == [.2, .4, .4, .42]
    assert spacing['old_source']['highlight_boxes'] == [[.2, .4, .3, .42], [.32, .4, .4, .42]]
    assert spacing['old_source']['cell_bbox'] != spacing['old_source']['bbox']
    assert result['reports'][0]['dimensions']['format']['changes'] == 1
    assert old['sheets'][0]['cells']['A2']['value'] == '合 计'


def test_global_extraction_blocker_is_not_a_paired_source_for_single_candidate(monkeypatch):
    old_source = {'kind': 'pdf', 'page': 1, 'bbox': [.1, .1, .9, .2], 'method': 'text', 'side': 'old'}
    unrelated = {'kind': 'pdf', 'page': 9, 'bbox': [.1, .5, .3, .6], 'method': 'ocr', 'side': 'new'}
    entry = {'id': 'candidate', 'title': 'Unpaired report', 'type_code': 'C.6.2', 'presence': 'uncertain',
             'confirmation': 'pending', 'old': {'id': 'old-candidate', 'pages': [1], 'source': old_source}, 'new': None,
             'basis': 'duplicate candidates', 'object': ''}
    issue = {'id': 'global-blocker', 'type': 'extraction', 'message': 'unresolved opposite content',
             'report_ids': ['candidate'], 'sources': [old_source, unrelated], 'pages': [1, 9], 'actions': ['reprocess']}
    inventory = {'reports': [entry], 'issues': [issue], 'report_inventory_version': 2, 'type_inventory': {}, 'non_report_regions': []}
    monkeypatch.setattr('backend.reports.compare_inventory', lambda *args: copy.deepcopy(inventory))
    value = {'report_inventory_version': 2, 'reports': [], 'sheets': [], 'pages': [], 'warnings': []}
    result = compare_pdfs(value, value, {'pairs': []})
    event = next(d for d in result['diffs'] if d['kind'] == 'report_review')
    assert {key: event['old_source'][key] for key in old_source} == old_source
    assert event['old_source']['location_granularity'] == 'region'
    assert event['new_source'] is None
    assert result['issues'][0]['sources'] == [old_source, unrelated]


def test_name_key_change_uses_unique_sequence_and_unchanged_other_fields(monkeypatch):
    from test_report_inventory import sheet

    def appearance(old, new, entry, *, overlays_by_side=None):
        return {'diffs': [], 'page_pairs': [{'old_page': 1, 'new_page': 1, 'basis': 'fixture', 'status': 'confirmed'}],
                'dimensions': {category: {'status': 'unchanged', 'changes': 0, 'issues': 0, 'summary': 'fixture', 'limitations': []}
                               for category in ('content', 'structure', 'format', 'layout')}}
    monkeypatch.setattr('backend.pdf_appearance.compare_report_appearance', appearance)
    values = []
    for name in ('发包人要求的其他项目费', '发包人要求的其他项目'):
        value = book()
        value.update(report_inventory_version=2, warnings=[])
        value['sheets'] = [sheet('p1', headers=('序号', '项目名称', '备注'))]
        from backend.excel import empty_cell
        for column, text in enumerate(('5', name, '详见同一参考表'), 1):
            cell = empty_cell(2, column)
            cell.update(value=text, display=text, raw_text=text, value_type='string',
                        pdf_source={'kind': 'pdf', 'page': 1, 'bbox': [.1*column, .5, .1*column+.09, .55], 'method': 'text'})
            value['sheets'][0]['cells'][cell['address']] = cell
        values.append(attach_report_inventory(value))
    result = compare_pdfs(*values, {'pairs': []})
    assert not any(d['kind'] in ('row_added', 'row_removed') for d in result['diffs'])
    difference = next(d for d in result['diffs'] if d['kind'] == 'value_changed')
    assert (difference['old_value'], difference['new_value']) == ('发包人要求的其他项目费', '发包人要求的其他项目')
    assert difference['old_source']['cell'] == difference['new_source']['cell'] == 'B2'
    assert '序号' in difference['message']


def test_grid_header_is_not_counted_again_after_native_cell_comparison(monkeypatch):
    def appearance(old, new, entry, *, overlays_by_side=None):
        diff = {'kind': 'text_changed', 'field': '文字内容', 'old_value': '金额（元）', 'new_value': '金额 (元)',
                'category': 'content', 'layer': 'content', 'severity': 'change', 'message': 'native header',
                'old_source': {'page': 1, 'bbox': [.3, .33, .5, .36]}, 'new_source': {'page': 1, 'bbox': [.3, .33, .5, .36]},
                'measurements': {'old': {'fragment_text': '金额（元）', 'page': 1, 'cell': {'row': 1, 'column': 2}},
                                 'new': {'fragment_text': '金额 (元)', 'page': 1, 'cell': {'row': 1, 'column': 2}}}}
        return {'diffs': [diff], 'page_pairs': [{'old_page': 1, 'new_page': 1, 'basis': 'fixture', 'status': 'confirmed'}],
                'dimensions': {category: {'status': 'changed' if category == 'content' else 'unchanged', 'changes': 1 if category == 'content' else 0,
                                         'issues': 0, 'summary': 'fixture', 'limitations': []}
                               for category in ('content', 'structure', 'format', 'layout')}}
    monkeypatch.setattr('backend.pdf_appearance.compare_report_appearance', appearance)
    old, new = book(), book()
    for value, label in ((old, '金额（元）'), (new, '金额 (元)')):
        value.update(report_inventory_version=2, warnings=[])
        value['sheets'][0]['cells']['B1'].update(value=label, raw_text=label, display=label)
    result = compare_pdfs(attach_report_inventory(old), attach_report_inventory(new), {'pairs': []})
    content = [d for d in result['diffs'] if d['category'] == 'content']
    assert len(content) == 1 and content[0]['message'] == 'native header'


def test_body_spacing_is_not_counted_again_after_native_cell_comparison(monkeypatch):
    def appearance(old, new, entry, *, overlays_by_side=None):
        diff = {'kind': 'text_whitespace_changed', 'field': '原文空白', 'old_value': '合    计', 'new_value': '合计',
                'category': 'format', 'layer': 'format', 'severity': 'change', 'message': 'native spacing',
                'old_source': {'page': 1, 'bbox': [.2, .4, .4, .42]}, 'new_source': {'page': 1, 'bbox': [.2, .4, .3, .42]},
                'measurements': {'old': {'fragment_text': '合    计', 'cell': {'row': 2, 'column': 1}},
                                 'new': {'fragment_text': '合计', 'cell': {'row': 2, 'column': 1}}}}
        return {'diffs': [diff], 'page_pairs': [{'old_page': 1, 'new_page': 1, 'basis': 'fixture', 'status': 'confirmed'}],
                'dimensions': {category: {'status': 'changed' if category == 'format' else 'unchanged', 'changes': 1 if category == 'format' else 0,
                                         'issues': 0, 'summary': 'fixture', 'limitations': []}
                               for category in ('content', 'structure', 'format', 'layout')}}
    monkeypatch.setattr('backend.pdf_appearance.compare_report_appearance', appearance)
    old, new = book(), book()
    for value, label in ((old, '合 计'), (new, '合计')):
        value.update(report_inventory_version=2, warnings=[])
        value['sheets'][0]['cells']['A2'].update(value=label, raw_text=label, display=label, value_type='string')
    result = compare_pdfs(attach_report_inventory(old), attach_report_inventory(new), {'pairs': []})
    changes = [d for d in result['diffs'] if d['kind'] == 'text_whitespace_changed']
    assert len(changes) == 1
    assert changes[0]['old_value'] == '合    计'
    assert result['reports'][0]['dimensions']['format']['changes'] == 1


def test_location_granularity_distinguishes_whole_frame_from_unconfirmed_text(monkeypatch):
    frame = {'page': 1, 'bbox': [.1, .2, .9, .9]}
    unavailable = {'page': 1, 'bbox': [0, 0, 1, 1], 'location_precision': 'page'}
    glyph = {'page': 1, 'bbox': [.2, .3, .25, .32], 'location_precision': 'text'}
    originals = copy.deepcopy((frame, unavailable, glyph))

    def appearance(old, new, entry, *, overlays_by_side=None):
        events = [
            {'kind': 'form_geometry_changed', 'category': 'layout', 'old_source': frame, 'new_source': frame},
            {'kind': 'text_changed', 'category': 'content', 'old_source': unavailable, 'new_source': glyph},
        ]
        for event in events:
            event.update(severity='change', layer=event['category'], field='fixture', old_value=None, new_value='changed', message='fixture')
        return {'diffs': events, 'page_pairs': [], 'dimensions': {
            category: {'status': 'changed', 'changes': 0, 'issues': 0, 'summary': 'fixture', 'limitations': []}
            for category in ('content', 'structure', 'format', 'layout')}}

    monkeypatch.setattr('backend.pdf_appearance.compare_report_appearance', appearance)
    old, new = book(), book()
    for value in (old, new):
        value.update(report_inventory_version=2, warnings=[])
    result = compare_pdfs(attach_report_inventory(old), attach_report_inventory(new), {'pairs': []})
    layout = next(d for d in result['diffs'] if d['kind'] == 'form_geometry_changed')
    text = next(d for d in result['diffs'] if d['kind'] == 'text_changed')
    assert layout['old_source']['location_granularity'] == 'frame'
    assert '完整闭合框' in layout['old_source']['location_reason']
    assert text['old_source']['location_granularity'] == 'unconfirmed'
    assert '文字位置未确认' in text['old_source']['location_reason']
    assert text['new_source']['location_granularity'] == 'text'
    assert (frame, unavailable, glyph) == originals
    assert layout['old_source']['bbox'] == frame['bbox']


def test_extract_pdf_reuses_single_table_extraction_and_keeps_original_index(tmp_path, monkeypatch):
    import pdfplumber
    from reportlab.pdfgen import canvas
    from backend.pdf import extract_pdf

    pdf_path = tmp_path / 'tables.pdf'
    writer = canvas.Canvas(str(pdf_path), pagesize=(400, 600))
    # 第 1 页：上方放置一个 1x2 小表（应被结构表过滤），下方放置一个 2x2 有效表
    for x in (40, 140, 240):
        writer.line(x, 500, x, 540)
    for y in (500, 540):
        writer.line(40, y, 240, y)
    writer.drawString(50, 515, 'A')
    writer.drawString(150, 515, 'B')

    for x in (40, 140, 240):
        writer.line(x, 300, x, 380)
    for y in (300, 340, 380):
        writer.line(40, y, 240, y)
    for x, y, label in ((50, 355, 'Code'), (150, 355, 'Amount'), (50, 315, '01'), (150, 315, '120')):
        writer.drawString(x, y, label)
    writer.showPage()

    # 第 2 页：不含任何表格（验证传入空表列表 [] 时也不会重复触发 find_tables）
    writer.drawString(40, 500, 'Page two without tables')
    writer.showPage()
    writer.save()

    find_calls = []
    extract_calls = []
    original_find_tables = pdfplumber.page.Page.find_tables
    original_extract = pdfplumber.table.Table.extract

    def counted_find_tables(self, *args, **kwargs):
        find_calls.append(self.page_number)
        return original_find_tables(self, *args, **kwargs)

    def counted_extract(self, *args, **kwargs):
        extract_calls.append(1)
        return original_extract(self, *args, **kwargs)

    monkeypatch.setattr(pdfplumber.page.Page, 'find_tables', counted_find_tables)
    monkeypatch.setattr(pdfplumber.table.Table, 'extract', counted_extract)

    extracted = extract_pdf(pdf_path, tmp_path / 'assets')
    assert find_calls == [1, 2]
    assert len(extract_calls) == 2
    # 跳过第 1 个 1x2 表后，第 2 个表仍保留原始 1-based 序号
    table_sheets = [s for s in extracted['sheets'] if s.get('structure') == 'table' and not s.get('member_sheets')]
    assert [s['name'] for s in table_sheets] == ['第 1 页 · 表 2']
    assert all('raw_tables' not in page for page in extracted['pages'])


def test_extract_page_appearance_two_args_and_omits_gray_png_base64(tmp_path):
    import pdfplumber
    from reportlab.pdfgen import canvas
    from backend.pdf_appearance import extract_page_appearance

    pdf_path = tmp_path / 'single.pdf'
    writer = canvas.Canvas(str(pdf_path), pagesize=(300, 400))
    writer.drawString(40, 320, 'Hello Appearance')
    writer.showPage()
    writer.save()

    with pdfplumber.open(pdf_path) as document:
        page = document.pages[0]
        page_image = page.to_image(resolution=96, antialias=True, force_mediabox=True)
        appearance = extract_page_appearance(page, page_image)

    render = appearance['render']
    assert 'gray_png_base64' not in render
    assert render['width'] > 0 and render['height'] > 0
    assert isinstance(render['rgb_png_base64'], str) and len(render['rgb_png_base64']) > 0
    assert isinstance(render['sha256'], str) and len(render['sha256']) == 64


def test_visual_regions_supports_new_rgb_only_and_legacy_gray_only_snapshots(tmp_path):
    import base64
    import io
    import pdfplumber
    from PIL import Image
    from reportlab.pdfgen import canvas
    from backend.pdf_appearance import _visual_regions, extract_page_appearance

    views = []
    for name, color in (('old', (0.1, 0.1, 0.1)), ('new', (0.8, 0.2, 0.2))):
        pdf_path = tmp_path / f'{name}.pdf'
        writer = canvas.Canvas(str(pdf_path), pagesize=(200, 200))
        writer.setFillColorRGB(*color)
        writer.rect(40, 60, 120, 80, fill=1, stroke=0)
        writer.showPage()
        writer.save()
        with pdfplumber.open(pdf_path) as document:
            page = document.pages[0]
            page_image = page.to_image(resolution=96, antialias=True, force_mediabox=True)
            appearance = extract_page_appearance(page, page_image)
        views.append({
            'number': 1,
            'scopes': [[0, 0, 1, 1]],
            'images': [],
            'original_lines': [],
            'appearance': appearance,
        })

    # 新快照（不含 gray_png_base64）应正常输出视觉差异区域
    assert 'gray_png_base64' not in views[0]['appearance']['render']
    rgb_regions = _visual_regions(views[0], views[1], residual=False)
    assert rgb_regions

    # 构造仅含 gray_png_base64、无 rgb_png_base64 的旧快照，验证惰性回退兼容性
    legacy_views = copy.deepcopy(views)
    for view in legacy_views:
        render = view['appearance']['render']
        rgb_bytes = base64.b64decode(render.pop('rgb_png_base64'))
        gray_image = Image.open(io.BytesIO(rgb_bytes)).convert('L')
        buffer = io.BytesIO()
        gray_image.save(buffer, format='PNG')
        render['gray_png_base64'] = base64.b64encode(buffer.getvalue()).decode()

    gray_regions = _visual_regions(legacy_views[0], legacy_views[1], residual=False)
    assert gray_regions
