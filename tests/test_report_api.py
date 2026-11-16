import copy
import hashlib
import json
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook

import backend.app as app_module
from backend.app import create_app, encode, export_report
from test_import_progress_api import PDF_BYTES, terminal_status
from test_review import workbook


def setup_job(client):
    response = client.post('/api/jobs', files={
        f'{side}_file': (f'{side}.pdf', content, 'application/pdf') for side, content in PDF_BYTES.items()})
    assert response.status_code == 201, response.text
    return response.json()


def test_reprocess_keeps_originals_results_and_reviews(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, 'extract_pdf', lambda *args, **kwargs: workbook())
    with TestClient(create_app(tmp_path)) as client:
        original = setup_job(client)
        store = client.app.state.store
        old_id = original['id']
        event = {'revision': 1, 'side': 'old', 'action': 'cell_verify', 'reason': 'original review'}
        historical = {'id': 'historical-run', 'job_id': old_id, 'review_revision': 1}
        with store.connect() as db:
            db.execute('INSERT INTO review_events VALUES (?,?,?)', (old_id, 1, encode(event)))
            db.execute('INSERT INTO runs VALUES (?,?,?)', ('historical-run', old_id, encode(historical)))
        originals = {side: (tmp_path / old_id / f'{side}.json').read_bytes() for side in ('old', 'new')}
        result = client.post(f'/api/jobs/{old_id}/reprocess')
        assert result.status_code == 202, result.text
        new_id = result.json()['id']
        assert new_id != old_id and result.json()['parent_job_id'] == old_id
        ready = terminal_status(client, new_id)
        assert ready['status'] == 'ready', ready
        assert store.job(new_id)['parent_job_id'] == old_id
        assert store.run('historical-run') == historical
        assert store.reviews(old_id) == (1, [event])
        assert store.reviews(new_id) == (0, [])
        for side in ('old', 'new'):
            assert (tmp_path / old_id / f'{side}.json').read_bytes() == originals[side]
            assert (tmp_path / new_id / f'{side}.pdf').read_bytes() == PDF_BYTES[side]
            assert store.job(new_id)['files'][side]['sha256'] == hashlib.sha256(PDF_BYTES[side]).hexdigest()


def test_reprocess_refuses_changed_original(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, 'extract_pdf', lambda *args, **kwargs: workbook())
    with TestClient(create_app(tmp_path)) as client:
        job = setup_job(client)
        (tmp_path / job['id'] / 'new.pdf').write_bytes(b'%PDF-changed')
        result = client.post(f"/api/jobs/{job['id']}/reprocess")
        assert result.status_code == 422 and '校验值' in result.text
        with client.app.state.store.connect() as db:
            assert db.execute('SELECT count(*) FROM imports').fetchone()[0] == 0


def test_report_snapshot_filter_and_export(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, 'extract_pdf', lambda *args, **kwargs: workbook())
    report = {'id': 'r1', 'title': '价款表', 'type_code': 'A.1', 'object': '工程一',
              'presence': 'uncertain', 'confirmation': 'pending', 'old': {'pages': [1]}, 'new': None,
              'basis': '对侧区域提取失败', 'blocking_reasons': ['不能确认缺失'],
              'content_status': 'pending', 'change_count': 0}
    issue = {'id': 'issue1', 'type': 'extraction', 'message': '区域提取失败', 'report_ids': ['r1'],
             'side': 'new', 'pages': [2], 'sources': [], 'actions': ['reprocess']}
    diff = {'id': 'diff-1', 'kind': 'value_changed', 'table_id': 'pair1', 'table_name': '其他可靠表',
            'report_id': 'r2', 'layer': 'content', 'row_key': 'x', 'field': '金额',
            'old_value': '1', 'new_value': '2', 'delta': '1', 'severity': 'change',
            'message': '数值变化', 'old_source': None, 'new_source': None}
    other = {**diff, 'id': 'diff-2', 'report_id': 'r1', 'layer': 'uncertainty', 'severity': 'issue'}
    mislabeled_issue = {**diff, 'id': 'diff-3', 'report_id': 'r3', 'severity': 'issue'}
    output = {'reports': [report], 'issues': [issue], 'type_inventory': {'shared': [], 'old_only': [], 'new_only': [], 'uncertain': []},
              'report_inventory_version': 1, 'diffs': [diff, other, mislabeled_issue],
              'summary': {'changes': 1, 'issues': 2, 'total': 3, 'by_kind': {'value_changed': 3}},
              'coverage': {'warnings': [], 'compared_sheets': {'old': [], 'new': []}, 'excluded_sheets': {'old': [], 'new': []}}}
    monkeypatch.setattr(app_module, 'compare_pdfs', lambda *args: copy.deepcopy(output))
    with TestClient(create_app(tmp_path)) as client:
        job = setup_job(client)
        response = client.post(f"/api/jobs/{job['id']}/runs", json={'pairs': []})
        assert response.status_code == 201, response.text
        run = response.json()
        assert run['reports'] == [report] and run['issues'] == [issue]
        assert run['status'] == 'completed_with_issues'
        filtered = client.get(f"/api/runs/{run['id']}/diffs", params={'layer': 'content', 'report_id': 'r2', 'limit': 1}).json()
        assert filtered['total'] == 1 and filtered['items'] == [diff]
        assert client.get(f"/api/runs/{run['id']}/diffs", params={'layer': 'content', 'report_id': 'r1'}).json()['total'] == 0
        book = load_workbook(BytesIO(export_report(client.app.state.store, run['id']).getvalue()))
        assert book['报表完整性']['E2'].value == '待确认缺失'
        assert book['报表完整性']['H2'].value == report['basis']
        assert book['可靠共有报表内容差异'].max_row == 2
        assert book['全部差异'].max_row == 4
        assert book['待核对'].max_row == 3
        assert book['提取与配对问题']['B2'].value == issue['message']
        output['reports'][0]['basis'] = 'mutated after run'
        assert client.get(f"/api/runs/{run['id']}").json()['reports'][0]['basis'] == '对侧区域提取失败'


def test_business_inventory_rebuild_preserves_review_ids_and_native_identity():
    from backend.reports import attach_report_inventory, suggest_report_pairs
    from backend.review import apply_reviews, review_catalog
    from test_report_inventory import book

    source = book()
    source['review_namespace'] = 'v1-review:old'
    source['warnings'] = []
    source['pages'][0]['method'] = 'ocr'
    source['sheets'][0]['method'] = 'ocr'
    value = source['sheets'][0]['cells']['B2']
    value.update(raw_text='10', pdf_source={**value['pdf_source'], 'method': 'ocr'})
    source = attach_report_inventory(source)
    initial = apply_reviews(source, [])
    target = review_catalog(initial)[0]
    assert not suggest_report_pairs(initial, initial)
    corrected = apply_reviews(source, [{'revision': 1, 'action': 'cell_update', 'target_id': target['id'],
                                       'value': '12', 'reason': 'verify fixture'}])
    assert review_catalog(corrected)[0]['id'] == target['id']
    assert corrected['reports'][0]['identity_reliable']
    reviewed = apply_reviews(source, [{'revision': 1, 'action': 'cell_update', 'target_id': target['id'],
                                      'value': '12', 'reason': 'verify fixture'},
                                     {'revision': 2, 'action': 'page_verify', 'page': 1, 'reason': 'checked all fields'}])
    assert suggest_report_pairs(reviewed, reviewed)
    assert review_catalog(reviewed)[0]['id'] == target['id']
    assert reviewed['sheets'][0]['cells']['B2']['display'] == '12'
    assert source['sheets'][0]['cells']['B2']['display'] == '10'


def test_mixed_structure_edit_keeps_native_provenance_but_requires_review():
    from backend.reports import attach_report_inventory
    from backend.review import apply_reviews, prepare_structure_event, review_catalog, structure_table
    from test_report_inventory import book

    value = book()
    value['review_namespace'] = 'mixed-structure:old'
    value['warnings'] = []
    value['pages'][0]['method'] = 'ocr'
    value['sheets'][0]['method'] = 'ocr'
    value['sheets'][0]['cells']['B2']['pdf_source']['method'] = 'ocr'
    for cell in value['sheets'][0]['cells'].values():
        cell['raw_text'] = cell['display']
    value = attach_report_inventory(value)
    current = apply_reviews(value, [])
    matrix = structure_table(current, 'p1')['rows']
    proposed = [[{'id': cell['id'], 'text': cell['text']} for cell in row] for row in matrix]
    proposed[1][0]['text'] = 'Y'
    event = prepare_structure_event(current, {'revision': 1, 'side': 'old', 'action': 'table_update', 'page': 1,
                                             'sheet': 'p1', 'structure_confirmed': True,
                                             'rows': proposed, 'reason': 'mixed-grid fix'})
    changed = apply_reviews(value, [event])
    native = changed['sheets'][0]['cells']['A2']
    assert native['pdf_source']['method'] == 'text'
    assert native['requires_review'] and not native['reviewed']
    assert any(c['id'] == native['review_id'] for c in review_catalog(changed))
    confirmed = apply_reviews(value, [event, {'revision': 2, 'action': 'page_verify', 'page': 1, 'reason': 'all fields checked'}])
    assert confirmed['sheets'][0]['cells']['A2']['reviewed']
    assert confirmed['sheets'][0]['cells']['A2']['pdf_source']['method'] == 'text'


def test_failed_text_region_resolves_only_after_covered_correction_and_review():
    from backend.reports import attach_report_inventory, suggest_report_pairs
    from backend.review import apply_reviews, review_catalog
    from test_report_inventory import book

    value = book()
    value.update(review_namespace='region-review:old', warnings=[])
    region = {'id': 'p1-ocr-1', 'reason': 'invalid_text', 'status': 'failed',
              'affects_content': True, 'bbox': [.2, .36, .3, .38]}
    value['pages'][0].update(method='ocr', ocr_regions=[region])
    value['sheets'][0]['method'] = 'ocr'
    cell = value['sheets'][0]['cells']['B2']
    cell.update(raw_text='(cid:0)', display='(cid:0)', value='(cid:0)', requires_review=True)
    cell['pdf_source'].update(region_ids=['p1-ocr-1'], unresolved_text=True)
    value = attach_report_inventory(value)
    current = apply_reviews(value, [])
    target = review_catalog(current)[0]['id']
    verified_bad = apply_reviews(value, [{'revision': 1, 'action': 'page_verify', 'page': 1, 'reason': 'not sufficient'}])
    assert verified_bad['pages'][0]['ocr_regions'][0]['status'] == 'failed'
    assert not suggest_report_pairs(verified_bad, verified_bad)
    fixed = apply_reviews(value, [{'revision': 1, 'action': 'cell_update', 'target_id': target, 'value': '12', 'reason': 'read original'},
                                 {'revision': 2, 'action': 'page_verify', 'page': 1, 'reason': 'all checked'}])
    assert fixed['pages'][0]['ocr_regions'][0]['resolved_by_review']
    assert suggest_report_pairs(fixed, fixed)
    assert value['pages'][0]['ocr_regions'][0]['status'] == 'failed'
    unmapped = copy.deepcopy(value)
    unmapped['pages'][0]['ocr_regions'].append({**region, 'id': 'unmapped'})
    still_pending = apply_reviews(unmapped, [{'revision': 1, 'action': 'cell_update', 'target_id': target, 'value': '12', 'reason': 'read original'},
                                           {'revision': 2, 'action': 'page_verify', 'page': 1, 'reason': 'all editable checked'}])
    assert still_pending['pages'][0]['ocr_regions'][1]['status'] == 'failed'
    assert not suggest_report_pairs(still_pending, still_pending)


def test_full_report_categories_page_pairs_and_export_are_same_snapshot(tmp_path, monkeypatch):
    source = workbook()
    source['pages'][0].update(physical_width=600, physical_height=800,
                              appearance={'width_pt': 600, 'height_pt': 800, 'chars': ['private-detail']})
    monkeypatch.setattr(app_module, 'extract_pdf', lambda *args, **kwargs: copy.deepcopy(source))
    categories = ('content', 'structure', 'format', 'layout')
    location = {'kind': 'pdf', 'page': 1, 'bbox': [0.1, 0.2, 0.8, 0.9], 'method': 'text'}
    report = {'id': 'full-r1', 'title': '说明表单', 'type_code': 'F.1', 'object': '',
              'presence': 'shared', 'confirmation': 'confirmed',
              'old': {'id': 'old-form', 'pages': [1], 'source': location},
              'new': {'id': 'new-form', 'pages': [2], 'source': {**location, 'page': 2}},
              'basis': '唯一报表编号对应；填写值作为差异比较', 'blocking_reasons': [],
              'content_status': 'reliable', 'change_count': 4,
              'dimensions': {category: {'status': 'changed', 'changes': 1, 'issues': 0,
                                        'summary': f'{category} fixture', 'limitations': []} for category in categories},
              'page_pairs': [{'old_page': 1, 'new_page': 2, 'status': 'confirmed', 'basis': '唯一报表页'}]}
    report['paragraph_checks'] = [{
        'attribute': 'line_spacing', 'status': 'changed', 'old_value': '18 pt', 'new_value': '24 pt',
        'message': '同段原生文字的相邻基线距离改变；整块平移不计入。',
        'old_source': location, 'new_source': {**location, 'page': 2},
        'measurements': {'unit': 'pt', 'old': {'baseline_gaps_pt': [18, 18]},
                         'new': {'baseline_gaps_pt': [24, 24]}}}]
    diffs = [{'id': f'diff-{index}', 'kind': f'{category}_changed', 'category': category, 'layer': category,
              'table_id': 'full-r1', 'table_name': '说明表单', 'report_id': 'full-r1', 'row_key': '标题',
              'field': category, 'old_value': 'old', 'new_value': 'new', 'delta': None,
              'severity': 'change', 'message': f'{category} changed', 'old_source': location,
              'new_source': {**location, 'page': 2}} for index, category in enumerate(categories)]
    diffs.append({**diffs[-1], 'id': 'font-unknown', 'category': 'format', 'layer': 'uncertainty',
                  'severity': 'issue', 'message': '扫描字形无法验证具体字体'})
    output = {'report_inventory_version': 2, 'reports': [report], 'issues': [], 'type_inventory': {}, 'non_report_regions': [],
              'diffs': diffs, 'summary': {'changes': 4, 'issues': 1, 'total': 5, 'by_kind': {},
                                        'by_category': {category: 1 for category in categories}},
              'coverage': {'warnings': [], 'compared_sheets': {'old': [], 'new': []}, 'excluded_sheets': {'old': [], 'new': []}}}
    monkeypatch.setattr(app_module, 'compare_pdfs', lambda *args: copy.deepcopy(output))
    with TestClient(create_app(tmp_path)) as client:
        job = setup_job(client)
        assert job['files']['old']['pages'][0]['physical_width'] == 600
        assert 'appearance' not in job['files']['old']['pages'][0]
        response = client.post(f"/api/jobs/{job['id']}/runs", json={'pairs': [
            {'id': 'forms', 'old_report_id': 'old-form', 'new_report_id': 'new-form',
             'pairing_confirmed': True, 'confirmation_reason': '编号相同，允许填写内容不同'}]})
        assert response.status_code == 201, response.text
        run = response.json()
        assert run['reports'][0]['dimensions'] == report['dimensions']
        assert run['reports'][0]['page_pairs'] == report['page_pairs']
        assert run['reports'][0]['paragraph_checks'] == report['paragraph_checks']
        assert 'appearance' not in run['review_pages']['old'][0]
        query = f"/api/runs/{run['id']}/diffs"
        filtered = client.get(query, params={'category': 'format', 'report_id': 'full-r1', 'limit': 1, 'offset': 1}).json()
        assert filtered['total'] == 2 and filtered['items'][0]['id'] == 'font-unknown'
        layout = client.get(query, params={'layer': 'layout'}).json()
        assert layout['total'] == 1 and layout['items'][0]['category'] == 'layout'
        workbook_export = load_workbook(BytesIO(export_report(client.app.state.store, run['id']).getvalue()))
        for category, name in zip(categories, ('内容差异', '结构差异', '格式差异', '版式差异')):
            sheet = workbook_export[name]
            assert sheet.max_row == (3 if category == 'format' else 2)
            assert sheet.cell(2, 1).value == name[:-2]
            assert '第 1 页' in sheet.cell(2, 10).value and '第 2 页' in sheet.cell(2, 11).value
        assert workbook_export['属性验证范围'].max_row == 5
        assert list(workbook_export['原页对应'].values)[1][2:4] == (1, 2)
        evidence = list(workbook_export['段落测量证据'].values)
        assert len(evidence) == 2
        assert evidence[1][2:6] == ('实际段落行距', '存在差异', '18 pt', '24 pt')
        assert '第 1 页' in evidence[1][6] and '第 2 页' in evidence[1][7]
        assert json.loads(evidence[1][9]) == report['paragraph_checks'][0]['measurements']
        report['paragraph_checks'][0]['measurements']['new']['baseline_gaps_pt'][0] = 99
        report['page_pairs'][0]['new_page'] = 99
        report['dimensions']['layout']['summary'] = 'changed after run'
        fixed = client.get(f"/api/runs/{run['id']}").json()
        assert fixed['reports'][0]['page_pairs'][0]['new_page'] == 2
        assert fixed['reports'][0]['dimensions']['layout']['summary'] == 'layout fixture'
        assert fixed['reports'][0]['paragraph_checks'][0]['measurements']['new']['baseline_gaps_pt'] == [24, 24]
