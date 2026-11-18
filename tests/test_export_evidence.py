import json
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from backend.app import Store, create_app, encode, export_report


def stored_run(tmp_path, checks):
    store = Store(tmp_path)
    job = {'id': 'job', 'kind': 'pdf', 'files': {
        side: {'name': side + '.pdf', 'sha256': side} for side in ('old', 'new')}}
    run = {'id': 'run', 'job_id': 'job', 'status': 'completed', 'report_inventory_version': 2,
           'summary': {'changes': 0, 'issues': 0}, 'config': {'pairs': [{'columns': [{'name': '工程量'}]}]},
           'coverage': {'warnings': []}, 'reports': [{'id': 'report', 'type_code': 'E.2', 'title': '单价分析表',
           'presence': 'shared', 'confirmation': 'confirmed', 'paragraph_checks': checks}]}
    with store.connect() as db:
        db.execute('INSERT INTO jobs VALUES (?,?)', ('job', encode(job)))
        db.execute('INSERT INTO runs VALUES (?,?,?)', ('run', 'job', encode(run)))
    return store


def test_paragraph_checks_use_one_json_record_instead_of_recursive_scalar_rows(tmp_path):
    checks = [{'attribute': 'line_spacing', 'status': 'unchanged', 'old_value': '12 pt', 'new_value': '12 pt',
               'message': '原生基线测量', 'measurements': {'old': {'lines': [{'bbox': [1, 2, 3, 4], 'text': str(n)} for n in range(100)]},
                                                         'new': {'gaps': list(range(100))}}} for _ in range(30)]
    store = stored_run(tmp_path, checks)
    book = load_workbook(BytesIO(export_report(store, 'run').getvalue()))
    rules = dict(book['范围与规则'].iter_rows(min_row=2, values_only=True))
    values = {path: text for path, text in rules.items() if '.paragraph_checks[' in path}
    assert len(values) == len(checks)
    for index, check in enumerate(checks):
        assert json.loads(values[f'reports[0].paragraph_checks[{index}]']) == check
    assert rules['config.pairs[0].columns[0].name'] == '"工程量"'
    assert book['范围与规则'].max_row < 100
    assert book['段落测量证据'].max_row == len(checks) + 1
    assert store.run('run')['reports'][0]['paragraph_checks'] == checks


def test_long_measurements_and_complete_checks_roundtrip_without_excel_cell_truncation(tmp_path):
    check = {'attribute': 'alignment', 'status': 'unchanged', 'message': '保留完整来源',
             'old_source': {'kind': 'pdf', 'page': 7, 'bbox': [0, 0, 1, 1]},
             'measurements': {'old': {'text': '测量证据' * 18000}, 'new': {'baselines': list(range(2000))}}}
    store = stored_run(tmp_path, [check])
    book = load_workbook(BytesIO(export_report(store, 'run').getvalue()))
    rules = [row for row in book['范围与规则'].iter_rows(min_row=2, values_only=True)
             if row[0].startswith('reports[0].paragraph_checks[0]')]
    assert len(rules) > 1
    assert all('按顺序拼接' in path and len(part) <= 30000 for path, part in rules)
    assert json.loads(''.join(part for path, part in rules)) == check
    evidence = list(book['段落测量证据'].iter_rows(min_row=2, values_only=True))
    assert len(evidence) > 1
    assert all('按行顺序拼接末列' in row[8] and len(row[9]) <= 30000 for row in evidence)
    assert json.loads(''.join(row[9] for row in evidence)) == check['measurements']


def test_rule_sheet_continuation_preserves_every_path(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.app.EXCEL_EXPORT_MAX_ROWS', 6)
    checks = [{'attribute': 'alignment', 'status': 'unchanged', 'measurements': {'index': index}} for index in range(8)]
    book = load_workbook(BytesIO(export_report(stored_run(tmp_path, checks), 'run').getvalue()))
    sheets = [sheet for sheet in book if sheet.title.startswith('范围与规则')]
    assert len(sheets) > 1
    assert all(sheet.max_row <= 6 for sheet in sheets)
    values = dict(row for sheet in sheets for row in sheet.iter_rows(min_row=2, values_only=True))
    assert values['config.pairs[0].columns[0].name'] == '"工程量"'
    assert [json.loads(values[f'reports[0].paragraph_checks[{index}]']) for index in range(8)] == checks


def test_export_endpoint_sanitizes_control_characters_and_keeps_formula_text_safe(tmp_path):
    with TestClient(create_app(tmp_path / 'data')) as client:
        store = client.app.state.store
        job = {
            'id': 'job-ctrl',
            'kind': 'pdf',
            'files': {
                'old': {'name': '旧版\x0b报表.pdf', 'sha256': 'sha-old'},
                'new': {'name': '新版\x0c报表.pdf', 'sha256': 'sha-new'},
            },
        }
        report = {
            'id': 'r1',
            'type_code': 'E.2',
            'title': '分部分项\x0c清单\x0b表',
            'object': {'name': '对象\x0b1'},
            'presence': 'shared',
            'confirmation': 'confirmed',
            'old': {'pages': [1]},
            'new': {'pages': [1]},
            'basis': '匹配\x0c依据\x0b',
            'blocking_reasons': ['阻塞\x0b原因'],
            'content_status': 'reliable',
            'change_count': 2,
            'dimensions': {
                'content': {'status': 'changed', 'changes': 1, 'issues': 0, 'summary': '内容\x0c摘要', 'limitations': ['限制\x0b1']},
                'structure': {'status': 'unchanged', 'changes': 0, 'issues': 0, 'summary': '结构摘要', 'limitations': []},
                'format': {'status': 'unchanged', 'changes': 0, 'issues': 0, 'summary': '格式摘要', 'limitations': []},
                'layout': {'status': 'unchanged', 'changes': 0, 'issues': 0, 'summary': '版式摘要', 'limitations': []},
            },
            'business': {'status': 'changed', 'changes': 1, 'compared_rows': 10, 'compared_fields': 4, 'issues': ['业务\x0b待核']},
            'page_pairs': [{'old_page': 1, 'new_page': 1, 'status': 'confirmed', 'basis': '页对\x0c依据'}],
            'paragraph_checks': [{
                'attribute': 'line_spacing',
                'status': 'changed',
                'old_value': '12\x0cpt',
                'new_value': '14\x0bpt',
                'message': '段落\x0c说明\x0b',
                'old_source': {'kind': 'pdf', 'page': 1, 'sheet': '原页\x0b', 'bbox': [0, 0, 1, 1]},
                'new_source': {'kind': 'pdf', 'page': 1, 'sheet': '新页\x0c', 'bbox': [0, 0, 1, 1]},
                'measurements': {'raw': '证据\x0c\x0b'},
            }],
        }
        run = {
            'id': 'run-ctrl',
            'job_id': 'job-ctrl',
            'status': 'completed_with_issues',
            'review_revision': 2,
            'report_inventory_version': 2,
            'summary': {'changes': 1, 'issues': 1},
            'config': {'pairs': [{'columns': [{'name': '字段\x0b'}]}], 'note\x0c': '配置\x0b说明'},
            'coverage': {'warnings': ['告警\x0c内容\x0b']},
            'reports': [report],
            'type_inventory': {'shared': [{'type_code': 'E.2', 'title': '清单\x0b表', 'old_pages': [1], 'new_pages': [1]}]},
            'issues': [{'type': 'ocr', 'message': '问题\x0c描述\x0b', 'report_ids': ['r1'], 'side': 'new', 'pages': [1],
                        'sources': [{'kind': 'pdf', 'page': 1, 'sheet': '页\x0b1'}], 'actions': ['review']}],
            'non_report_regions': [{'side': 'old', 'type': 'document_section', 'pages': [2], 'basis': '正文\x0c区域',
                                    'source': {'kind': 'pdf', 'page': 2, 'sheet': '正文\x0b'}}],
        }
        diffs = [
            {
                'kind': 'value_changed', 'category': 'content', 'layer': 'content', 'severity': 'change',
                'table_name': '表\x0b1', 'row_key': 'K\x0c1', 'field': '字段\x0b',
                'old_value': '=1+1\x0c旧\x0b值', 'new_value': '新\x0b值\n第二行', 'delta': 12.5,
                'old_source': {'kind': 'xlsx', 'sheet': '明细\x0b', 'cell': 'B2'},
                'new_source': {'kind': 'xlsx', 'sheet': '明细\x0c', 'cell': 'B2'},
                'message': '差异\x0c说明\x0b',
            },
            {
                'kind': 'ocr_review', 'category': 'content', 'layer': 'uncertainty', 'severity': 'issue',
                'table_name': '表\x001', 'row_key': 'K2', 'field': '备注',
                'old_value': '原\x00值', 'new_value': '新值', 'delta': None,
                'old_source': None, 'new_source': None,
                'message': '待核\x0c说明',
            },
        ]
        review_events = [
            {
                'revision': 1, 'side': 'new', 'action': 'cell_update', 'page': 1, 'sheet': '表\x0b1', 'cell': 'B2',
                'original_text': '原\x0c文', 'before_text': '前\x0b文', 'after_text': '后\x0c文',
                'reason': '修订\x0b原因', 'created_at': '2026-11-18T00:00:00Z',
            },
            {
                'revision': 2, 'side': 'new', 'action': 'table_update', 'page': 1, 'sheet': '表\x0b1',
                'table_before': {'sheet': '表\x0b1', 'rows': [[{'id': 'c1', 'text': '旧\x0c格', 'original_text': '原\x0b格', 'source': {'kind': 'pdf', 'page': 1}}]]},
                'table_after': {'sheet': '表\x0b1', 'rows': [[{'id': 'c1', 'text': '新\x0b格', 'original_text': '原\x0b格', 'source': {'kind': 'pdf', 'page': 1}}]]},
                'reason': '结构\x0c调整', 'created_at': '2026-11-18T00:01:00Z',
            },
        ]
        with store.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?)', ('job-ctrl', encode(job)))
            db.execute('INSERT INTO runs VALUES (?,?,?)', ('run-ctrl', 'job-ctrl', encode(run)))
            db.executemany('INSERT INTO diffs VALUES (?,?,?,?,?)', [
                ('run-ctrl', index, diff['kind'], '', encode(diff)) for index, diff in enumerate(diffs)
            ])
            db.executemany('INSERT INTO review_events VALUES (?,?,?)', [
                ('job-ctrl', event['revision'], encode(event)) for event in review_events
            ])

        response = client.get('/api/runs/run-ctrl/export')
        assert response.status_code == 200
        book = load_workbook(BytesIO(response.content))

        # 验证全工作簿不存在公式单元格与非法控制字符
        all_cells = [cell for sheet in book for row in sheet for cell in row]
        assert all(cell.data_type != 'f' for cell in all_cells)
        str_values = [cell.value for cell in all_cells if isinstance(cell.value, str)]
        assert all('\x0c' not in val and '\x0b' not in val and '\x00' not in val for val in str_values)

        formula_cells = [cell for row in book['全部差异'] for cell in row if cell.value == '=1+1旧值']
        assert formula_cells and all(cell.data_type == 's' for cell in formula_cells)
        assert any(cell.value == '新值\n第二行' for row in book['全部差异'] for cell in row)
        assert dict(book['摘要'].iter_rows(min_row=2, values_only=True))['变化事件'] == 1
        assert book['原页对应'].cell(2, 3).value == 1 and book['原页对应'].cell(2, 4).value == 1


def test_export_module_max_rows_monkeypatch_propagates_to_app_and_http_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.export.EXCEL_EXPORT_MAX_ROWS', 4)
    checks = [{'attribute': 'alignment', 'status': 'unchanged', 'measurements': {'index': index}} for index in range(6)]
    with TestClient(create_app(tmp_path / 'data')) as client:
        store = client.app.state.store
        job = {'id': 'job-split', 'kind': 'pdf', 'files': {
            side: {'name': side + '.pdf', 'sha256': side} for side in ('old', 'new')}}
        run = {'id': 'run-split', 'job_id': 'job-split', 'status': 'completed', 'report_inventory_version': 2,
               'summary': {'changes': 0, 'issues': 0}, 'config': {'pairs': [{'columns': [{'name': '工程量'}]}]},
               'coverage': {'warnings': []}, 'reports': [{'id': 'report', 'type_code': 'E.2', 'title': '单价分析表',
               'presence': 'shared', 'confirmation': 'confirmed', 'paragraph_checks': checks}]}
        with store.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?)', ('job-split', encode(job)))
            db.execute('INSERT INTO runs VALUES (?,?,?)', ('run-split', 'job-split', encode(run)))

        # 验证经由 backend.app.export_report 与 HTTP 端点均能触发分片且完整保留路径
        for raw_bytes in (export_report(store, 'run-split').getvalue(), client.get('/api/runs/run-split/export').content):
            book = load_workbook(BytesIO(raw_bytes))
            sheets = [sheet for sheet in book if sheet.title.startswith('范围与规则')]
            assert len(sheets) > 1
            assert all(2 <= sheet.max_row <= 4 for sheet in sheets)
            values = dict(row for sheet in sheets for row in sheet.iter_rows(min_row=2, values_only=True))
            assert [json.loads(values[f'reports[0].paragraph_checks[{index}]']) for index in range(6)] == checks


def test_export_report_clamps_max_rows_below_two_without_empty_first_sheet(tmp_path):
    from backend.export import export_report as export_with_limit

    checks = [{'attribute': 'alignment', 'status': 'unchanged', 'measurements': {'index': index}} for index in range(3)]
    store = stored_run(tmp_path, checks)
    book = load_workbook(BytesIO(export_with_limit(store, 'run', max_rows=1).getvalue()))
    sheets = [sheet for sheet in book if sheet.title.startswith('范围与规则')]
    assert len(sheets) == 10
    assert all(sheet.max_row == 2 for sheet in sheets)
    values = dict(row for sheet in sheets for row in sheet.iter_rows(min_row=2, values_only=True))
    assert [json.loads(values[f'reports[0].paragraph_checks[{index}]']) for index in range(3)] == checks
