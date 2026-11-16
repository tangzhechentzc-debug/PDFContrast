import copy

from backend.pdf import compare_pdfs
from backend.reports import attach_report_inventory
from backend.review import apply_reviews, review_catalog
from test_report_inventory import book


def test_whole_report_keeps_literal_grid_values_and_decimal_delta(monkeypatch):
    calls = []

    def appearance(old, new, entry):
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
    def appearance(old, new, entry):
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

    def appearance(old, new, entry):
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
    def appearance(old, new, entry):
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
    def appearance(old, new, entry):
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

    def appearance(old, new, entry):
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
