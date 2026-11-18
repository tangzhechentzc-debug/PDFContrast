import copy

import pytest
from openpyxl.utils import get_column_letter

from backend.cost_reports import compare_cost_reports


HEADERS = ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价']
ROW = ['1', '001000001', '混凝土', 'C30', 'm3', '10', '20', '200']


def make_book(rows, headers=None, page=1, name='table'):
    cells = {}
    for row, values in enumerate([headers or HEADERS, *rows], 1):
        for col, value in enumerate(values, 1):
            address = f'{get_column_letter(col)}{row}'
            cells[address] = {'row': row, 'column': col, 'address': address, 'value': value,
                              'display': value, 'raw_text': value,
                              'pdf_source': {'kind': 'pdf', 'page': page, 'bbox': [.1, row / 20, .9, (row + 1) / 20], 'method': 'text'}}
    return {'sheets': [{'name': name, 'structure': 'table', 'max_column': len(headers or HEADERS), 'cells': cells}]}


def inventory(code='E.1', names=None):
    report = {'id': 'r1', 'type_code': code, 'title': code + '造价表', 'member_sheets': names or ['table'],
              'source': {'kind': 'pdf', 'page': 1, 'bbox': [0, 0, 1, 1]}}
    return {'reports': [{'id': 'comparison-1', 'title': report['title'], 'old': copy.deepcopy(report),
                         'new': copy.deepcopy(report), 'confirmation': 'confirmed', 'blocking_reasons': [], 'match_method': 'object'}]}


def test_values_decimal_equivalence_and_evidence_are_immutable():
    old = make_book([ROW])
    new = make_book([ROW[:5] + ['10.00', '21', '210']])
    before = copy.deepcopy((old, new))
    result = compare_cost_reports(old, new, inventory())
    assert result['reports'][0]['status'] == 'changed'
    assert {d['field'] for d in result['diffs']} == {'综合单价', '金额'}
    assert next(d for d in result['diffs'] if d['field'] == '金额')['delta'] == '10'
    assert all(d['old_source']['page'] == 1 and d['category'] == 'business' for d in result['diffs'])
    assert (old, new) == before


def test_reorder_and_continuation_do_not_create_rows_or_duplicate_logical_sheet():
    second = ['2', '001000002', '钢筋', 'HRB400', 't', '2', '100', '200']
    old = make_book([ROW])
    old['sheets'] += make_book([second], page=2, name='page2')['sheets']
    old['sheets'] += make_book([ROW, second], name='logical')['sheets']
    new = make_book([second, ROW])
    config = inventory(names=['table', 'page2'])
    config['reports'][0]['new']['member_sheets'] = ['table']
    result = compare_cost_reports(old, new, config)
    assert result['reports'][0]['status'] == 'unchanged'
    assert result['reports'][0]['compared_rows'] == 2
    assert result['diffs'] == []


def test_code_leading_zeros_are_not_numeric():
    changed = ROW.copy()
    changed[1] = '1000001'
    result = compare_cost_reports(make_book([ROW]), make_book([changed]), inventory())
    assert {d['kind'] for d in result['diffs']} == {'business_row_added', 'business_row_removed'}


@pytest.mark.parametrize('defect', ['duplicate', 'empty_key', 'unknown_header', 'invalid_number', 'ocr', 'missing_field', 'no_rows'])
def test_extraction_uncertainty_never_reports_unchanged(defect):
    old = make_book([ROW])
    new = copy.deepcopy(old)
    if defect == 'duplicate':
        new = make_book([ROW, ROW])
    elif defect == 'empty_key':
        new['sheets'][0]['cells']['B2']['raw_text'] = ''
    elif defect == 'unknown_header':
        new = make_book([ROW + ['x']], headers=HEADERS + ['未知列'])
    elif defect == 'invalid_number':
        new['sheets'][0]['cells']['F2']['raw_text'] = '未定'
    elif defect == 'ocr':
        new['sheets'][0]['cells']['F2']['pdf_source']['method'] = 'ocr'
    elif defect == 'missing_field':
        new = make_book([ROW[:-1]], headers=HEADERS[:-1])
    else:
        new = make_book([])
    result = compare_cost_reports(old, new, inventory())
    assert result['reports'][0]['status'] == 'pending'
    assert result['diffs'][0]['severity'] == 'issue'
    assert not any(d['severity'] == 'change' for d in result['diffs'])


@pytest.mark.parametrize('code, header', [('D.1', '单项工程名称'), ('D.2', '单位工程名称'), ('D.3', '汇总内容')])
def test_summary_reports_keep_totals_and_compare_amounts(code, header):
    old = make_book([['1', '工程A', '1,000.00'], ['', '合计', '1000']], headers=['序号', header, '金额（元）'])
    new = make_book([['1', '工程A', '1000'], ['', '合计', '1200']], headers=['序号', header, '金额(元)'])
    result = compare_cost_reports(old, new, inventory(code))
    assert len(result['diffs']) == 1
    assert result['diffs'][0]['row_key'] == '合计'
    assert result['diffs'][0]['delta'] == '200'


@pytest.mark.parametrize('method', ['sequence', 'remaining'])
def test_sequence_identity_requires_manual_confirmation(method):
    config = inventory()
    config['reports'][0]['match_method'] = method
    result = compare_cost_reports(make_book([ROW]), make_book([ROW]), config)
    assert result['reports'][0]['status'] == 'pending'
    config['reports'][0]['match_method'] = 'manual'
    assert compare_cost_reports(make_book([ROW]), make_book([ROW]), config)['reports'][0]['status'] == 'unchanged'


def test_limit_and_bid_d2_are_not_conflated():
    config = inventory('D.2')
    config['reports'][0]['old']['title'] = 'D.2 单项工程最高投标限价汇总表'
    config['reports'][0]['new']['title'] = 'D.2 单项工程投标报价汇总表'
    result = compare_cost_reports(make_book([ROW]), make_book([ROW]), config)
    assert result['reports'][0]['status'] == 'pending'


def test_blank_amount_is_not_zero():
    old = make_book([['1', '工程A', '']], headers=['序号', '工程名称', '金额'])
    new = make_book([['1', '工程A', '0']], headers=['序号', '工程名称', '金额'])
    result = compare_cost_reports(old, new, inventory('D.1'))
    assert result['diffs'][0]['old_value'] == ''
    assert result['diffs'][0]['new_value'] == '0'
    assert result['diffs'][0]['delta'] is None


def test_multiline_header_maps_leaf_price_columns():
    first = ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额（元）', '']
    old = make_book([['', '', '', '', '', '', '综合单价', '合价'], ROW], headers=first)
    new = copy.deepcopy(old)
    new['sheets'][0]['cells']['G3']['raw_text'] = '25'
    result = compare_cost_reports(old, new, inventory())
    assert result['reports'][0]['status'] == 'changed'
    assert result['diffs'][0]['field'] == '综合单价'


@pytest.mark.parametrize('defect', ['ocr_header', 'failed_page', 'structure_unconfirmed', 'duplicate_identity'])
def test_unreliable_evidence_blocks_semantic_comparison(defect):
    old = make_book([ROW])
    new = copy.deepcopy(old)
    config = inventory()
    if defect == 'ocr_header':
        new['sheets'][0]['cells']['F1']['pdf_source']['method'] = 'ocr'
    elif defect == 'failed_page':
        config['reports'][0]['new']['pages'] = [1]
        new['pages'] = [{'number': 1, 'status': 'unavailable'}]
    elif defect == 'structure_unconfirmed':
        new['sheets'][0]['structure_unconfirmed'] = True
    else:
        config['reports'][0]['new']['duplicate'] = True
    result = compare_cost_reports(old, new, config)
    assert result['reports'][0]['status'] == 'pending'
    assert result['reports'][0]['issue_count'] > 0
    assert result['diffs'][0]['kind'] == 'business_issue'
    assert result['diffs'][0]['old_source']['location_precision'] == 'corresponding_region'
