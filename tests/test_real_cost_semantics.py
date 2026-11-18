"""Regression shapes transcribed from PDFAnalysis Data/2.pdf, physical pages 3–7."""
import copy

import pytest

from backend.cost_reports import _extract, _table_header, compare_cost_reports
from backend.reports import _cost_engineering_context, _family, _identity
from test_cost_reports import inventory, make_book


def test_real_summary_two_row_heading_and_merged_total_are_all_compared():
    headers = ['序号', '单项工程名称', '金额(元)', '其中：（元）', '']
    rows = [['', '', '', '暂估价', '不可竞争费'],
            ['1', '固镇县建设项目-湖沟镇姚集中心村', '1589822.60', '', '80148.29'],
            ['合计', '', '9561784.47', '', '451964.29']]
    book = make_book(rows, headers=headers)
    report = inventory('D.1')['reports'][0]['old']
    records, fields, issues = _extract(book, report, 'D.1')
    assert not issues
    assert len(records) == 2
    assert records[-1]['key'] == '合计'
    assert records[-1]['sources']['name']['cell'] == 'A4'
    assert records[-1]['values']['amount'] == '9561784.47'
    assert 'noncompetitive' in fields


def test_real_e1_spanning_heading_phantom_columns_and_priced_sections():
    headers = ['序\n号', '项目编码', '项目名称', '项目特征描述', '计量\n单位', '工程量', '', '', '金额', '', '（', '元', '）', '', '', '', '']
    rows = [
        ['', '', '', '', '', '', '综合单\n价', '合价', '', '', '', '', '', '', '其中', '', ''],
        ['', '', '', '', '', '', '', '', '', '定额\n人工费', '', '', '', '定额\n机械费', '定额', '', '暂估价'],
        ['', '001', '垃圾桶及垃圾收集亭子', '', '', '', '', '11914.29', '', '', '', '', '', '', '', '', ''],
        ['', '0104', '砌筑工程', '', '', '', '', '50000.00', '', '', '', '', '', '', '', '', ''],
        ['1', '011101003001', '细石混凝土楼\n地面', '100厚c20混凝土地面', 'm2', '16.830', '47.25', '795.22', '', '216.43', '', '', '', '0.50', '', '', ''],
        ['2', 'WB\n011501001001', '垃圾桶', '120L成品采购安装', '个', '22.000', '125.15', '2753.30', '', '', '', '', '', '', '', '', ''],
    ]
    book = make_book(rows, headers=headers, page=6)
    report = inventory()['reports'][0]['old']
    records, fields, issues = _extract(book, report, 'E.1')
    assert not issues
    assert [row['key'] for row in records] == ['011101003001', 'WB011501001001']
    assert {'labor', 'machine', 'provisional'} <= fields
    changed = copy.deepcopy(book)
    changed['sheets'][0]['cells']['F6']['raw_text'] = '17.830'
    result = compare_cost_reports(book, changed, inventory())
    assert result['reports'][0]['compared_rows'] == 2
    assert len(result['diffs']) == 1
    diff = result['diffs'][0]
    assert diff['field'] == '工程量' and diff['delta'] == '1.000'
    assert diff['old_source']['page'] == diff['new_source']['page'] == 6
    assert diff['old_source']['cell'] == 'F6'


def test_real_d3_repeated_subitem_name_keeps_hierarchical_sequence():
    headers = ['序号', '汇总内容', '金额(元)', '其中：材料、设备暂估价（元）']
    old = make_book([['1', '分部分项工程费', '100', ''], ['1.1', '混凝土', '40', ''],
                     ['1.2', '混凝土', '60', ''], ['工程造价=1+2+3+4+5', '', '100', '']], headers=headers)
    new = copy.deepcopy(old)
    new['sheets'][0]['cells']['C4']['raw_text'] = '65'
    result = compare_cost_reports(old, new, inventory('D.3'))
    assert result['reports'][0]['status'] == 'changed'
    assert result['diffs'][0]['row_key'] == '1.2 · 混凝土'
    assert result['diffs'][0]['delta'] == '5'


def test_real_wrapped_engineering_name_ignores_page_marker_and_section_label():
    def line(text, x, y):
        return {'text': text, 'bbox': [x, y, .95, y + .012], 'method': 'text'}
    context = [line('工程名称：固镇县2024年度湖沟镇、杨庙镇、任桥镇', .073, .075),
               line('第1页 共1页', .856, .082),
               line('和美乡村省级中心村建设项目-湖沟镇姚集中心村', .073, .089)]
    title = line('D.2 单项工程投标报价汇总表', .3, .04)
    title.update(type_code='D.2')
    fields = _identity(title, context)[-1]
    assert fields['工程名称'].endswith('和美乡村省级中心村建设项目-湖沟镇姚集中心村')
    title['type_code'] = 'D.3'
    assert _identity(title, [line('工程名称：园建工程-1 标段： 第1页 共1页', .073, .075)])[-1] == {'工程名称': '园建工程-1'}


def test_parent_context_distinguishes_same_named_units():
    reports, sheets = [], []
    for page, code, name, rows in [
        (1, 'D.2', '单项A', [['1', '土建', '100']]),
        (2, 'D.3', '土建', [['1', '分部分项工程费', '100']]),
        (3, 'D.2', '单项B', [['1', '土建', '200']]),
        (4, 'D.3', '土建', [['1', '分部分项工程费', '200']]),
    ]:
        sheet_name = f'page{page}'
        sheets += make_book(rows, headers=['序号', '工程名称', '金额'], page=page, name=sheet_name)['sheets']
        reports.append({'type_code': code, 'object_fields': {'工程名称': name}, 'member_sheets': [sheet_name], 'pages': [page]})
    book = {'sheets': sheets, 'reports': reports}
    _cost_engineering_context(book)
    assert reports[1]['cost_identity']['single_project'] == '单项A'
    assert reports[3]['cost_identity']['single_project'] == '单项B'
    assert reports[1]['cost_identity_sources'][0]['page'] == 1
    assert reports[3]['cost_identity_sources'][0]['page'] == 3


def test_limit_and_bid_summary_families_remain_distinct():
    assert _family({'type_code': 'D.2', 'title': '单项工程最高投标限价汇总表'}) != _family(
        {'type_code': 'D.2', 'title': '单项工程投标报价汇总表'})


def test_empty_duplicate_amount_column_is_not_assumed_to_be_a_phantom():
    old = make_book([['1', '工程A', '100', '']], headers=['序号', '工程名称', '金额', '金额'])
    result = compare_cost_reports(old, old, inventory('D.1'))
    assert result['reports'][0]['status'] == 'pending'
    assert any('多个表头' in issue for issue in result['reports'][0]['issues'])


@pytest.mark.parametrize(
    'code, headers, old_rows, new_rows, expected_old, expected_new',
    [
        (
            'D.3',
            ['序号', '汇总内容', '金额(元)', '备注'],
            [['1', '分部分项工程费', '100.00', '暂估价'], ['2', '措施项目费', '200.00', '']],
            [['1', '分部分项工程费', '120.00', '暂估价'], ['2', '措施项目费', '200.00', '']],
            '100.00',
            '120.00',
        ),
        (
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价', '说明'],
            [['1', '010101001001', '挖土方', '三类土', 'm3', '10', '20', '200.00', '人工费'],
             ['2', '010101001002', '填土方', '夯填', 'm3', '5', '10', '50.00', '']],
            [['1', '010101001001', '挖土方', '三类土', 'm3', '10', '25', '250.00', '人工费'],
             ['2', '010101001002', '填土方', '夯填', 'm3', '5', '10', '50.00', '']],
            '200.00',
            '250.00',
        ),
        (
            'D.1',
            ['序号', '单项工程名称', '金额(元)', '说明'],
            [['', '一号楼', '0.00', '税金'], ['2', '二号楼', '300.00', '']],
            [['', '一号楼', '50.00', '税金'], ['2', '二号楼', '300.00', '']],
            '0.00',
            '50.00',
        ),
    ],
)
def test_first_data_row_with_alias_in_unmapped_column_is_not_swallowed(
    code, headers, old_rows, new_rows, expected_old, expected_new
):
    # 首行数据在未映射列含“暂估价”等表头别名词时，不得吞掉首行并漏报金额变动
    old = make_book(old_rows, headers=headers)
    new = make_book(new_rows, headers=headers)
    inv = inventory(code)

    sheet = old['sheets'][0]
    rows_by_num = {}
    for cell in sheet['cells'].values():
        rows_by_num.setdefault(cell['row'], {})[cell['column']] = cell
    _, header_end = _table_header(rows_by_num, sheet['max_column'], 'code' if code == 'E.1' else 'name')
    assert header_end == 1

    old_records, _, old_issues = _extract(old, inv['reports'][0]['old'], code)
    new_records, _, new_issues = _extract(new, inv['reports'][0]['new'], code)
    assert len(old_records) == 2 and len(new_records) == 2
    assert old_records[0]['values']['amount'] == expected_old
    assert new_records[0]['values']['amount'] == expected_new
    assert any('存在未映射表头' in issue for issue in old_issues)
    assert any('存在未映射表头' in issue for issue in new_issues)

    # 存在未映射列时按业务红线报告 pending/issue，绝不因吞行而假报 unchanged
    result = compare_cost_reports(old, new, inv)
    assert result['reports'][0]['status'] == 'pending'
    assert result['diffs'][0]['kind'] == 'business_issue'


def test_multiline_header_with_column_numbers_and_units_does_not_break_early():
    # 括号列号行 (1)/(2) 与单位行“元”属于多行表头，不应提前停止扩张表头
    headers = ['序号', '汇总内容', '金额', '其中']
    old = make_book([['(1)', '(2)', '元', '暂估价'], ['1', '分部分项工程费', '100.00', '10.00']], headers=headers)
    new = make_book([['(1)', '(2)', '元', '暂估价'], ['1', '分部分项工程费', '120.00', '10.00']], headers=headers)
    result = compare_cost_reports(old, new, inventory('D.3'))
    assert result['reports'][0]['status'] == 'changed'
    assert len(result['diffs']) == 1
    assert result['diffs'][0]['row_key'] == '分部分项工程费'
    assert result['diffs'][0]['delta'] == '20.00'


@pytest.mark.parametrize(
    'case_id, code, headers, old_rows, new_rows, expected_mapped_cols, expected_records, require_invalid_num',
    [
        (
            'chinese_seq_invalid_amount_alias',
            'D.3',
            ['序号', '汇总内容', '金额(元)', '备注'],
            [['一', '分部分项工程费', '待定', '暂估价'], ['二', '措施费', '200.00', '']],
            [['一', '分部分项工程费', '待定', '暂估价'], ['二', '措施费', '200.00', '']],
            3,
            2,
            True,
        ),
        (
            'paren_chinese_seq_blank_amount_alias',
            'D.3',
            ['序号', '汇总内容', '金额(元)', '备注'],
            [['（一）', '分部分项工程费', '', '暂估价'], ['（二）', '措施费', '200.00', '']],
            [['（一）', '分部分项工程费', '', '暂估价'], ['（二）', '措施费', '200.00', '']],
            3,
            2,
            False,
        ),
        (
            'e1_section_or_unpriced_row1_with_alias',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价', '备注'],
            [['一', '010101001001', '挖土方', '三类土', 'm3', '', '', '', '人工费'],
             ['2', '010101001002', '填土方', '夯填', 'm3', '5', '10', '50.00', '']],
            [['一', '010101001001', '挖土方', '三类土', 'm3', '', '', '', '人工费'],
             ['2', '010101001002', '填土方', '夯填', 'm3', '5', '10', '50.00', '']],
            8,
            2,
            False,
        ),
        (
            'empty_seq_blank_amount_alias_row1_diff',
            'D.3',
            ['序号', '汇总内容', '金额(元)', '备注'],
            [['', '分部分项工程费', '', '暂估价'], ['2', '措施费', '200.00', '']],
            [['', '分部分项工程费', '50.00', '暂估价'], ['2', '措施费', '200.00', '']],
            3,
            2,
            False,
        ),
        (
            'empty_seq_blank_amount_alias_row1_same',
            'D.3',
            ['序号', '汇总内容', '金额(元)', '备注'],
            [['', '分部分项工程费', '', '暂估价'], ['2', '措施费', '200.00', '']],
            [['', '分部分项工程费', '', '暂估价'], ['2', '措施费', '200.00', '']],
            3,
            2,
            False,
        ),
    ],
)
def test_non_arabic_or_blank_first_row_with_unmapped_alias_is_not_swallowed(
    case_id, code, headers, old_rows, new_rows, expected_mapped_cols, expected_records, require_invalid_num
):
    # 非阿拉伯序号或空序号首行在数值为空/无效且未映射列含别名词时，严禁吞行并假报 unchanged
    old = make_book(old_rows, headers=headers)
    new = make_book(new_rows, headers=headers)
    inv = inventory(code)

    for book in (old, new):
        sheet = book['sheets'][0]
        rows_by_num = {}
        for cell in sheet['cells'].values():
            rows_by_num.setdefault(cell['row'], {})[cell['column']] = cell
        mapping, header_end = _table_header(rows_by_num, sheet['max_column'], 'code' if code == 'E.1' else 'name')
        assert header_end == 1, case_id
        assert len(mapping) == expected_mapped_cols, case_id

    old_records, _, old_issues = _extract(old, inv['reports'][0]['old'], code)
    assert len(old_records) == expected_records, case_id
    assert any('存在未映射表头' in issue for issue in old_issues), case_id
    if require_invalid_num:
        assert any('不是可确认的数值' in issue for issue in old_issues), case_id

    result = compare_cost_reports(old, new, inv)
    assert result['reports'][0]['status'] == 'pending', case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


@pytest.mark.parametrize(
    'case_id, headers, old_rows, new_rows, expected_header_end, expected_mapped_cols, expected_status, expected_delta',
    [
        (
            '2row_colnums_only',
            ['序号', '汇总内容', '金额'],
            [['(1)', '（2）', '(3)'], ['1', '分部分项工程费', '100.00']],
            [['(1)', '（2）', '(3)'], ['1', '分部分项工程费', '120.00']],
            2,
            3,
            'changed',
            '20.00',
        ),
        (
            '2row_units_only',
            ['序号', '汇总内容', '金额'],
            [['', '', '万元'], ['1', '分部分项工程费', '100.00']],
            [['', '', '万元'], ['1', '分部分项工程费', '120.00']],
            2,
            3,
            'changed',
            '20.00',
        ),
        (
            '2row_placeholders_only',
            ['序号', '汇总内容', '金额'],
            [['-', '—', '/'], ['1', '分部分项工程费', '100.00']],
            [['-', '—', '/'], ['1', '分部分项工程费', '120.00']],
            2,
            3,
            'changed',
            '20.00',
        ),
        (
            'row2_sub_row3_paren_colnums',
            ['序号', '汇总内容', '金额(元)', '其中'],
            [['', '', '', '暂估价'], ['(1)', '（2）', '(3)', '(4)'], ['1', '分部分项工程费', '100.00', '10.00']],
            [['', '', '', '暂估价'], ['(1)', '（2）', '(3)', '(4)'], ['1', '分部分项工程费', '120.00', '10.00']],
            3,
            4,
            'changed',
            '20.00',
        ),
        (
            'row2_sub_row3_units',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '合价', '暂估价'], ['', '', '元', '元'], ['1', '分部分项工程费', '100.00', '10.00']],
            [['', '', '合价', '暂估价'], ['', '', '元', '元'], ['1', '分部分项工程费', '120.00', '10.00']],
            3,
            4,
            'changed',
            '20.00',
        ),
        (
            'row2_sub_row3_placeholders',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '合价', '暂估价'], ['-', '—', '/', '-'], ['1', '分部分项工程费', '100.00', '10.00']],
            [['', '', '合价', '暂估价'], ['-', '—', '/', '-'], ['1', '分部分项工程费', '120.00', '10.00']],
            3,
            4,
            'changed',
            '20.00',
        ),
        (
            'row2_colnum_unit_row3_sub',
            ['序号', '汇总内容', '金额', '其中'],
            [['(1)', '（2）', '元', ''], ['-', '—', '/', '暂估价'], ['1', '分部分项工程费', '100.00', '10.00']],
            [['(1)', '（2）', '元', ''], ['-', '—', '/', '暂估价'], ['1', '分部分项工程费', '120.00', '10.00']],
            3,
            4,
            'changed',
            '20.00',
        ),
        (
            'row2_units_m3_wan_pct',
            ['序号', '汇总内容', '工程量', '费率', '金额', '其中'],
            [['(1)', '（2）', 'm3', '%', '万元', ''], ['', '', '-', '—', '/', '暂估价'], ['1.1', '土方工程', '100', '5', '100.00', '10.00']],
            [['(1)', '（2）', 'm3', '%', '万元', ''], ['', '', '-', '—', '/', '暂估价'], ['1.1', '土方工程', '100', '5', '210.00', '10.00']],
            3,
            6,
            'changed',
            '110.00',
        ),
        (
            'row2_blank_row_row3_sub',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', ''], ['', '', '', '暂估价'], ['1', '分部分项工程费', '100.00', '10.00']],
            [['', '', '', ''], ['', '', '', '暂估价'], ['1', '分部分项工程费', '120.00', '10.00']],
            3,
            4,
            'changed',
            '20.00',
        ),
        (
            'two_blank_rows_before_sub',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', ''], ['', '', '', ''], ['', '', '', '暂估价'], ['1', '分部分项工程费', '100.00', '10.00']],
            [['', '', '', ''], ['', '', '', ''], ['', '', '', '暂估价'], ['1', '分部分项工程费', '120.00', '10.00']],
            1,
            3,
            'pending',
            None,
        ),
    ],
)
def test_multiline_header_decorative_trailing_and_interleaved_rows(
    case_id, headers, old_rows, new_rows, expected_header_end, expected_mapped_cols, expected_status, expected_delta
):
    # 多行表头末尾或中间出现括号列号、纯单位或占位符装饰行时，应正确消费装饰行且不影响子表头映射
    old = make_book(old_rows, headers=headers)
    new = make_book(new_rows, headers=headers)
    inv = inventory('D.3')

    sheet = old['sheets'][0]
    rows_by_num = {}
    for cell in sheet['cells'].values():
        rows_by_num.setdefault(cell['row'], {})[cell['column']] = cell
    mapping, header_end = _table_header(rows_by_num, sheet['max_column'], 'name')
    assert header_end == expected_header_end, case_id
    assert len(mapping) == expected_mapped_cols, case_id

    result = compare_cost_reports(old, new, inv)
    assert result['reports'][0]['status'] == expected_status, case_id
    if expected_status == 'changed':
        assert result['reports'][0]['issues'] == [], case_id
        assert len(result['diffs']) == 1, case_id
        assert result['diffs'][0]['delta'] == expected_delta, case_id
    else:
        assert any('存在未映射表头' in issue for issue in result['reports'][0]['issues']), case_id
        assert result['diffs'][0]['kind'] == 'business_issue', case_id


@pytest.mark.parametrize(
    'case_id, code, headers, old_rows, new_rows, expected_mapped_cols',
    [
        (
            'B1_unmapped_yuan_with_body_remark',
            'D.3',
            ['序号', '汇总内容', '金额', '元'],
            [['', '', '', ''], ['1', '土方工程', '100.00', '真实备注内容']],
            [['', '', '', ''], ['1', '土方工程', '100.00', '真实备注内容']],
            3,
        ),
        (
            'B2_unmapped_col4_paren_with_body_remark',
            'D.3',
            ['序号', '汇总内容', '金额', '(4)'],
            [['', '', '', ''], ['1', '土方工程', '100.00', '真实备注内容']],
            [['', '', '', ''], ['1', '土方工程', '100.00', '真实备注内容']],
            3,
        ),
        (
            'B3_unmapped_yuan_single_row_hdr_with_body_remark',
            'D.3',
            ['序号', '汇总内容', '金额', '元'],
            [['1', '土方工程', '100.00', '真实备注内容']],
            [['1', '土方工程', '100.00', '真实备注内容']],
            3,
        ),
        (
            'B4_DIFF_unmapped_col4_body_then_alias_diff',
            'D.3',
            ['序号', '汇总内容', '金额', '备注'],
            [['', '', '', '旧版重要备注：不含暂估'], ['', '', '', '暂估价'], ['1', '土方工程', '100.00', '']],
            [['', '', '', '新版重要备注：含暂估50万'], ['', '', '', '暂估价'], ['1', '土方工程', '100.00', '']],
            3,
        ),
        (
            'D1_note_row_then_colnums',
            'D.3',
            ['序号', '汇总内容', '金额'],
            [['注：单位元', '', ''], ['(1)', '（2）', '(3)'], ['1', '土方工程', '100.00']],
            [['注：单位元', '', ''], ['(1)', '（2）', '(3)'], ['1', '土方工程', '100.00']],
            3,
        ),
        (
            'D2_note_row_in_col4_then_colnums',
            'D.3',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', '注：含税'], ['(1)', '（2）', '(3)', '(4)'], ['1', '土方工程', '100.00', '']],
            [['', '', '', '注：含税'], ['(1)', '（2）', '(3)', '(4)'], ['1', '土方工程', '100.00', '']],
            3,
        ),
        (
            'D3_note_row_in_col4_then_alias_sub_header',
            'D.3',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', '注：本行是数据注记或真实文本'], ['', '', '', '暂估价'], ['1', '分部分项工程费', '100.00', '10.00']],
            [['', '', '', '注：本行是数据注记或真实文本'], ['', '', '', '暂估价'], ['1', '分部分项工程费', '100.00', '10.00']],
            3,
        ),
    ],
)
def test_unmapped_non_empty_body_or_note_row_stops_header_expansion(
    case_id, code, headers, old_rows, new_rows, expected_mapped_cols
):
    # 严禁越过未映射列的非空数据行或注记行继续扩张表头，必须拦截并报告 pending
    old = make_book(old_rows, headers=headers)
    new = make_book(new_rows, headers=headers)
    inv = inventory(code)

    for book in (old, new):
        sheet = book['sheets'][0]
        rows_by_num = {}
        for cell in sheet['cells'].values():
            rows_by_num.setdefault(cell['row'], {})[cell['column']] = cell
        mapping, header_end = _table_header(rows_by_num, sheet['max_column'], 'code' if code == 'E.1' else 'name')
        assert header_end == 1, case_id
        assert len(mapping) == expected_mapped_cols, case_id

    result = compare_cost_reports(old, new, inv)
    assert result['reports'][0]['status'] == 'pending', case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


@pytest.mark.parametrize(
    'case_id, code, headers, old_rows, new_rows, expected_mapped_cols, amount_col',
    [
        (
            'A1_paren_amount_with_real_name',
            'D.3',
            ['序号', '项目名称', '计量单位', '金额'],
            [['', '土方工程', 'm3', '(100.00)']],
            [['', '土方工程', 'm3', '(100.00)']],
            4,
            4,
        ),
        (
            'A2_dash_amount_with_unit_m3_and_name_qita',
            'D.3',
            ['序号', '项目名称', '计量单位', '金额'],
            [['', '其它', 'm3', '-']],
            [['', '其它', 'm3', '-']],
            4,
            4,
        ),
        (
            'A3_dash_amount_with_name_mi',
            'D.3',
            ['序号', '项目名称', '计量单位', '金额'],
            [['', '米', 'm', '-']],
            [['', '米', 'm', '-']],
            4,
            4,
        ),
        (
            'A5_empty_name_dash_amount_alias_remark',
            'D.3',
            ['序号', '汇总内容', '金额', '备注'],
            [['', '', '-', '暂估价'], ['1', '土方工程', '200.00', '']],
            [['', '', '-', '暂估价'], ['1', '土方工程', '200.00', '']],
            3,
            3,
        ),
        (
            'A6_e1_empty_name_unit_m3_dash_amount_alias_remark',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价', '备注'],
            [['', '', '', '', 'm3', '-', '-', '-', '人工费'],
             ['1', '010101001001', '挖土方', '三类土', 'm3', '10', '20', '200.00', '']],
            [['', '', '', '', 'm3', '-', '-', '-', '人工费'],
             ['1', '010101001001', '挖土方', '三类土', 'm3', '10', '20', '200.00', '']],
            8,
            8,
        ),
        (
            'C1_name_zangucailiaofei_col3_yuan_col4_zangujia',
            'D.3',
            ['序号', '汇总内容', '金额', '说明'],
            [['', '暂估材料费', '元', '暂估价']],
            [['', '暂估材料费', '元', '暂估价']],
            3,
            3,
        ),
        (
            'C2_name_zangucailiaofei_col3_zangu_col4_zangujia',
            'D.3',
            ['序号', '汇总内容', '金额', '说明'],
            [['', '暂估材料费', '暂估', '暂估价']],
            [['', '暂估材料费', '暂估', '暂估价']],
            3,
            3,
        ),
        (
            'C3_name_zangucailiaofei_col3_jine_col4_zangujia',
            'D.3',
            ['序号', '汇总内容', '金额', '说明'],
            [['', '暂估材料费', '金额', '暂估价']],
            [['', '暂估材料费', '金额', '暂估价']],
            3,
            3,
        ),
        (
            'C4_name_xiangmu_col3_yuan_col4_zangujia',
            'D.3',
            ['序号', '汇总内容', '金额', '说明'],
            [['', '项目', '元', '暂估价'], ['1', '土方工程', '100.00', '']],
            [['', '项目', '元', '暂估价'], ['1', '土方工程', '100.00', '']],
            3,
            3,
        ),
        (
            'C5_empty_name_col3_xiaoji_col4_zangujia',
            'D.3',
            ['序号', '汇总内容', '金额', '说明'],
            [['', '', '小计', '暂估价'], ['1', '土方工程', '100.00', '']],
            [['', '', '小计', '暂估价'], ['1', '土方工程', '100.00', '']],
            3,
            3,
        ),
        (
            'C6_empty_name_col3_zangujia_col4_rengongfei',
            'D.3',
            ['序号', '汇总内容', '金额', '说明'],
            [['', '', '暂估价', '人工费'], ['1', '土方工程', '100.00', '']],
            [['', '', '暂估价', '人工费'], ['1', '土方工程', '100.00', '']],
            3,
            3,
        ),
        (
            'E1b_3col_paren_2digit_negative_diff',
            'D.3',
            ['序号', '汇总内容', '金额'],
            [['', '', '(12)'], ['1', '土方工程', '200.00']],
            [['', '', '(25)'], ['1', '土方工程', '200.00']],
            3,
            3,
        ),
    ],
)
def test_empty_name_with_placeholder_alias_or_isolated_paren_negative_is_not_swallowed(
    case_id, code, headers, old_rows, new_rows, expected_mapped_cols, amount_col
):
    # 空名称配合占位符/小计/暂估价或孤立括号负数 (12) 不得被吞行，也不得覆写已映射的金额列
    old = make_book(old_rows, headers=headers)
    new = make_book(new_rows, headers=headers)
    inv = inventory(code)

    sheet = old['sheets'][0]
    rows_by_num = {}
    for cell in sheet['cells'].values():
        rows_by_num.setdefault(cell['row'], {})[cell['column']] = cell
    mapping, header_end = _table_header(rows_by_num, sheet['max_column'], 'code' if code == 'E.1' else 'name')
    assert header_end == 1, case_id
    assert len(mapping) == expected_mapped_cols, case_id
    assert mapping[amount_col] == 'amount', case_id

    result = compare_cost_reports(old, new, inv)
    assert result['reports'][0]['status'] == 'pending', case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


def test_four_row_multiline_header_preserves_first_row_mapping_and_decorative_e1():
    # D4：4 行表头含重复行与第 3 行括号列号行，不得因 3 行滑窗丢弃首行“序号”列映射
    d4_headers = ['序号', '汇总内容', '金额', '其中', '']
    d4_old = make_book(
        [
            ['', '汇总内容', '金额', '其中', ''],
            ['(1)', '（2）', '(3)', '(4)', ''],
            ['', '', '', '暂估价', '不可竞争费'],
            ['1.1', '土方工程', '100.00', '10.00', '20.00'],
        ],
        headers=d4_headers,
    )
    d4_new = make_book(
        [
            ['', '汇总内容', '金额', '其中', ''],
            ['(1)', '（2）', '(3)', '(4)', ''],
            ['', '', '', '暂估价', '不可竞争费'],
            ['1.1', '土方工程', '120.00', '10.00', '20.00'],
        ],
        headers=d4_headers,
    )
    rows_by_num = {}
    for cell in d4_old['sheets'][0]['cells'].values():
        rows_by_num.setdefault(cell['row'], {})[cell['column']] = cell
    mapping, header_end = _table_header(rows_by_num, 5, 'name')
    assert header_end == 4
    assert mapping == {1: 'sequence', 2: 'name', 3: 'amount', 4: 'provisional', 5: 'noncompetitive'}

    d4_res = compare_cost_reports(d4_old, d4_new, inventory('D.3'))
    assert d4_res['reports'][0]['status'] == 'changed'
    assert d4_res['reports'][0]['issues'] == []
    assert d4_res['diffs'][0]['row_key'] == '1.1 · 土方工程'
    assert d4_res['diffs'][0]['delta'] == '20.00'

    # E2：真实 E.1 3 行表头 + 第 4 行括号列号装饰行应全部识别 11 列并准确检出工程量变动
    e2_headers = ['序\n号', '项目编码', '项目名称', '项目特征描述', '计量\n单位', '工程量', '', '', '金额', '', '（', '元', '）', '', '', '', '']
    e2_old = make_book(
        [
            ['', '', '', '', '', '', '综合单\n价', '合价', '', '', '', '', '', '', '其中', '', ''],
            ['', '', '', '', '', '', '', '', '', '定额\n人工费', '', '', '', '定额\n机械费', '定额', '', '暂估价'],
            ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)', '', '(9)', '', '', '', '(10)', '', '', '(11)'],
            ['', '001', '垃圾桶及垃圾收集亭子', '', '', '', '', '11914.29', '', '', '', '', '', '', '', '', ''],
            ['1', '011101003001', '细石混凝土楼\n地面', '100厚c20混凝土地面', 'm2', '16.830', '47.25', '795.22', '', '216.43', '', '', '', '0.50', '', '', ''],
        ],
        headers=e2_headers,
        page=6,
    )
    e2_new = copy.deepcopy(e2_old)
    e2_new['sheets'][0]['cells']['F6']['raw_text'] = '17.830'
    e2_rows = {}
    for cell in e2_old['sheets'][0]['cells'].values():
        e2_rows.setdefault(cell['row'], {})[cell['column']] = cell
    e2_mapping, e2_hend = _table_header(e2_rows, e2_old['sheets'][0]['max_column'], 'code')
    assert e2_hend == 4
    assert len(e2_mapping) == 11

    e2_res = compare_cost_reports(e2_old, e2_new, inventory('E.1'))
    assert e2_res['reports'][0]['status'] == 'changed'
    assert e2_res['reports'][0]['issues'] == []
    assert len(e2_res['diffs']) == 1
    assert e2_res['diffs'][0]['field'] == '工程量'
    assert e2_res['diffs'][0]['delta'] == '1.000'


@pytest.mark.parametrize(
    'case_id, headers, old_rows, new_rows, expected_mapping',
    [
        (
            'ATK9_four_row_window_preserves_col3_amount_and_flags_duplicate_xiaoji',
            ['序号', '汇总内容', '金额', '其中', ''],
            [
                ['(1)', '(2)', '(3)', '(4)', ''],
                ['', '', '元', '', ''],
                ['', '', '', '小计', '暂估价'],
                ['1', '土方工程', '100.00', '50.00', '10.00'],
            ],
            [
                ['(1)', '(2)', '(3)', '(4)', ''],
                ['', '', '元', '', ''],
                ['', '', '', '小计', '暂估价'],
                ['1', '土方工程', '999.00', '50.00', '10.00'],
            ],
            {1: 'sequence', 2: 'name', 3: 'amount', 4: 'amount', 5: 'provisional'},
        ),
        (
            'ATK9b_four_row_window_empty_col3_amount_must_not_report_unchanged',
            ['序号', '汇总内容', '金额', '其中', ''],
            [
                ['(1)', '(2)', '(3)', '(4)', ''],
                ['', '', '元', '', ''],
                ['', '', '', '小计', '暂估价'],
                ['1', '土方工程', '', '50.00', '10.00'],
            ],
            [
                ['(1)', '(2)', '(3)', '(4)', ''],
                ['', '', '元', '', ''],
                ['', '', '', '小计', '暂估价'],
                ['1', '土方工程', '', '50.00', '10.00'],
            ],
            {1: 'sequence', 2: 'name', 3: 'amount', 4: 'amount', 5: 'provisional'},
        ),
        (
            'ATK9c_four_row_window_preserves_col1_sequence_against_col5_sequence',
            ['序号', '汇总内容', '金额', '其中', ''],
            [
                ['', '', '元', '', ''],
                ['(1)', '(2)', '(3)', '(4)', ''],
                ['', '', '', '暂估价', '序号'],
                ['1', '土方工程', '100.00', '10.00', '1.1'],
            ],
            [
                ['', '', '元', '', ''],
                ['(1)', '(2)', '(3)', '(4)', ''],
                ['', '', '', '暂估价', '序号'],
                ['1', '土方工程', '120.00', '10.00', '1.1'],
            ],
            {1: 'sequence', 2: 'name', 3: 'amount', 4: 'provisional', 5: 'sequence'},
        ),
        (
            'P1_cross_window_duplicate_empty_amount_vs_hejia_must_report_pending',
            ['序号', '汇总内容', '金额', '其中', ''],
            [
                ['(1)', '(2)', '(3)', '(4)', ''],
                ['', '', '元', '', ''],
                ['', '', '', '合价', '暂估价'],
                ['1', '土方工程', '', '50.00', '10.00'],
            ],
            [
                ['(1)', '(2)', '(3)', '(4)', ''],
                ['', '', '元', '', ''],
                ['', '', '', '合价', '暂估价'],
                ['1', '土方工程', '', '50.00', '10.00'],
            ],
            {1: 'sequence', 2: 'name', 3: 'amount', 4: 'amount', 5: 'provisional'},
        ),
    ],
)
def test_multiline_header_incremental_merge_preserves_prior_columns_and_reports_conflict(
    case_id, headers, old_rows, new_rows, expected_mapping
):
    # 4 行以上多行表头滑窗增量合并时，严禁静默踢掉前序已确立主列，冲突列必须报出 pending
    old = make_book(old_rows, headers=headers)
    new = make_book(new_rows, headers=headers)
    rows_by_num = {}
    for cell in old['sheets'][0]['cells'].values():
        rows_by_num.setdefault(cell['row'], {})[cell['column']] = cell
    mapping, header_end = _table_header(rows_by_num, old['sheets'][0]['max_column'], 'name')
    assert header_end == 4, case_id
    assert mapping == expected_mapping, case_id

    result = compare_cost_reports(old, new, inventory('D.3'))
    assert result['reports'][0]['status'] == 'pending', case_id
    assert any('多个表头映射到相同业务字段' in issue for issue in result['reports'][0]['issues']), case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


@pytest.mark.parametrize(
    'case_id, headers, old_rows, new_rows',
    [
        (
            'ATK1_numeric_col_tax_scope_diff_is_not_swallowed',
            ['序号', '汇总内容', '金额'],
            [['', '', '含税'], ['1', '土方工程', '100.00']],
            [['', '', '不含税'], ['1', '土方工程', '100.00']],
        ),
        (
            'ATK2_numeric_cols_connector_diff_is_not_swallowed',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '人民币', '定额'], ['1', '土方工程', '100.00', '10.00']],
            [['', '', '含税', '其中'], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'ATK3_unmapped_note_col_connector_diff_is_not_swallowed',
            ['序号', '汇总内容', '金额', '备注'],
            [['', '', '', '含税'], ['1', '土方工程', '100.00', '']],
            [['', '', '', '不含税'], ['1', '土方工程', '100.00', '']],
        ),
        (
            'ATK3b_ATK4_unmapped_unit_col_connector_diff_is_not_swallowed',
            ['序号', '汇总内容', '金额', '元'],
            [['', '', '', '定额'], ['1', '土方工程', '100.00', '']],
            [['', '', '', '其中'], ['1', '土方工程', '100.00', '']],
        ),
        (
            'ATK5_unmapped_modifier_col_unit_diff_without_subcol_is_not_swallowed',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', '万元'], ['1', '土方工程', '100.00', '']],
            [['', '', '', '元'], ['1', '土方工程', '100.00', '']],
        ),
        (
            'ATK6_two_col_out_of_order_paren_negatives_are_not_colnums',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(12)', '(5)'], ['1', '土方工程', '100.00', '10.00']],
            [['', '', '(25)', '(5)'], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'ATK6b_numeric_only_ascending_paren_negatives_are_not_colnums',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(1)', '(3)'], ['1', '土方工程', '100.00', '10.00']],
            [['', '', '(2)', '(3)'], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'ATK7_name_col_currency_unit_with_dash_is_not_decorative',
            ['序号', '汇总内容', '金额'],
            [['', '元', '-'], ['1', '土方工程', '100.00']],
            [['', '万元', '-'], ['1', '土方工程', '100.00']],
        ),
        (
            'ATK8_single_dash_plus_unmapped_unit_is_not_decorative',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '-', '元'], ['1', '土方工程', '100.00', '']],
            [['', '', '-', '万元'], ['1', '土方工程', '100.00', '']],
        ),
        (
            'ATK8b_isolated_unmapped_unit_without_subcol_is_not_decorative',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', '元'], ['1', '土方工程', '100.00', '']],
            [['', '', '', '万元'], ['1', '土方工程', '100.00', '']],
        ),
    ],
)
def test_unmapped_continuation_or_connector_units_and_paren_negatives_report_pending(
    case_id, headers, old_rows, new_rows
):
    # 口径连接词、未映射列无后续子列的修饰/单位差异、name 列货币单位及非表头括号负数不得被吞行假报 unchanged
    old = make_book(old_rows, headers=headers)
    new = make_book(new_rows, headers=headers)
    rows_by_num = {}
    for cell in old['sheets'][0]['cells'].values():
        rows_by_num.setdefault(cell['row'], {})[cell['column']] = cell
    _, header_end = _table_header(rows_by_num, old['sheets'][0]['max_column'], 'name')
    assert header_end == 1, case_id

    result = compare_cost_reports(old, new, inventory('D.3'))
    assert result['reports'][0]['status'] == 'pending', case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


def test_centered_spanning_amount_heading_deduplicates_without_phantom_resurrection():
    # ATK14：居中合并单单元格“金额（元）”跨列拆出“综合单价”+“合价”时应消重且准确比较
    headers = ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', '']
    old = make_book(
        [
            ['', '', '', '', '', '', '综合单价', '', '合价'],
            ['1', '010101001001', '挖土方', '三类土', 'm3', '10', '20', '', '200'],
        ],
        headers=headers,
    )
    new = make_book(
        [
            ['', '', '', '', '', '', '综合单价', '', '合价'],
            ['1', '010101001001', '挖土方', '三类土', 'm3', '10', '25', '', '250'],
        ],
        headers=headers,
    )
    rows_by_num = {}
    for cell in old['sheets'][0]['cells'].values():
        rows_by_num.setdefault(cell['row'], {})[cell['column']] = cell
    mapping, header_end = _table_header(rows_by_num, old['sheets'][0]['max_column'], 'code')
    assert header_end == 2
    assert mapping == {1: 'sequence', 2: 'code', 3: 'name', 4: 'description', 5: 'unit', 6: 'quantity', 7: 'unit_price', 9: 'amount'}

    result = compare_cost_reports(old, new, inventory('E.1'))
    assert result['reports'][0]['status'] == 'changed'
    assert result['reports'][0]['issues'] == []
    assert {(d['field'], d['delta']) for d in result['diffs']} == {('综合单价', '5'), ('金额', '50')}


@pytest.mark.parametrize(
    'case_id, code, headers, old_rows, new_rows, expected_hend, expected_status, expected_delta',
    [
        (
            'ATK12_split_xiangmu_mingcheng_then_unit_then_sub',
            'D.3',
            ['序号', '项目', '金额', '其中'],
            [['', '名称', '元', ''], ['', '', '', '暂估价'], ['1', '土方工程', '100.00', '10.00']],
            [['', '名称', '元', ''], ['', '', '', '暂估价'], ['1', '土方工程', '150.00', '10.00']],
            3,
            'changed',
            '50.00',
        ),
        (
            'ATK13_split_zangu_jia_unmapped_then_mapped',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估'],
            [['', '', '', '价'], ['1', '土方工程', '100.00', '10.00']],
            [['', '', '', '价'], ['1', '土方工程', '115.00', '10.00']],
            2,
            'changed',
            '15.00',
        ),
        (
            'ATK15_legit_5row_header_with_blank_and_two_decorative_layers',
            'D.3',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '元', ''], ['', '', '', ''], ['(1)', '(2)', '(3)', '(4)'], ['', '', '', '暂估价'], ['1', '土方工程', '100.00', '10.00']],
            [['', '', '元', ''], ['', '', '', ''], ['(1)', '(2)', '(3)', '(4)'], ['', '', '', '暂估价'], ['1', '土方工程', '130.00', '10.00']],
            5,
            'changed',
            '30.00',
        ),
        (
            'ATK16_legit_4row_header_split_amount_after_decorative',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额', ''],
            [
                ['', '', '', '', '', '', '', '元', ''],
                ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '', '(8)'],
                ['', '', '', '', '', '', '单价', '', '合价'],
                ['1', '010101001001', '挖土方', '三类土', 'm3', '10', '20', '', '200'],
            ],
            [
                ['', '', '', '', '', '', '', '元', ''],
                ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '', '(8)'],
                ['', '', '', '', '', '', '单价', '', '合价'],
                ['1', '010101001001', '挖土方', '三类土', 'm3', '10', '20', '', '250'],
            ],
            4,
            'changed',
            '50',
        ),
        (
            'ATK17_legit_split_gongchengliang_zonghedanjia',
            'E.1',
            ['序号', '项目编码', '项目', '项目特征描述', '计量单位', '工程', '综合', '合价'],
            [['', '', '名称', '', '', '量', '单价', ''], ['1', '010101001001', '挖土方', '三类土', 'm3', '10', '20', '200']],
            [['', '', '名称', '', '', '量', '单价', ''], ['1', '010101001001', '挖土方', '三类土', 'm3', '12', '20', '200']],
            2,
            'changed',
            '2',
        ),
        (
            'ATK18_two_blank_rows_in_4row_header_blocks_expansion',
            'D.3',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', ''], ['', '', '', ''], ['', '', '', '暂估价'], ['1', '土方工程', '100.00', '10.00']],
            [['', '', '', ''], ['', '', '', ''], ['', '', '', '暂估价'], ['1', '土方工程', '130.00', '10.00']],
            1,
            'pending',
            None,
        ),
        (
            'ATK19_phantom_col_not_resurrected_with_5row_e1',
            'E.1',
            ['序\n号', '项目编码', '项目名称', '项目特征描述', '计量\n单位', '工程量', '', '', '金额', '', '（', '元', '）', '', '', '', ''],
            [
                ['', '', '', '', '', '', '综合单\n价', '合价', '', '', '', '', '', '', '其中', '', ''],
                ['', '', '', '', '', '', '', '', '', '定额\n人工费', '', '', '', '定额\n机械费', '定额', '', '暂估价'],
                ['', '', '', '', '', '', '', '元', '', '元', '', '', '', '元', '', '', '元'],
                ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)', '', '(9)', '', '', '', '(10)', '', '', '(11)'],
                ['1', '011101003001', '细石混凝土地面', '100厚', 'm2', '16.830', '47.25', '795.22', '', '216.43', '', '', '', '0.50', '', '', ''],
            ],
            [
                ['', '', '', '', '', '', '综合单\n价', '合价', '', '', '', '', '', '', '其中', '', ''],
                ['', '', '', '', '', '', '', '', '', '定额\n人工费', '', '', '', '定额\n机械费', '定额', '', '暂估价'],
                ['', '', '', '', '', '', '', '元', '', '元', '', '', '', '元', '', '', '元'],
                ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)', '', '(9)', '', '', '', '(10)', '', '', '(11)'],
                ['1', '011101003001', '细石混凝土地面', '100厚', 'm2', '17.830', '47.25', '795.22', '', '216.43', '', '', '', '0.50', '', '', ''],
            ],
            5,
            'changed',
            '1.000',
        ),
    ],
)
def test_legit_split_and_five_row_headers_remain_supported_without_regression(
    case_id, code, headers, old_rows, new_rows, expected_hend, expected_status, expected_delta
):
    # 验证合法跨行拆字、5 行多行表头、4 行含装饰拆分子列及 5 行 E.1 不复活幻影列均不退化
    old = make_book(old_rows, headers=headers)
    new = make_book(new_rows, headers=headers)
    rows_by_num = {}
    for cell in old['sheets'][0]['cells'].values():
        rows_by_num.setdefault(cell['row'], {})[cell['column']] = cell
    _, header_end = _table_header(rows_by_num, old['sheets'][0]['max_column'], 'code' if code == 'E.1' else 'name')
    assert header_end == expected_hend, case_id

    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == expected_status, case_id
    if expected_status == 'changed':
        assert result['reports'][0]['issues'] == [], case_id
        assert len(result['diffs']) == 1, case_id
        assert result['diffs'][0]['delta'] == expected_delta, case_id
    else:
        assert result['diffs'][0]['kind'] == 'business_issue', case_id


@pytest.mark.parametrize(
    'case_id, old_headers, old_rows, new_headers, new_rows',
    [
        (
            'NEW_ATK_A1_paren_neg_with_unmapped_col4',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '(1)', '(2)'], ['1', '土方工程', '100.00', '']],
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '(2)', '(3)'], ['1', '土方工程', '100.00', '']],
        ),
        (
            'NEW_ATK_A2_paren_neg_with_blank_unmapped_col4',
            ['序号', '汇总内容', '金额', ''],
            [['', '', '(1)', '(2)'], ['1', '土方工程', '100.00', '']],
            ['序号', '汇总内容', '金额', ''],
            [['', '', '(1)', '(3)'], ['1', '土方工程', '100.00', '']],
        ),
        (
            'NEW_ATK_D9_formula_paren_does_not_exempt_numeric_col',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(12)', '(4)=(2)+(3)'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(25)', '(4)=(2)+(3)'], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'ATK_formula_paren_bypass_range_check_in_col3_and_col4',
            ['序号', '汇总内容', '金额', '暂估价', '其中'],
            [['', '', '(12)', '(1)', '(5)=(2)+(3)'], ['1', '土方工程', '100.00', '10.00', '']],
            ['序号', '汇总内容', '金额', '暂估价', '其中'],
            [['', '', '(99)', '(1)', '(5)=(2)+(3)'], ['1', '土方工程', '100.00', '10.00', '']],
        ),
        (
            'NEW_ATK_B1_header_unit_diff_yuan_vs_wanyuan_3col',
            ['序号', '汇总内容', '金额'],
            [['', '', '元'], ['1', '土方工程', '100.00']],
            ['序号', '汇总内容', '金额'],
            [['', '', '万元'], ['1', '土方工程', '100.00']],
        ),
        (
            'NEW_ATK_B2_header_unit_diff_on_mapped_plus_unmapped',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '元', '万元'], ['1', '土方工程', '100.00', '']],
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '元', '元'], ['1', '土方工程', '100.00', '']],
        ),
        (
            'NEW_ATK_B3_header_unit_diff_in_5row_header',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '元', ''], ['', '', '', ''], ['(1)', '(2)', '(3)', '(4)'], ['', '', '', '暂估价'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '万元', ''], ['', '', '', ''], ['(1)', '(2)', '(3)', '(4)'], ['', '', '', '暂估价'], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'NEW_ATK_B4_header_colnum_diff_legit_both_sides',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['(1)', '(2)', '(3)', '(4)'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['(1)', '(2)', '', '(4)'], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'NEW_ATK_B5_header_modifier_diff_dinge_vs_buhanshui',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', '定额'], ['', '', '', '暂估价'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', '不含税'], ['', '', '', '暂估价'], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'ATK_unmapped_col4_unit_diff_committed_by_col5_subcol',
            ['序号', '汇总内容', '金额', '其中', ''],
            [['', '', '', '万元', ''], ['', '', '', '', '暂估价'], ['1', '土方工程', '100.00', '', '10.00']],
            ['序号', '汇总内容', '金额', '其中', ''],
            [['', '', '', '元', ''], ['', '', '', '', '暂估价'], ['1', '土方工程', '100.00', '', '10.00']],
        ),
    ],
)
def test_paren_colnum_bypasses_and_header_unit_modifier_diffs_report_pending(
    case_id, old_headers, old_rows, new_headers, new_rows
):
    # 括号列号未映射/同行公式绕过、跨版表头单位/口径修饰/列号结构差异及跨列挂起吞并必须报 pending
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    result = compare_cost_reports(old, new, inventory('D.3'))
    assert result['reports'][0]['status'] == 'pending', case_id
    assert result['reports'][0]['issues'], case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, new_headers, new_rows, expected_hend, expected_delta',
    [
        (
            'NEW_ATK_C1_LEGIT_E1_split_xiangmu_mingcheng_tezheng_miaoshu',
            'E.1',
            ['序号', '项目编码', '项目', '项目特征', '计量单位', '工程量', '综合单价', '合价'],
            [['', '', '名称', '描述', '', '', '', ''], ['1', '010101001001', '挖土方', '三类土', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目', '项目特征', '计量单位', '工程量', '综合单价', '合价'],
            [['', '', '名称', '描述', '', '', '', ''], ['1', '010101001001', '挖土方', '三类土', 'm3', '10', '20', '250']],
            2,
            '50',
        ),
        (
            'NEW_ATK_C2_LEGIT_D3_split_xiangmu_tezheng_miaoshu',
            'D.3',
            ['序号', '汇总内容', '金额', '项目特征'],
            [['', '', '', '描述'], ['1', '土方工程', '100.00', '深度2m']],
            ['序号', '汇总内容', '金额', '项目特征'],
            [['', '', '', '描述'], ['1', '土方工程', '120.00', '深度2m']],
            2,
            '20.00',
        ),
        (
            'LEGIT_D3_split_tezheng_miaoshu_then_unit',
            'D.3',
            ['序号', '汇总内容', '金额', '项目特征'],
            [['', '', '', '描述'], ['', '', '元', ''], ['1', '土方工程', '100.00', '深度2m']],
            ['序号', '汇总内容', '金额', '项目特征'],
            [['', '', '', '描述'], ['', '', '元', ''], ['1', '土方工程', '120.00', '深度2m']],
            3,
            '20.00',
        ),
        (
            'LEGIT_D3_repeated_amount_and_unit_delayed_rollback',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '合价', '暂估价'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '合价', '暂估价'], ['1', '土方工程', '120.00', '10.00']],
            2,
            '20.00',
        ),
        (
            'NEW_ATK_C3_PROBE2_ATK14_with_colnum_dash_row',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '-', '(8)'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '-', '(8)'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '250']],
            3,
            '50',
        ),
        (
            'NEW_ATK_C4_PROBE1_ATK14_with_yuan_row',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['', '', '', '', '', '', '元', '元', '元'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['', '', '', '', '', '', '元', '元', '元'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '250']],
            3,
            '50',
        ),
        (
            'NEW_ATK_C5_spanning_jine_wanyuan_split',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额', '（', '万元', '）'],
            [['', '', '', '', '', '', '综合单价', '', '合价', ''], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200', '']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额', '（', '万元', '）'],
            [['', '', '', '', '', '', '综合单价', '', '合价', ''], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '250', '']],
            2,
            '50',
        ),
        (
            'NEW_ATK_C6_split_zangu_jia_under_qizhong',
            'D.3',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', '暂估'], ['', '', '', '价'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', '暂估'], ['', '', '', '价'], ['1', '土方工程', '120.00', '10.00']],
            3,
            '20.00',
        ),
        (
            'Probe10_13col_e1_split_then_colnum_with_dash',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '', '金额', '', '', '', ''],
            [['', '', '', '', '', '', '综合单价', '合价', '', '', '', '', ''], ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)', '-', '', '', '', ''], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200', '', '', '', '', '']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '', '金额', '', '', '', ''],
            [['', '', '', '', '', '', '综合单价', '合价', '', '', '', '', ''], ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)', '-', '', '', '', ''], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '250', '', '', '', '', '']],
            3,
            '50',
        ),
        (
            'Probe12_centered_zaojia_yuan_spanning',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '造价（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '造价（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '250']],
            2,
            '50',
        ),
        (
            'LEGIT_synonym_field_alias_jine_vs_hejia_and_dinge_labor',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '金额', '定额人工费'],
            [['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200', '50']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价', '人工费'],
            [['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '250', '50']],
            1,
            '50',
        ),
    ],
)
def test_legit_split_repeated_subheaders_spanning_dedup_and_synonym_aliases(
    case_id, code, old_headers, old_rows, new_headers, new_rows, expected_hend, expected_delta
):
    # 已映射列跨行拆字、同列细化/重复子表头、父修饰下拆字、ATK14 后跟装饰行消重、居中造价及同义字段别名均应正常通过
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    rows_by_num = {}
    for cell in old['sheets'][0]['cells'].values():
        rows_by_num.setdefault(cell['row'], {})[cell['column']] = cell
    _, header_end = _table_header(rows_by_num, old['sheets'][0]['max_column'], 'code' if code == 'E.1' else 'name')
    assert header_end == expected_hend, case_id

    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'changed', case_id
    assert result['reports'][0]['issues'] == [], case_id
    assert len(result['diffs']) == 1, case_id
    assert result['diffs'][0]['delta'] == expected_delta, case_id


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, new_headers, new_rows',
    [
        (
            'B1_ATK_I5_F1_paren_formula_operator_diff_in_colnum_row',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['(1)', '(2)', '(3)', '(4)=(2)+(3)'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['(1)', '(2)', '(3)', '(4)=(2)-(3)'], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'B1b_ATK_I5_S20_e1_5row_formula_mul_vs_add',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '', '金额', '', '（', '元', '）', '', '', '', ''],
            [
                ['', '', '', '', '', '', '综合单价', '合价', '', '', '', '', '', '', '其中', '', ''],
                ['', '', '', '', '', '', '', '', '', '定额人工费', '', '', '', '定额机械费', '定额', '', '暂估价'],
                ['', '', '', '', '', '', '', '元', '', '元', '', '', '', '元', '', '', '元'],
                ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)=(6)×(7)', '', '(9)', '', '', '', '(10)', '', '', '(11)'],
                ['1', '0101', '路面', '100厚', 'm2', '10', '20', '200', '', '10', '', '', '', '5', '', '', ''],
            ],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '', '金额', '', '（', '元', '）', '', '', '', ''],
            [
                ['', '', '', '', '', '', '综合单价', '合价', '', '', '', '', '', '', '其中', '', ''],
                ['', '', '', '', '', '', '', '', '', '定额人工费', '', '', '', '定额机械费', '定额', '', '暂估价'],
                ['', '', '', '', '', '', '', '元', '', '元', '', '', '', '元', '', '', '元'],
                ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)=(6)+(7)', '', '(9)', '', '', '', '(10)', '', '', '(11)'],
                ['1', '0101', '路面', '100厚', 'm2', '10', '20', '200', '', '10', '', '', '', '5', '', '', ''],
            ],
        ),
        (
            'ATK_I5_F2_formula_alone_row_diff',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '', '(4)=(2)+(3)'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '', '(4)=(1)+(2)'], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'ATK_I5_F3_out_of_range_paren_formula_diff',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(12)-(5)', ''], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(25)-(5)', ''], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'ATK_I5_F3b_identical_out_of_range_paren_formula_is_not_header',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(12)-(5)', ''], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(12)-(5)', ''], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'ATK_I5_F5_out_of_range_formula_beside_eq_formula',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(12)-(5)', '(4)=(2)+(3)'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(25)-(5)', '(4)=(2)+(3)'], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'ATK_I5_F7_unmapped_col4_formula_diff_with_dinge_and_subcol',
            'D.3',
            ['序号', '汇总内容', '金额', '其中', ''],
            [['', '', '', '(4)=(2)+(3)', ''], ['', '', '', '定额', ''], ['', '', '', '', '暂估价'], ['1', '土方工程', '100.00', '', '10.00']],
            ['序号', '汇总内容', '金额', '其中', ''],
            [['', '', '', '(4)=(2)-(3)', ''], ['', '', '', '定额', ''], ['', '', '', '', '暂估价'], ['1', '土方工程', '100.00', '', '10.00']],
        ),
        (
            'ATK_I5_F8_two_multidigit_out_of_range_formulas',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(12)+(34)', '(56)-(78)'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '', '(99)+(34)', '(56)-(78)'], ['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'B2_ATK_I5_S15_col4_unit_then_group_then_col5_subcol',
            'D.3',
            ['序号', '汇总内容', '金额', '其中', ''],
            [['', '', '', '万元', ''], ['', '', '', '定额', ''], ['', '', '', '', '暂估价'], ['1', '土方工程', '100.00', '', '10.00']],
            ['序号', '汇总内容', '金额', '其中', ''],
            [['', '', '', '万元', ''], ['', '', '', '定额', ''], ['', '', '', '', '暂估价'], ['1', '土方工程', '120.00', '', '10.00']],
        ),
        (
            'ATK_I5_S16_col4_group_then_unit_then_col5_subcol_symmetric',
            'D.3',
            ['序号', '汇总内容', '金额', '其中', ''],
            [['', '', '', '定额', ''], ['', '', '', '万元', ''], ['', '', '', '', '暂估价'], ['1', '土方工程', '100.00', '', '10.00']],
            ['序号', '汇总内容', '金额', '其中', ''],
            [['', '', '', '定额', ''], ['', '', '', '万元', ''], ['', '', '', '', '暂估价'], ['1', '土方工程', '120.00', '', '10.00']],
        ),
    ],
)
def test_formula_range_signature_and_pending_state_set_block_pending(
    case_id, code, old_headers, old_rows, new_headers, new_rows
):
    # 表头列号公式越界/孤立算式/跨版公式差异及同列 unit+group 顺序混合挂起必须严格报 pending，严禁假报 unchanged
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'pending', case_id
    assert result['reports'][0]['issues'], case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, new_headers, new_rows, expected_diff_kind, expected_delta',
    [
        (
            'ATK_I5_S14_d3_body_key_in_aliases_detects_amount_change',
            'D.3',
            ['序号', '汇总内容', '金额'],
            [['1', '项目', '100.00'], ['2', '土方工程', '50.00']],
            ['序号', '汇总内容', '金额'],
            [['1', '项目', '999.00'], ['2', '土方工程', '50.00']],
            'business_value_changed',
            '899.00',
        ),
        (
            'ATK_I5_P11b_e1_body_code_in_aliases_detects_amount_change',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['1', '编码', '项目1', '特征', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['1', '编码', '项目1', '特征', 'm3', '10', '20', '999']],
            'business_value_changed',
            '799',
        ),
        (
            'ATK_I5_P11_isolated_alias_row_does_not_swallow_into_header_or_report_unchanged',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '汇总内容', '', ''], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '项目名称', '', ''], ['1', '土方工程', '100.00', '10.00']],
            'business_row_removed',
            None,
        ),
    ],
)
def test_body_rows_matching_header_aliases_are_not_silently_skipped(
    case_id, code, old_headers, old_rows, new_headers, new_rows, expected_diff_kind, expected_delta
):
    # 数据行仅因业务键命中表头别名（如“项目”“编码”）或孤立单列重复别名，严禁被静默跳过并假报 unchanged
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'changed', case_id
    assert result['reports'][0]['issues'] == [], case_id
    assert any(d['kind'] == expected_diff_kind and d['delta'] == expected_delta for d in result['diffs']), case_id


def test_real_repeated_full_header_row_inside_body_is_still_filtered():
    # 跨页合并表体中夹杂的完整重复表头行（含业务键与数值列表头词且无数字序号/数值）仍应安全过滤
    headers = ['序号', '汇总内容', '金额', '暂估价']
    old = make_book(
        [['1', '土方工程', '100.00', '10.00'], ['序号', '汇总内容', '金额', '暂估价'], ['2', '砌筑工程', '200.00', '20.00']],
        headers=headers,
    )
    new = make_book(
        [['1', '土方工程', '100.00', '10.00'], ['2', '砌筑工程', '250.00', '20.00']],
        headers=headers,
    )
    result = compare_cost_reports(old, new, inventory('D.3'))
    assert result['reports'][0]['status'] == 'changed'
    assert result['reports'][0]['issues'] == []
    assert len(result['diffs']) == 1 and result['diffs'][0]['delta'] == '50.00'


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, new_headers, new_rows, expected_delta',
    [
        (
            'A1_spanning_jine_col7_vs_centered_col8',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '250']],
            '50',
        ),
        (
            'A3_spanning_jine_yuan_with_redundant_r3_yuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['', '', '', '', '', '', '元', '元', '元'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '250']],
            '50',
        ),
        (
            'A4_atk19_17cols_vs_16cols_blank_shift',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '', '金额', '', '（', '元', '）', '', '', '', ''],
            [
                ['', '', '', '', '', '', '综合单价', '合价', '', '', '', '', '', '', '其中', '', ''],
                ['', '', '', '', '', '', '', '', '', '定额人工费', '', '', '', '定额机械费', '定额', '', '暂估价'],
                ['', '', '', '', '', '', '', '元', '', '元', '', '', '', '元', '', '', '元'],
                ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)', '', '(9)', '', '', '', '(10)', '', '', '(11)'],
                ['1', '0111', '路面', '100', 'm2', '16.83', '47.25', '795.22', '', '216.43', '', '', '', '0.50', '', '', ''],
            ],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '', '金额', '（', '元', '）', '', '', '', ''],
            [
                ['', '', '', '', '', '', '综合单价', '合价', '', '', '', '', '', '其中', '', ''],
                ['', '', '', '', '', '', '', '', '', '定额人工费', '', '', '定额机械费', '定额', '', '暂估价'],
                ['', '', '', '', '', '', '', '元', '', '元', '', '', '元', '', '', '元'],
                ['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)', '', '(9)', '', '', '(10)', '', '', '(11)'],
                ['1', '0111', '路面', '100', 'm2', '17.83', '47.25', '795.22', '', '216.43', '', '', '0.50', '', '', ''],
            ],
            '1.00',
        ),
        (
            'A5_d3_hejia_with_unmapped_yuan_same_row',
            'D.3',
            ['序号', '汇总内容', '金额', ''],
            [['', '', '合价', '元'], ['1', '土方工程', '100.00', '']],
            ['序号', '汇总内容', '金额', ''],
            [['', '', '合价', '元'], ['1', '土方工程', '120.00', '']],
            '20.00',
        ),
        (
            'A5_e1_hejia_with_unmapped_yuan_same_row',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额', ''],
            [['', '', '', '', '', '', '综合单价', '合价', '元'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200', '']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额', ''],
            [['', '', '', '', '', '', '综合单价', '合价', '元'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '250', '']],
            '50',
        ),
        (
            'A7a_ATK_I5_S5_unit_unicode_m2_equivalence',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '', '', '', '', '', '元/㎡', '元'], ['1', '0101', '挖土方', '三类土', 'm2', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '', '', '', '', '', '元/m2', '元'], ['1', '0101', '挖土方', '三类土', 'm2', '10', '25', '250']],
            '50',
        ),
        (
            'ATK_I5_S5b_unit_unicode_m3_equivalence',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '', '', '', '', '', '元/㎥', '元'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '', '', '', '', '', '元/m³', '元'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '25', '250']],
            '50',
        ),
        (
            'ATK_I5_S6_reordered_columns_with_colnums',
            'D.3',
            ['序号', '汇总内容', '金额'],
            [['(1)', '(2)', '(3)'], ['1', '土方工程', '100.00']],
            ['序号', '金额', '汇总内容'],
            [['(1)', '(2)', '(3)'], ['1', '120.00', '土方工程']],
            '20.00',
        ),
        (
            'ATK_I5_S7_reordered_columns_with_qizhong_group',
            'D.3',
            ['序号', '汇总内容', '金额', '其中', ''],
            [['', '', '', '', '暂估价'], ['1', '土方工程', '100.00', '', '10.00']],
            ['序号', '汇总内容', '其中', '', '金额'],
            [['', '', '', '暂估价', ''], ['1', '土方工程', '', '10.00', '120.00']],
            '20.00',
        ),
        (
            'ATK_I5_S8_spanning_amount_yuan_vs_parent_plus_child_yuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['', '', '', '', '', '', '元', '', '元'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '250']],
            '50',
        ),
        (
            'ATK_I5_S9_spanning_amount_shifted_center_col',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额（元）', '', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '250']],
            '50',
        ),
    ],
)
def test_equivalent_header_normalization_spanning_alignment_and_column_reorder(
    case_id, code, old_headers, old_rows, new_headers, new_rows, expected_delta
):
    # 等价单位符号（㎡/m²/m2、㎥/m³/m3）、跨列父表头对齐差异、冗余单位行、空列位移、同行装饰及列重排均应准确比对不误报 pending
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'changed', (case_id, result['reports'][0]['issues'])
    assert result['reports'][0]['issues'] == [], case_id
    assert any(d['delta'] == expected_delta for d in result['diffs']), case_id


@pytest.mark.parametrize('omit_kind', ['colnums', 'units', 'spanning'])
def test_multipage_continuation_sheet_allows_omitted_colnum_unit_or_spanning_parent(omit_kind):
    # MP1/A6、MP2、MP3：同版多页续表第 2 页省略列号行、单位行或跨列父表头直接写子列名应正常通过
    base_headers = ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价']
    row1 = ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200']
    row2 = ['2', '0102', '填土方', '夯填', 'm3', '5', '10', '50']
    if omit_kind == 'colnums':
        p1_h, p1_rows = base_headers, [['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)'], row1]
        p2_h, p2_rows = base_headers, [row2]
    elif omit_kind == 'units':
        p1_h, p1_rows = base_headers, [['', '', '', '', '', '', '元', '元'], row1]
        p2_h, p2_rows = base_headers, [row2]
    else:
        p1_h = ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', '']
        p1_rows = [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200']]
        p2_h, p2_rows = base_headers, [row2]

    old = make_book(p1_rows, headers=p1_h, page=1, name='p1')
    old['sheets'] += make_book(p2_rows, headers=p2_h, page=2, name='p2')['sheets']
    new = copy.deepcopy(old)
    new['sheets'][1]['cells']['H2']['raw_text'] = '80'

    result = compare_cost_reports(old, new, inventory('E.1', names=['p1', 'p2']))
    assert result['reports'][0]['status'] == 'changed', (omit_kind, result['reports'][0]['issues'])
    assert result['reports'][0]['issues'] == [], omit_kind
    assert len(result['diffs']) == 1 and result['diffs'][0]['delta'] == '30'


def test_multipage_continuation_sheet_explicit_unit_conflict_still_reports_pending():
    # 续表第 2 页显式出现与首页冲突的单位（如首页 元 vs 续表 万元）必须继续阻断为 pending
    base_headers = ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价']
    old = make_book([['', '', '', '', '', '', '元', '元'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200']], headers=base_headers, page=1, name='p1')
    old['sheets'] += make_book([['', '', '', '', '', '', '元', '万元'], ['2', '0102', '填土方', '夯填', 'm3', '5', '10', '50']], headers=base_headers, page=2, name='p2')['sheets']
    result = compare_cost_reports(old, copy.deepcopy(old), inventory('E.1', names=['p1', 'p2']))
    assert result['reports'][0]['status'] == 'pending'
    assert any('续表表头' in issue for issue in result['reports'][0]['issues'])


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, new_headers, new_rows',
    [
        (
            'ADV1_d3_reordered_cols_with_unchanged_colnum_formula_semantic_reversal',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价', '规费'],
            [['(1)', '(2)', '(3)', '(4)', '(5)=(3)-(4)'], ['1', '土方工程', '100.00', '10.00', '90.00']],
            ['序号', '汇总内容', '暂估价', '金额', '规费'],
            [['(1)', '(2)', '(3)', '(4)', '(5)=(3)-(4)'], ['1', '土方工程', '10.00', '100.00', '90.00']],
        ),
        (
            'ADV1b_e1_reordered_quantity_unit_price_with_unchanged_div_formula',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)=(6)/(7)'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '0.5']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '综合单价', '工程量', '合价'],
            [['(1)', '(2)', '(3)', '(4)', '(5)', '(6)', '(7)', '(8)=(6)/(7)'], ['1', '0101', '挖土方', '三类土', 'm3', '20', '10', '0.5']],
        ),
        (
            'ADV2_spanning_amount_does_not_swallow_distant_unmapped_yuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', '', '元', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价', '', '暂估价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200', '', '10']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', '', '', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价', '', '暂估价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200', '', '10']],
        ),
        (
            'BREACH_2a_spanning_parent_and_child_unit_reversal_conflict',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额', '（', '万元', '）'],
            [['', '', '', '', '', '', '综合单价', '', '合价', '', ''], ['', '', '', '', '', '', '元', '', '元', '', ''], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200', '', '']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额', '（', '元', '）'],
            [['', '', '', '', '', '', '综合单价', '', '合价', '', ''], ['', '', '', '', '', '', '万元', '', '万元', '', ''], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200', '', '']],
        ),
        (
            'BREACH_2b_spanning_amount_cannot_cross_blank_into_right_unmapped_unit',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额（元）', '', '万元', ''],
            [['', '', '', '', '', '', '综合单价', '合价', '', ''], ['', '', '', '', '', '', '', '', '', '暂估价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200', '', '5']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额（万元）', '', '元', ''],
            [['', '', '', '', '', '', '综合单价', '合价', '', ''], ['', '', '', '', '', '', '', '', '', '暂估价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200', '', '5']],
        ),
        (
            'BREACH_2c_spanning_amount_cannot_cross_left_mapped_cols_into_unmapped_unit',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '万元', '', '', '', '金额（元）', ''],
            [['', '', '', '', '', '计量单位', '工程量', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', '', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '元', '', '', '', '金额（万元）', ''],
            [['', '', '', '', '', '计量单位', '工程量', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', '', 'm3', '10', '20', '', '200']],
        ),
        (
            'ADV4_BREACH_1a_body_row_cross_field_alias_in_amount_is_not_skipped',
            'D.3',
            ['序号', '汇总内容', '金额'],
            [['', '项目', '暂估价'], ['1', '土方工程', '100.00']],
            ['序号', '汇总内容', '金额'],
            [['', '项目', '人工费'], ['1', '土方工程', '100.00']],
        ),
        (
            'ADV4b_body_row_one_side_cross_field_alias_vs_number_blocks_pending',
            'D.3',
            ['序号', '汇总内容', '金额'],
            [['', '项目', '暂估价'], ['1', '土方工程', '100.00']],
            ['序号', '汇总内容', '金额'],
            [['', '项目', '100.00'], ['1', '土方工程', '100.00']],
        ),
        (
            'ADV4c_body_row_exact_key_with_cross_field_alias_in_amount_blocks_pending',
            'D.3',
            ['序号', '汇总内容', '金额'],
            [['', '汇总内容', '暂估价'], ['1', '土方工程', '100.00']],
            ['序号', '汇总内容', '金额'],
            [['', '汇总内容', '人工费'], ['1', '土方工程', '100.00']],
        ),
        (
            'BREACH_1b_e1_body_row_with_code_name_alias_and_zangujia_in_amount',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '编码', '项目', '', '', '', '', '暂估价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200']],
        ),
        (
            'BREACH_1c_d3_body_row_cross_field_alias_in_provisional_blocks_pending',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '项目', '', '人工费'], ['1', '土方工程', '50.00', '5.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '项目', '', '机械费'], ['1', '土方工程', '50.00', '5.00']],
        ),
        (
            'BREACH_3a_single_amount_col_unit_price_vs_hejia_blocks_pending',
            'D.3',
            ['序号', '汇总内容', '金额'],
            [['', '汇总内容', '综合单价'], ['1', '土方工程', '100.00']],
            ['序号', '汇总内容', '金额'],
            [['', '汇总内容', '合价'], ['1', '土方工程', '100.00']],
        ),
        (
            'BREACH_3b_single_amount_col_jine_vs_xiaoji_blocks_pending',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '汇总内容', '金额', ''], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['', '汇总内容', '小计', ''], ['1', '土方工程', '100.00', '10.00']],
        ),
    ],
)
def test_formula_field_binding_spanning_bounds_and_cross_field_body_guards_block_pending(
    case_id, code, old_headers, old_rows, new_headers, new_rows
):
    # 列重排公式语义颠倒、跨列金额越界/父子单位冲突、表体跨字段别名及单列异义子表头必须全部拦截报 pending
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'pending', case_id
    assert result['reports'][0]['issues'], case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


def test_reordered_columns_with_semantically_equivalent_formula_remains_comparable():
    # 列位置互换且公式按业务字段同步等价改写（规费 = 金额 - 暂估价）应允许正常比较
    old = make_book(
        [['(1)', '(2)', '(3)', '(4)', '(5)=(3)-(4)'], ['1', '土方工程', '100.00', '10.00', '90.00']],
        headers=['序号', '汇总内容', '金额', '暂估价', '规费'],
    )
    new = make_book(
        [['(1)', '(2)', '(3)', '(4)', '(5)=(4)-(3)'], ['1', '土方工程', '10.00', '120.00', '110.00']],
        headers=['序号', '汇总内容', '暂估价', '金额', '规费'],
    )
    result = compare_cost_reports(old, new, inventory('D.3'))
    assert result['reports'][0]['status'] == 'changed'
    assert result['reports'][0]['issues'] == []
    assert {(d['field'], d['delta']) for d in result['diffs']} == {('金额', '20.00'), ('规费', '20.00')}


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, new_headers, new_rows, expected_delta',
    [
        (
            'ADV3b_distant_unmapped_wanyuan_does_not_pollute_equivalent_amount_units',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', '', '万元', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价', '', '暂估价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200', '', '10']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '', '', '万元', ''],
            [['', '', '', '', '', '', '综合单价（元）', '', '合价（元）', '', '暂估价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '25', '', '250', '', '10']],
            '50',
        ),
        (
            'FALSE_PENDING_4a_fullwidth_plus_minus_formula_equivalence',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['（1）', '（2）', '（3）', '（4）＝（2）＋（3）'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['(1)', '(2)', '(3)', '(4)=(2)+(3)'], ['1', '土方工程', '120.00', '10.00']],
            '20.00',
        ),
        (
            'FALSE_PENDING_4b_single_cell_spanning_jine_wanyuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（万元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（万元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '250']],
            '50',
        ),
        (
            'FALSE_PENDING_4b_single_cell_spanning_feiyong_yuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '费用（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '费用（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '250']],
            '50',
        ),
    ],
)
def test_legit_fullwidth_formulas_spanning_suffixed_parents_and_isolated_units(
    case_id, code, old_headers, old_rows, new_headers, new_rows, expected_delta
):
    # 全角加减号公式、带单位后缀单单元格跨列父表头及远端独立未映射单位不污染子列单位均应正常通过
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'changed', (case_id, result['reports'][0]['issues'])
    assert result['reports'][0]['issues'] == [], case_id
    assert any(d['delta'] == expected_delta for d in result['diffs']), case_id


def test_c5a_three_page_continuation_with_spanning_parent_and_compound_unit_price():
    # C5a：3 页续表 P1 跨列“金额（元）”+子列“元/m2”“元”、P2 列重排省略装饰、P3 恢复“元/㎡”“元”应正常通过
    p1 = make_book(
        [['', '', '', '', '', '', '综合单价', '', '合价'], ['', '', '', '', '', '', '元/m2', '', '元'], ['1', '0101', '挖土方', '三类土', 'm2', '10', '20', '', '200']],
        headers=['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
        page=1,
        name='p1',
    )
    p2 = make_book(
        [['2', '0102', '填土方', '夯填', 'm2', '5', '50', '10']],
        headers=['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '合价', '综合单价'],
        page=2,
        name='p2',
    )
    p3 = make_book(
        [['', '', '', '', '', '', '元/㎡', '元'], ['3', '0103', '运土方', '5km', 'm2', '8', '15', '120']],
        headers=['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
        page=3,
        name='p3',
    )
    old = {'sheets': p1['sheets'] + p2['sheets'] + p3['sheets']}
    new = copy.deepcopy(old)
    new['sheets'][2]['cells']['H3']['raw_text'] = '150'
    result = compare_cost_reports(old, new, inventory('E.1', names=['p1', 'p2', 'p3']))
    assert result['reports'][0]['status'] == 'changed'
    assert result['reports'][0]['issues'] == []
    assert len(result['diffs']) == 1 and result['diffs'][0]['delta'] == '30'


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, new_headers, new_rows',
    [
        # m1_reviewer_13 P1 & m1_challenger_13 B_I7_1/B_I7_7/B_I7_2/B_I7_2b：跨列父表头多货币冲突与跨行漏采单位
        (
            'R13_P1_spanning_subrow_unmapped_col_unit_conflict',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['', '', '', '', '', '', '元', '万元', '元'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['', '', '', '', '', '', '元', '元', '元'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '', '200']],
        ),
        (
            'B_I7_1_spanning_parent_internal_multi_currency_conflict',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额（元）', '', '（万元）'],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额（万元）', '', '（元）'],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '', '200']],
        ),
        (
            'B_I7_7_spanning_parent_split_brackets_multi_currency_conflict',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额（元）', '（', '万元', '）'],
            [['', '', '', '', '', '', '综合单价', '', '', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额（万元）', '（', '元', '）'],
            [['', '', '', '', '', '', '综合单价', '', '', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '', '', '200']],
        ),
        (
            'B_I7_2_spanning_consumed_unmapped_subrow_unit_conflict',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '万元', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '元', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '', '200']],
        ),
        (
            'B_I7_2b_spanning_consumed_unmapped_subrow_unit_vs_blank',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '万元', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '', '金额（元）', ''],
            [['', '', '', '', '', '', '综合单价', '', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '', '200']],
        ),
        # m1_reviewer_14 CRIT_F1* & m1_challenger_13 A_I7_1/A_I7_4：部分列号下公式字段绑定语义颠倒
        (
            'CRIT_F1_A_I7_1_partial_colnums_with_eq_formula_swap',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价', '规费'],
            [['(1)', '(2)', '元', '元', '(5)=(3)-(4)'], ['1', '土方工程', '100.00', '10.00', '90.00']],
            ['序号', '汇总内容', '暂估价', '金额', '规费'],
            [['(1)', '(2)', '元', '元', '(5)=(3)-(4)'], ['1', '土方工程', '10.00', '100.00', '90.00']],
        ),
        (
            'CRIT_F1b_multiline_partial_colnums_swap',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价', '规费'],
            [['(1)', '(2)', '元', '元', '元'], ['', '', '', '', '(5)=(3)-(4)'], ['1', '土方工程', '100.00', '10.00', '90.00']],
            ['序号', '汇总内容', '暂估价', '金额', '规费'],
            [['(1)', '(2)', '元', '元', '元'], ['', '', '', '', '(5)=(3)-(4)'], ['1', '土方工程', '10.00', '100.00', '90.00']],
        ),
        (
            'CRIT_F1d_two_formulas_partial_colnums_swap',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价', '规费', '税金'],
            [['(1)', '(2)', '元', '元', '(5)=(3)-(4)', '(6)=(5)+(4)'], ['1', '土方工程', '100.00', '10.00', '90.00', '100.00']],
            ['序号', '汇总内容', '暂估价', '金额', '规费', '税金'],
            [['(1)', '(2)', '元', '元', '(5)=(3)-(4)', '(6)=(5)+(4)'], ['1', '土方工程', '10.00', '100.00', '90.00', '100.00']],
        ),
        (
            'A_I7_4_partial_colnums_with_no_eq_formula_swap',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价', '规费'],
            [['(1)', '(2)', '', '', '(3)-(4)'], ['1', '土方工程', '100.00', '10.00', '90.00']],
            ['序号', '汇总内容', '暂估价', '金额', '规费'],
            [['(1)', '(2)', '', '', '(3)-(4)'], ['1', '土方工程', '10.00', '100.00', '90.00']],
        ),
        # m1_reviewer_13 P2a/P2b、m1_reviewer_14 CRIT_B1*、m1_challenger_13 C_I7_* 及伪占位符/伪列号/伪公式表体拦截
        (
            'R13_P2a_d3_xiaoji_col_body_jine_bypass',
            'D.3',
            ['序号', '汇总内容', '小计', '暂估价'],
            [['', '汇总内容', '金额', '暂估价'], ['1', '土方工程', '100.00', '10.00']],
            ['序号', '汇总内容', '小计', '暂估价'],
            [['1', '土方工程', '100.00', '10.00']],
        ),
        (
            'R13_P2a_e1_hejia_col_body_jine_wanyuan_bypass',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '项目编码', '项目名称', '', '', '', '', '金额（万元）'], ['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['1', '0101', '挖土方', '三类土', 'm3', '10', '20', '200']],
        ),
        (
            'R13_P2b_body_unit_conflict_wanyuan_vs_yuan',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['1', '土方工程', '100.00', '10.00'], ['', '汇总内容', '万元', '暂估价'], ['2', '石方工程', '200.00', '20.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['1', '土方工程', '100.00', '10.00'], ['', '汇总内容', '元', '暂估价'], ['2', '石方工程', '200.00', '20.00']],
        ),
        (
            'CRIT_B1_exact_key_connector_both_sides',
            'D.3',
            ['序号', '汇总内容', '造价', '暂估价'],
            [['', '汇总内容', '金额', '暂估价'], ['1', '土方', '50.00', '5.00']],
            ['序号', '汇总内容', '造价', '暂估价'],
            [['', '汇总内容', '人民币', '暂估价'], ['1', '土方', '50.00', '5.00']],
        ),
        (
            'CRIT_B1_one_side_connector',
            'D.3',
            ['序号', '汇总内容', '造价', '暂估价'],
            [['', '汇总内容', '金额', '暂估价'], ['1', '土方', '50.00', '5.00']],
            ['序号', '汇总内容', '造价', '暂估价'],
            [['1', '土方', '50.00', '5.00']],
        ),
        (
            'C_I7_1a_d3_body_hanshui_vs_buhanshui',
            'D.3',
            ['序号', '汇总内容', '暂估价', '金额'],
            [['序号', '汇总内容', '含税', ''], ['1', '土方', '10.00', '100.00']],
            ['序号', '汇总内容', '暂估价', '金额'],
            [['序号', '汇总内容', '不含税', ''], ['1', '土方', '10.00', '100.00']],
        ),
        (
            'C_I7_1c_e1_body_hanshui_buhanshui_swap',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '项目编码', '项目名称', '', '', '', '含税', '不含税'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '项目编码', '项目名称', '', '', '', '不含税', '含税'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
        ),
        (
            'C_I7_1d_body_pure_punctuation_parens',
            'D.3',
            ['序号', '汇总内容', '金额'],
            [['序号', '汇总内容', '((()))'], ['1', '土方', '100.00']],
            ['序号', '汇总内容', '金额'],
            [['1', '土方', '100.00']],
        ),
        (
            'C_I7_4_body_jine_wanyuan_under_jine_yuan',
            'D.3',
            ['序号', '汇总内容', '金额（元）'],
            [['1', '土方', '100.00'], ['序号', '汇总内容', '金额（万元）'], ['2', '砌筑', '200.00']],
            ['序号', '汇总内容', '金额（元）'],
            [['1', '土方', '100.00'], ['2', '砌筑', '200.00']],
        ),
        (
            'C_I7_6b_body_wanyuan_under_yuan_unit_row',
            'D.3',
            ['序号', '汇总内容', '金额'],
            [['', '', '元'], ['1', '土方', '100.00'], ['序号', '汇总内容', '万元'], ['2', '砌筑', '200.00']],
            ['序号', '汇总内容', '金额'],
            [['', '', '元'], ['1', '土方', '100.00'], ['2', '砌筑', '200.00']],
        ),
        (
            'FAKE_PH_row2_dash_mixed_with_repeated_labels',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['-', '汇总内容', '-', '暂估价'], ['1', '土方', '100.00', '10.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['1', '土方', '100.00', '10.00']],
        ),
        (
            'FAKE_PH_body_dash_mixed_with_repeated_labels',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['1', '土方', '100.00', '10.00'], ['-', '汇总内容', '-', '暂估价'], ['2', '石方', '200.00', '20.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['1', '土方', '100.00', '10.00'], ['2', '石方', '200.00', '20.00']],
        ),
        (
            'FAKE_CN_body_fake_colnum_row',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['1', '土方', '100.00', '10.00'], ['(1)', '汇总内容', '(3)', '暂估价'], ['2', '石方', '200.00', '20.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['1', '土方', '100.00', '10.00'], ['2', '石方', '200.00', '20.00']],
        ),
        (
            'FAKE_FM_body_fake_formula_row',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价'],
            [['1', '土方', '100.00', '10.00'], ['(1)', '汇总内容', '(3)', '(4)=(1)+(2)'], ['2', '石方', '200.00', '20.00']],
            ['序号', '汇总内容', '金额', '暂估价'],
            [['1', '土方', '100.00', '10.00'], ['2', '石方', '200.00', '20.00']],
        ),
        (
            'NOTE_PAREN_non_unit_note_in_amount_header_not_stripped',
            'D.3',
            ['序号', '汇总内容', '金额（注：不含规费）'],
            [['1', '土方', '100.00']],
            ['序号', '汇总内容', '金额（注：不含规费）'],
            [['1', '土方', '100.00']],
        ),
    ],
)
def test_iter8_adversarial_bypasses_and_non_unit_paren_headers_block_pending(
    case_id, code, old_headers, old_rows, new_headers, new_rows
):
    # Iteration 7 暴露的跨列漏采、父表头冲突、部分列号公式退化、表体修饰/单位/伪占位符/伪列号/伪公式及备注括号必须全部阻断为 pending
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'pending', case_id
    assert result['reports'][0]['issues'], case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, new_headers, new_rows, expected_delta',
    [
        (
            'CRIT_483_rmb_spanning_5col_split',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额', '（', '人民币', '元', '）'],
            [['', '', '', '', '', '', '综合单价', '', '', '', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '', '', '', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '金额', '（', '人民币', '元', '）'],
            [['', '', '', '', '', '', '综合单价', '', '', '', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '', '', '', '250']],
            '50',
        ),
        (
            'CRIT_F1_equiv_partial_colnums_reordered_formula',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价', '规费'],
            [['(1)', '(2)', '元', '元', '(5)=(3)-(4)'], ['1', '土方工程', '100.00', '10.00', '90.00']],
            ['序号', '汇总内容', '暂估价', '金额', '规费'],
            [['(1)', '(2)', '元', '元', '(5)=(4)-(3)'], ['1', '土方工程', '10.00', '120.00', '110.00']],
            '20.00',
        ),
        (
            'SINGLE_ROW_jine_wanyuan',
            'D.3',
            ['序号', '汇总内容', '金额（万元）'],
            [['1', '土方工程', '100.00']],
            ['序号', '汇总内容', '金额（万元）'],
            [['1', '土方工程', '125.00']],
            '25.00',
        ),
        (
            'SINGLE_ROW_feiyong_yuan',
            'D.3',
            ['序号', '汇总内容', '费用（元）'],
            [['1', '土方工程', '100.00']],
            ['序号', '汇总内容', '费用（元）'],
            [['1', '土方工程', '125.00']],
            '25.00',
        ),
        (
            'SINGLE_ROW_zaojia_wanyuan',
            'D.3',
            ['序号', '汇总内容', '造价（万元）'],
            [['1', '土方工程', '100.00']],
            ['序号', '汇总内容', '造价（万元）'],
            [['1', '土方工程', '125.00']],
            '25.00',
        ),
        (
            'LEGIT_repeat_body_header_under_qizhong_group',
            'D.3',
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', '暂估价'], ['1', '土方工程', '100.00', '10.00'], ['序号', '汇总内容', '金额', '其中'], ['2', '石方工程', '200.00', '20.00']],
            ['序号', '汇总内容', '金额', '其中'],
            [['', '', '', '暂估价'], ['1', '土方工程', '100.00', '10.00'], ['2', '石方工程', '250.00', '20.00']],
            '50.00',
        ),
        (
            'LEGIT_repeat_body_header_root_alias_stripped_unit',
            'D.3',
            ['序号', '汇总内容', '金额（元）', '暂估价（元）'],
            [['1', '土方工程', '100.00', '10.00'], ['序号', '汇总内容', '金额', '暂估价'], ['2', '石方工程', '200.00', '20.00']],
            ['序号', '汇总内容', '金额（元）', '暂估价（元）'],
            [['1', '土方工程', '100.00', '10.00'], ['2', '石方工程', '250.00', '20.00']],
            '50.00',
        ),
    ],
)
def test_iter8_legit_rmb_spanning_partial_equiv_single_suffixed_and_repeated_headers(
    case_id, code, old_headers, old_rows, new_headers, new_rows, expected_delta
):
    # 5 列拆分“金额（人民币元）”、部分列号等价公式重排、单行带单位表头及合法同义重复表头应正常检出变动
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'changed', (case_id, result['reports'][0]['issues'])
    assert result['reports'][0]['issues'] == [], case_id
    assert any(d['delta'] == expected_delta for d in result['diffs']), case_id


@pytest.mark.parametrize('parent_label', ['费用（元）', '费用（万元）', '造价（万元）'])
@pytest.mark.parametrize('code', ['E.1', 'D.3'])
def test_real_two_col_merged_spanning_parent_without_spacer_col(code, parent_label):
    # m1_challenger_14：真实 2 列合并父表头（无中间空列，如 G1="费用（元）", H1="" 覆盖 G2="综合单价", H2="合价"）应准确比较
    if code == 'E.1':
        headers = ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', parent_label, '']
        rows_old = [['', '', '', '', '', '', '综合单价', '合价'], ['1', '00124', '土方', '挖土', 'm3', '10', '5.00', '50.00']]
        rows_new = [['', '', '', '', '', '', '综合单价', '合价'], ['1', '00124', '土方', '挖土', 'm3', '10', '5.00', '80.00']]
        merged_range = 'G1:H1'
    else:
        headers = ['序号', '汇总内容', '暂估价', parent_label, '']
        rows_old = [['', '', '', '综合单价', '合价'], ['1', '土方工程', '10.00', '5.00', '50.00']]
        rows_new = [['', '', '', '综合单价', '合价'], ['1', '土方工程', '10.00', '5.00', '80.00']]
        merged_range = 'D1:E1'

    old = make_book(rows_old, headers=headers)
    new = make_book(rows_new, headers=headers)
    old['sheets'][0]['merged_ranges'] = [merged_range]
    new['sheets'][0]['merged_ranges'] = [merged_range]

    unchanged = compare_cost_reports(old, copy.deepcopy(old), inventory(code))
    assert unchanged['reports'][0]['status'] == 'unchanged'
    assert unchanged['reports'][0]['issues'] == []

    changed = compare_cost_reports(old, new, inventory(code))
    assert changed['reports'][0]['status'] == 'changed'
    assert changed['reports'][0]['issues'] == []
    assert len(changed['diffs']) == 1 and changed['diffs'][0]['delta'] == '30.00'


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, old_merges, new_headers, new_rows, new_merges',
    [
        # m1_challenger_15 类别 I：同一已映射字段自身多单位冲突与互换抵消
        (
            'ATK_I8_1a_single_cell_double_units_swap',
            'D.3',
            ['序号', '汇总内容', '金额（元）（万元）'],
            [['1', '土方', '100.00']],
            [],
            ['序号', '汇总内容', '金额（万元）（元）'],
            [['1', '土方', '100.00']],
            [],
        ),
        (
            'ATK_I8_1b_multiline_same_col_unit_conflict_swap',
            'D.3',
            ['序号', '汇总内容', '金额（元）'],
            [['', '', '万元'], ['1', '土方', '100.00']],
            [],
            ['序号', '汇总内容', '金额（万元）'],
            [['', '', '元'], ['1', '土方', '100.00']],
            [],
        ),
        (
            'ATK_I8_1c_compound_unit_same_col_conflict_swap',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价（元/m2）', '合价'],
            [['', '', '', '', '', '', '元/m3', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            [],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价（元/m3）', '合价'],
            [['', '', '', '', '', '', '元/m2', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            [],
        ),
        (
            'ATK_I8_1d_suffix_plus_unit_row_conflict_swap',
            'D.3',
            ['序号', '汇总内容', '金额元'],
            [['', '', '（万元）'], ['1', '土方', '100.00']],
            [],
            ['序号', '汇总内容', '金额万元'],
            [['', '', '（元）'], ['1', '土方', '100.00']],
            [],
        ),
        (
            'ATK_I8_1e_single_side_same_col_dual_unit_conflict',
            'D.3',
            ['序号', '汇总内容', '金额（元）'],
            [['', '', '万元'], ['1', '土方', '100.00']],
            [],
            ['序号', '汇总内容', '金额（元）'],
            [['', '', '万元'], ['1', '土方', '100.00']],
            [],
        ),
        # m1_challenger_15 类别 II：含税/不含税口径冲突跨 Section 及多行/斜向分组修饰列
        (
            'ATK_I8_2a_group_mod_plus_spanning_mod_tax_conflict_swap',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '含税', '', ''],
            [['', '', '', '', '', '', '', '金额（不含税）', ''], ['', '', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '', '20', '200']],
            [],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '不含税', '', ''],
            [['', '', '', '', '', '', '', '金额（含税）', ''], ['', '', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '', '20', '200']],
            [],
        ),
        (
            'ATK_I8_2c_two_row_group_mods_tax_conflict_swap',
            'D.3',
            ['序号', '汇总内容', '含税', ''],
            [['', '', '不含税', ''], ['', '', '', '金额'], ['1', '土方', '', '100.00']],
            [],
            ['序号', '汇总内容', '不含税', ''],
            [['', '', '含税', ''], ['', '', '', '金额'], ['1', '土方', '', '100.00']],
            [],
        ),
        (
            'P2_5_diagonal_two_group_cols_tax_conflict',
            'D.3',
            ['序号', '汇总内容', '含税', '', ''],
            [['', '', '', '不含税', ''], ['', '', '', '', '金额'], ['1', '土方', '', '', '100.00']],
            [],
            ['序号', '汇总内容', '不含税', '', ''],
            [['', '', '', '含税', ''], ['', '', '', '', '金额'], ['1', '土方', '', '', '100.00']],
            [],
        ),
        # m1_challenger_16 Challenge 1：header_end is None 时第 1 行非法备注或未知文本不得被第 2 行覆盖吞没
        (
            'ATTACK_2_5_d3_merged_parent_with_conflicting_notes',
            'D.3',
            ['序号', '项目名称', '费用（注：不含规费）', ''],
            [['', '', '综合单价', '合价'], ['1.1', '人工费', '5.00', '50.00']],
            ['C1:D1'],
            ['序号', '项目名称', '费用（注：含规费）', ''],
            [['', '', '综合单价', '合价'], ['1.1', '人工费', '5.00', '50.00']],
            ['C1:D1'],
        ),
        (
            'ATTACK_A1_d3_merged_parent_note_vs_plain_fee',
            'D.3',
            ['序号', '项目名称', '费用（注：不含规费）', ''],
            [['', '', '综合单价', '合价'], ['1.1', '人工费', '5.00', '50.00']],
            ['C1:D1'],
            ['序号', '项目名称', '费用', ''],
            [['', '', '综合单价', '合价'], ['1.1', '人工费', '5.00', '50.00']],
            ['C1:D1'],
        ),
        (
            'ATTACK_2_6_d1_single_numeric_col_note_swallowed_by_hejia',
            'D.1',
            ['序号', '项目名称', '金额（注：不含规费）'],
            [['', '', '合价'], ['1', '工程A', '100.00']],
            [],
            ['序号', '项目名称', '金额（注：含规费）'],
            [['', '', '合价'], ['1', '工程A', '100.00']],
            [],
        ),
        (
            'ATTACK_A2_e1_quantity_row2_parent_note_swallowed',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '', '费用（注：不含规费）', ''],
            [['', '', '', '', '', '工程量', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '', '费用（注：含规费）', ''],
            [['', '', '', '', '', '工程量', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
        ),
        (
            'ATTACK_A3_d1_unknown_row1_prompt_swallowed_by_amount',
            'D.1',
            ['序号', '项目名称', '请核对是否含税'],
            [['', '', '金额'], ['1', '工程A', '100.00']],
            [],
            ['序号', '项目名称', '已确认不含税'],
            [['', '', '金额'], ['1', '工程A', '100.00']],
            [],
        ),
        (
            'ATTACK_key_row2_e1_code_note_and_parent_note_swallowed',
            'E.1',
            ['序号', '项目编码（注：暂定）', '项目名称', '项目特征描述', '计量单位', '工程量', '费用（注：不含规费）', ''],
            [['', '项目编码', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
            ['序号', '项目编码（注：已定）', '项目名称', '项目特征描述', '计量单位', '工程量', '费用（注：含规费）', ''],
            [['', '项目编码', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
        ),
        # m1_reviewer_16 Minor：跨行同一显式列号指向不同物理列冲突
        (
            'R16_MINOR_cross_row_duplicate_colnum_different_physical_cols',
            'D.3',
            ['序号', '汇总内容', '金额', '暂估价', '规费'],
            [['(1)', '(2)', '(3)', '', ''], ['(1)', '(2)', '', '(3)', '(5)=(1)+(3)'], ['1', '土方', '100', '10', '110']],
            [],
            ['序号', '汇总内容', '金额', '暂估价', '规费'],
            [['(1)', '(2)', '(3)', '', ''], ['(1)', '(2)', '', '(3)', '(5)=(1)+(3)'], ['1', '土方', '100', '10', '110']],
            [],
        ),
    ],
)
def test_iter9_unit_tax_modifier_header_override_and_cross_row_colnum_conflicts_block_pending(
    case_id, code, old_headers, old_rows, old_merges, new_headers, new_rows, new_merges
):
    # 同列多单位冲突、跨 Section 含税/不含税冲突、header_end=None 前序非法备注吞没及跨行同列号跨列冲突必须阻断为 pending
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    if old_merges:
        old['sheets'][0]['merged_ranges'] = old_merges
    if new_merges:
        new['sheets'][0]['merged_ranges'] = new_merges
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'pending', (case_id, result['reports'][0])
    assert result['reports'][0]['issues'], case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, old_merges, new_headers, new_rows, new_merges, expected_delta',
    [
        (
            'LEGIT_equiv_unit_repeated_same_unit_and_rmb_yuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价（元/m2）', '合价（人民币元）'],
            [['', '', '', '', '', '', '元/m2', '人民币元'], ['1', '0101', '挖土', 'C30', 'm2', '10', '20', '200']],
            [],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价（元／㎡）', '合价（人民币元）'],
            [['', '', '', '', '', '', '元／㎡', '人民币元'], ['1', '0101', '挖土', 'C30', 'm2', '10', '20', '250']],
            [],
            '50',
        ),
        (
            'LEGIT_e1_quantity_row2_merged_feiyong_yuan_parent',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '', '费用（元）', ''],
            [['', '', '', '', '', '工程量', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '', '费用（元）', ''],
            [['', '', '', '', '', '工程量', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '250']],
            ['G1:H1'],
            '50',
        ),
        (
            'LEGIT_header_end_none_row1_split_xiangmu_zonghe_hejia',
            'D.3',
            ['序号', '项目', '综合', '合'],
            [['', '名称', '单价', '价'], ['1', '土方', '10', '100']],
            [],
            ['序号', '项目', '综合', '合'],
            [['', '名称', '单价', '价'], ['1', '土方', '10', '150']],
            [],
            '50',
        ),
    ],
)
def test_iter9_legitimate_equivalent_units_merged_parent_and_split_headers_remain_comparable(
    case_id, code, old_headers, old_rows, old_merges, new_headers, new_rows, new_merges, expected_delta
):
    # 合法等价单位重复、工程量在第 2 行的 2 列合并父表头及首行无数值时的合法跨行拆字均应正常比较
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    if old_merges:
        old['sheets'][0]['merged_ranges'] = old_merges
    if new_merges:
        new['sheets'][0]['merged_ranges'] = new_merges
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'changed', (case_id, result['reports'][0]['issues'])
    assert result['reports'][0]['issues'] == [], case_id
    assert any(d['delta'] == expected_delta for d in result['diffs']), case_id


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, old_merges, new_headers, new_rows, new_merges',
    [
        # m1_reviewer_18 与 m1_challenger_17 类别 II：括号外后缀单位被括号内单位/修饰掩蔽（含万倍单位差与复合单位混用）
        (
            'R18_feiyong_yuan_hanshui_vs_wanyuan_hanshui',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '费用元（含税）', ''],
            [['', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '费用万元（含税）', ''],
            [['', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
        ),
        (
            'R18_feiyong_yuan_hanshui_vs_no_unit',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '费用元（含税）', ''],
            [['', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '费用（含税）', ''],
            [['', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
        ),
        (
            'ATK_I9_2a_jine_yuan_paren_wanyuan_vs_wanyuan',
            'D.3',
            ['序号', '汇总内容', '金额元（万元）'],
            [['1', '土方', '100.00']],
            [],
            ['序号', '汇总内容', '金额（万元）'],
            [['1', '土方', '100.00']],
            [],
        ),
        (
            'ATK_I9_2b_jine_yuan_paren_wanyuan_both_sides',
            'D.3',
            ['序号', '汇总内容', '金额元（万元）'],
            [['1', '土方', '100.00']],
            [],
            ['序号', '汇总内容', '金额元（万元）'],
            [['1', '土方', '100.00']],
            [],
        ),
        (
            'R18_feiyong_yuan_paren_wanyuan_merged',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '费用元（万元）', ''],
            [['', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '费用（万元）', ''],
            [['', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
        ),
        (
            'ATK_I9_2c_unit_price_yuan_paren_m2_vs_m2',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价元（元/m2）', '合价'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            [],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价（元/m2）', '合价'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            [],
        ),
        (
            'ATK_I9_2d_generic_parent_yuan_paren_m2',
            'D.3',
            ['序号', '汇总内容', '金额元（元/m2）', ''],
            [['', '', '综合单价', '合价'], ['1', '土方', '20', '200']],
            [],
            ['序号', '汇总内容', '金额（元/m2）', ''],
            [['', '', '综合单价', '合价'], ['1', '土方', '20', '200']],
            [],
        ),
        (
            'ATK_I9_2e_unit_price_wanyuan_paren_m2_vs_m2',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价万元（元/m2）', '合价'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            [],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价（元/m2）', '合价'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            [],
        ),
        (
            'R18_d1_two_row_jine_yuan_vs_wanyuan_hanshui',
            'D.1',
            ['序号', '项目名称', '金额元（含税）'],
            [['', '', '合价'], ['1', '工程A', '100.00']],
            [],
            ['序号', '项目名称', '金额万元（含税）'],
            [['', '', '合价'], ['1', '工程A', '100.00']],
            [],
        ),
        # m1_challenger_17 类别 I：括号外含税/不含税口径修饰被括号掩蔽（ATK_I9_1a-1i）
        (
            'ATK_I9_1a_group_col_tax_with_rmb_paren_swap',
            'D.3',
            ['序号', '汇总内容', '含税（人民币）', ''],
            [['', '', '', '金额'], ['1', '土方', '', '100.00']],
            [],
            ['序号', '汇总内容', '不含税（人民币）', ''],
            [['', '', '', '金额'], ['1', '土方', '', '100.00']],
            [],
        ),
        (
            'ATK_I9_1b_same_col_tax_with_rmb_paren_swap',
            'D.3',
            ['序号', '汇总内容', '含税（人民币）'],
            [['', '', '金额'], ['1', '土方', '100.00']],
            [],
            ['序号', '汇总内容', '不含税（人民币）'],
            [['', '', '金额'], ['1', '土方', '100.00']],
            [],
        ),
        (
            'ATK_I9_1c_group_col_hanshui_paren_buhanshui_vs_buhanshui',
            'D.3',
            ['序号', '汇总内容', '含税（不含税）', ''],
            [['', '', '', '金额'], ['1', '土方', '', '100.00']],
            [],
            ['序号', '汇总内容', '不含税', ''],
            [['', '', '', '金额'], ['1', '土方', '', '100.00']],
            [],
        ),
        (
            'ATK_I9_1d_same_col_hanshui_paren_buhanshui_both_sides',
            'D.3',
            ['序号', '汇总内容', '含税（不含税）'],
            [['', '', '金额'], ['1', '土方', '100.00']],
            [],
            ['序号', '汇总内容', '含税（不含税）'],
            [['', '', '金额'], ['1', '土方', '100.00']],
            [],
        ),
        (
            'ATK_I9_1e_tax_with_dinge_paren_swap',
            'D.3',
            ['序号', '汇总内容', '含税（定额）'],
            [['', '', '金额'], ['1', '土方', '100.00']],
            [],
            ['序号', '汇总内容', '不含税（定额）'],
            [['', '', '金额'], ['1', '土方', '100.00']],
            [],
        ),
        (
            'ATK_I9_1f_tax_with_dinge_and_yuan_paren_swap',
            'D.3',
            ['序号', '汇总内容', '含税（定额）（元）'],
            [['', '', '金额'], ['1', '土方', '100.00']],
            [],
            ['序号', '汇总内容', '不含税（定额）（元）'],
            [['', '', '金额'], ['1', '土方', '100.00']],
            [],
        ),
        (
            'ATK_I9_1g_e1_spanning_group_tax_rmb_paren_swap',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '含税（人民币）', '', ''],
            [['', '', '', '', '', '', '', '金额', ''], ['', '', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '', '20', '200']],
            [],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '不含税（人民币）', '', ''],
            [['', '', '', '', '', '', '', '金额', ''], ['', '', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '', '20', '200']],
            [],
        ),
        (
            'ATK_I9_1h_tax_with_compound_unit_paren_swap',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '含税（元/m2）', '合价'],
            [['', '', '', '', '', '', '综合单价', ''], ['1', '0101', '挖土', 'C30', 'm2', '10', '20', '200']],
            [],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '不含税（元/m2）', '合价'],
            [['', '', '', '', '', '', '综合单价', ''], ['1', '0101', '挖土', 'C30', 'm2', '10', '20', '200']],
            [],
        ),
        (
            'ATK_I9_1i_tax_with_yuan_and_rmb_paren_swap',
            'D.3',
            ['序号', '汇总内容', '含税（元）（人民币）'],
            [['', '', '金额'], ['1', '土方', '100.00']],
            [],
            ['序号', '汇总内容', '不含税（元）（人民币）'],
            [['', '', '金额'], ['1', '土方', '100.00']],
            [],
        ),
        # 二阶复合“修饰词+单位”括号阻断
        (
            'I10_2nd_order_feiyong_hanshui_yuan_vs_buhanshui_yuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '费用（含税元）', ''],
            [['', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '费用（不含税元）', ''],
            [['', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
        ),
        (
            'I10_2nd_order_feiyong_hanshui_yuan_vs_buhanshui_wanyuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '费用（含税元）', ''],
            [['', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '费用（不含税万元）', ''],
            [['', '', '', '', '', '', '综合单价', '合价'], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['G1:H1'],
        ),
        # 非数值列纯单位/带括号纯单位、跨行拆括号单位冲突与恶意拼合阻断
        (
            'ATK_I9_3i_non_numeric_code_col_with_bare_yuan',
            'E.1',
            ['序号', '元', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '项目编码', '', '', '', '', '', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            [],
            ['序号', '元', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '项目编码', '', '', '', '', '', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            [],
        ),
        (
            'I10_non_numeric_code_col_with_paren_yuan',
            'E.1',
            ['序号', '（元）', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '项目编码', '', '', '', '', '', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            [],
            ['序号', '（元）', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '项目编码', '', '', '', '', '', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            [],
        ),
        (
            'I10_split_wanyuan_vs_single_yuan',
            'D.1',
            ['序号', '项目名称', '金额（'],
            [['', '', '万元）'], ['1', '工程A', '100.00']],
            [],
            ['序号', '项目名称', '金额（元）'],
            [['1', '工程A', '100.00']],
            [],
        ),
        (
            'I10_split_rmb_yuan_vs_rmb_wanyuan',
            'D.1',
            ['序号', '项目名称', '金额（人民币'],
            [['', '', '元）'], ['1', '工程A', '100.00']],
            [],
            ['序号', '项目名称', '金额（人民币'],
            [['', '', '万元）'], ['1', '工程A', '100.00']],
            [],
        ),
        (
            'NEW_V4_ATK_E1_split_colon_note_parens',
            'D.1',
            ['序号', '项目名称', '金额（注：'],
            [['', '', '不含规费）'], ['1', '工程A', '100.00']],
            [],
            ['序号', '项目名称', '金额（注：'],
            [['', '', '含规费）'], ['1', '工程A', '100.00']],
            [],
        ),
        (
            'ATK_E2_split_unknown_paren_note',
            'D.1',
            ['序号', '项目名称', '金额（'],
            [['', '', '含暂列）'], ['1', '工程A', '100.00']],
            [],
            ['序号', '项目名称', '金额（'],
            [['', '', '不含暂列）'], ['1', '工程A', '100.00']],
            [],
        ),
        (
            'I10_split_unclosed_paren_missing_right',
            'D.1',
            ['序号', '项目名称', '金额（'],
            [['', '', '元'], ['1', '工程A', '100.00']],
            [],
            ['序号', '项目名称', '金额（'],
            [['', '', '元'], ['1', '工程A', '100.00']],
            [],
        ),
        (
            'I10_split_nested_parens',
            'D.1',
            ['序号', '项目名称', '金额（'],
            [['', '', '（元））'], ['1', '工程A', '100.00']],
            [],
            ['序号', '项目名称', '金额（'],
            [['', '', '（元））'], ['1', '工程A', '100.00']],
            [],
        ),
        (
            'ATK_I9_3e_unrelated_split_alias_zangu_jia_above_amount',
            'D.1',
            ['序号', '项目名称', '暂估'],
            [['', '', '价'], ['', '', '金额'], ['1', '工程A', '100.00']],
            [],
            ['序号', '项目名称', '暂估'],
            [['', '', '价'], ['', '', '金额'], ['1', '工程A', '100.00']],
            [],
        ),
    ],
)
def test_iter10_outer_unit_modifier_split_paren_and_non_numeric_unit_attacks_block_pending(
    case_id, code, old_headers, old_rows, old_merges, new_headers, new_rows, new_merges
):
    # 括号内外单位/口径冲突、二阶复合括号、非数值列单位及恶意跨行拆括号变体必须全部阻断为 pending
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    if old_merges:
        old['sheets'][0]['merged_ranges'] = old_merges
    if new_merges:
        new['sheets'][0]['merged_ranges'] = new_merges
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'pending', (case_id, result['reports'][0])
    assert result['reports'][0]['issues'], case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, new_headers, new_rows, expected_delta',
    [
        # 合法跨行拆括号防误伤回归（含 2 行、3 行 D.1/E.1 及与单行完整括号表头等价对比）
        (
            'LEGIT_d1_2row_split_rmb_yuan',
            'D.1',
            ['序号', '项目名称', '金额（人民币'],
            [['', '', '元）'], ['1', '分部分项', '50.00']],
            ['序号', '项目名称', '金额（人民币'],
            [['', '', '元）'], ['1', '分部分项', '60.00']],
            '10.00',
        ),
        (
            'LEGIT_d1_3row_split_name_and_rmb_yuan',
            'D.1',
            ['序号', '项', '金额（人民币'],
            [['', '目', '元）'], ['', '名称', ''], ['1', '分部分项', '50.00']],
            ['序号', '项', '金额（人民币'],
            [['', '目', '元）'], ['', '名称', ''], ['1', '分部分项', '60.00']],
            '10.00',
        ),
        (
            'LEGIT_e1_3row_split_name_desc_and_rmb_yuan',
            'E.1',
            ['序号', '项目编码', '项', '项目特', '计量单位', '工程量', '综合单价', '金额（人民币'],
            [['', '', '目', '征', '', '', '', '元）'], ['', '', '名称', '描述', '', '', '', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '5.00', '50.00']],
            ['序号', '项目编码', '项', '项目特', '计量单位', '工程量', '综合单价', '金额（人民币'],
            [['', '', '目', '征', '', '', '', '元）'], ['', '', '名称', '描述', '', '', '', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '5.00', '60.00']],
            '10.00',
        ),
        (
            'LEGIT_d1_2row_split_open_paren_yuan',
            'D.1',
            ['序号', '项目名称', '金额（'],
            [['', '', '元）'], ['1', '分部分项', '50.00']],
            ['序号', '项目名称', '金额（'],
            [['', '', '元）'], ['1', '分部分项', '60.00']],
            '10.00',
        ),
        (
            'LEGIT_d1_2row_split_open_yuan_vs_single_line',
            'D.1',
            ['序号', '项目名称', '金额（'],
            [['', '', '元）'], ['1', '分部分项', '50.00']],
            ['序号', '项目名称', '金额（元）'],
            [['1', '分部分项', '60.00']],
            '10.00',
        ),
        (
            'LEGIT_d1_2row_split_rmb_yuan_vs_single_line',
            'D.1',
            ['序号', '项目名称', '金额（人民币'],
            [['', '', '元）'], ['1', '分部分项', '50.00']],
            ['序号', '项目名称', '金额（人民币元）'],
            [['1', '分部分项', '60.00']],
            '10.00',
        ),
        (
            'LEGIT_e1_2row_split_unit_price_compound_m2',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价（', '合价'],
            [['', '', '', '', '', '', '元/m2）', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价（元/m2）', '合价'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '30', '200']],
            '10',
        ),
        (
            'LEGIT_non_numeric_name_col_with_qizhong_scope_modifier',
            'D.1',
            ['序号', '其中', '金额（元）'],
            [['', '项目名称', ''], ['1', '分部分项', '50.00']],
            ['序号', '其中', '金额（元）'],
            [['', '项目名称', ''], ['1', '分部分项', '60.00']],
            '10.00',
        ),
        (
            'LEGIT_full_alias_dinge_rengongfei_with_paren_unit_not_split',
            'D.3',
            ['序号', '汇总内容', '金额（元）', '定额人工费（元）'],
            [['1', '土方', '100.00', '30.00']],
            ['序号', '汇总内容', '金额（元）', '定额人工费（元）'],
            [['1', '土方', '100.00', '45.00']],
            '15.00',
        ),
    ],
)
def test_iter10_legitimate_split_paren_headers_remain_comparable(
    case_id, code, old_headers, old_rows, new_headers, new_rows, expected_delta
):
    # 合法跨行拆括号表头（含与单行完整括号表头对照及复合单位闭合）必须正常检出差异且零 issues
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'changed', (case_id, result['reports'][0]['issues'])
    assert result['reports'][0]['issues'] == [], case_id
    assert any(d['delta'] == expected_delta for d in result['diffs']), case_id


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, new_headers, new_rows',
    [
        # 漏洞类别 A：含单位别名叠加第二后缀单位（单格双单位吞并）必须全部阻断为 pending
        (
            'I11_A_jine_yuan_wanyuan_vs_paren_wanyuan',
            'D.3',
            ['序号', '汇总内容', '金额元万元'],
            [['1', '土方', '100']],
            ['序号', '汇总内容', '金额（万元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_A_jine_yuan_wanyuan_both_sides',
            'D.3',
            ['序号', '汇总内容', '金额元万元'],
            [['1', '土方', '100']],
            ['序号', '汇总内容', '金额元万元'],
            [['1', '土方', '100']],
        ),
        (
            'I11_A_jine_yuan_wanyuan_paren_wanyuan',
            'D.3',
            ['序号', '汇总内容', '金额元万元（万元）'],
            [['1', '土方', '100']],
            ['序号', '汇总内容', '金额（万元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_A_unit_price_yuan_wanyuan_vs_wanyuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价元万元', '合价'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价（万元）', '合价'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
        ),
        (
            'I11_A_hejia_yuan_yiyuan_vs_yiyuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价元亿元'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价（亿元）'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
        ),
        (
            'I11_A_jine_yuan_meiyuan_vs_meiyuan',
            'D.3',
            ['序号', '汇总内容', '金额元美元'],
            [['1', '土方', '100']],
            ['序号', '汇总内容', '金额（美元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_A_jine_yuan_rmbyuan_vs_rmbyuan',
            'D.1',
            ['序号', '项目名称', '金额元人民币元'],
            [['1', '土方', '100']],
            ['序号', '项目名称', '金额（人民币元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_A_jine_yuan_percent_vs_percent',
            'D.3',
            ['序号', '汇总内容', '金额元%'],
            [['1', '土方', '100']],
            ['序号', '汇总内容', '金额%'],
            [['1', '土方', '100']],
        ),
        (
            'I11_A_rate_percent_yuan_vs_yuan',
            'D.3',
            ['序号', '汇总内容', '费率%元', '金额'],
            [['1', '土方', '5', '100']],
            ['序号', '汇总内容', '费率（元）', '金额'],
            [['1', '土方', '5', '100']],
        ),
        (
            'I11_A_zangujia_yuan_wanyuan',
            'D.3',
            ['序号', '汇总内容', '暂估价元万元', '金额'],
            [['1', '土方', '5', '100']],
            ['序号', '汇总内容', '暂估价（万元）', '金额'],
            [['1', '土方', '5', '100']],
        ),
        (
            'I11_A_shuijin_yuan_wanyuan',
            'D.3',
            ['序号', '汇总内容', '税金元万元', '金额'],
            [['1', '土方', '5', '100']],
            ['序号', '汇总内容', '税金（万元）', '金额'],
            [['1', '土方', '5', '100']],
        ),
        (
            'I11_A_jine_yuan_paren_wanyuan_yuan',
            'D.3',
            ['序号', '汇总内容', '金额元（万元）元'],
            [['1', '土方', '100']],
            ['序号', '汇总内容', '金额（万元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_A_hanshui_yuan_wanyuan_mod',
            'D.3',
            ['序号', '汇总内容', '含税元万元'],
            [['', '', '金额'], ['1', '土方', '100']],
            ['序号', '汇总内容', '含税（万元）'],
            [['', '', '金额'], ['1', '土方', '100']],
        ),
        # 漏洞类别 B：跨行非括号单位拆字截断必须精确采集整段或冲突单位并阻断为 pending
        (
            'I11_B_split_jine_wan_plus_yuan_vs_yuan',
            'D.3',
            ['序号', '汇总内容', '金额万'],
            [['', '', '元'], ['1', '土方', '100']],
            ['序号', '汇总内容', '金额（元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_B_split_jine_qian_plus_yuan_vs_yuan',
            'D.1',
            ['序号', '项目名称', '金额千'],
            [['', '', '元'], ['1', '土方', '100']],
            ['序号', '项目名称', '金额（元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_B_split_jine_yi_plus_yuan_vs_yuan',
            'D.3',
            ['序号', '汇总内容', '金额亿'],
            [['', '', '元'], ['1', '土方', '100']],
            ['序号', '汇总内容', '金额（元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_B_split_unit_price_wan_plus_yuan_vs_yuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价万', '合价'],
            [['', '', '', '', '', '', '元', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价（元）', '合价'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
        ),
        (
            'I11_B_split_jine_yuan_wan_plus_yuan_vs_yuan',
            'D.3',
            ['序号', '汇总内容', '金额元万'],
            [['', '', '元'], ['1', '土方', '100']],
            ['序号', '汇总内容', '金额（元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_B_split_jine_paren_yuan_wan_plus_yuan_vs_yuan',
            'D.3',
            ['序号', '汇总内容', '金额（元）万'],
            [['', '', '元'], ['1', '土方', '100']],
            ['序号', '汇总内容', '金额（元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_B_split_meiyuan_vs_yuan',
            'D.3',
            ['序号', '汇总内容', '金额美'],
            [['', '', '元'], ['1', '土方', '100']],
            ['序号', '汇总内容', '金额（元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_B_split_3row_bracket_yuan_wan_yuan',
            'D.3',
            ['序号', '汇总内容', '金额（'],
            [['', '', '元）万'], ['', '', '元'], ['1', '土方', '100']],
            ['序号', '汇总内容', '金额（元）'],
            [['1', '土方', '100']],
        ),
        (
            'I11_B_split_3row_zonghe_danjia_paren_yuan_wan_yuan',
            'E.1',
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合', '合价'],
            [['', '', '', '', '', '', '单价（元）万', ''], ['', '', '', '', '', '', '元', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['序号', '项目编码', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价（元）', '合价'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
        ),
        (
            'I11_B_split_rmb_yuan_vs_yuan',
            'D.3',
            ['序号', '汇总内容', '金额人民币'],
            [['', '', '元'], ['1', '土方', '100']],
            ['序号', '汇总内容', '金额（元）'],
            [['1', '土方', '100']],
        ),
        # 非数值列带括号计量单位（m2）与裸 m2 口径统一：均阻断为 pending
        (
            'I11_non_numeric_code_paren_m2',
            'E.1',
            ['序号', '项目编码（m2）', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['序号', '项目编码（m2）', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
        ),
        (
            'I11_non_numeric_code_split_paren_m2',
            'E.1',
            ['序号', '项目编码（', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', 'm2）', '', '', '', '', '', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['序号', '项目编码（', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', 'm2）', '', '', '', '', '', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
        ),
        (
            'I11_non_numeric_code_prev_paren_m2',
            'E.1',
            ['序号', '（m2）', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '项目编码', '', '', '', '', '', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
            ['序号', '（m2）', '项目名称', '项目特征描述', '计量单位', '工程量', '综合单价', '合价'],
            [['', '项目编码', '', '', '', '', '', ''], ['1', '0101', '挖土', 'C30', 'm3', '10', '20', '200']],
        ),
    ],
)
def test_iter11_dual_suffix_units_split_char_units_and_non_numeric_measure_units_block_pending(
    case_id, code, old_headers, old_rows, new_headers, new_rows
):
    # 单格双后缀单位、跨行非括号单位拆字及非数值列带括号计量单位均必须阻断为 pending
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'pending', (case_id, result['reports'][0])
    assert result['reports'][0]['issues'], case_id
    assert result['diffs'][0]['kind'] == 'business_issue', case_id


@pytest.mark.parametrize(
    'case_id, code, old_headers, old_rows, new_headers, new_rows, expected_delta',
    [
        # 合法跨行非括号单位拆字与单行括号单位对称等价对照，以及人民币元/同单位防误拆对照
        (
            'I11_LEGIT_split_wan_yuan_vs_paren_wanyuan',
            'D.3',
            ['序号', '汇总内容', '金额万'],
            [['', '', '元'], ['1', '土方', '100']],
            ['序号', '汇总内容', '金额（万元）'],
            [['1', '土方', '150']],
            '50',
        ),
        (
            'I11_LEGIT_split_qian_yuan_vs_paren_qianyuan',
            'D.1',
            ['序号', '项目名称', '金额千'],
            [['', '', '元'], ['1', '土方', '100']],
            ['序号', '项目名称', '金额（千元）'],
            [['1', '土方', '150']],
            '50',
        ),
        (
            'I11_LEGIT_split_mei_yuan_vs_paren_meiyuan',
            'D.3',
            ['序号', '汇总内容', '金额美'],
            [['', '', '元'], ['1', '土方', '100']],
            ['序号', '汇总内容', '金额（美元）'],
            [['1', '土方', '150']],
            '50',
        ),
        (
            'I11_LEGIT_split_rmb_yuan_vs_paren_rmbyuan',
            'D.3',
            ['序号', '汇总内容', '金额人民币'],
            [['', '', '元'], ['1', '土方', '100']],
            ['序号', '汇总内容', '金额（人民币元）'],
            [['1', '土方', '150']],
            '50',
        ),
        (
            'I11_LEGIT_single_jine_rmb_yuan_vs_paren_rmbyuan',
            'D.3',
            ['序号', '汇总内容', '金额人民币元'],
            [['1', '土方', '100']],
            ['序号', '汇总内容', '金额（人民币元）'],
            [['1', '土方', '150']],
            '50',
        ),
        (
            'I11_LEGIT_jine_yuan_paren_yuan_vs_paren_yuan',
            'D.3',
            ['序号', '汇总内容', '金额元（元）'],
            [['1', '土方', '100']],
            ['序号', '汇总内容', '金额（元）'],
            [['1', '土方', '150']],
            '50',
        ),
    ],
)
def test_iter11_legitimate_split_char_units_and_rmb_yuan_remain_comparable(
    case_id, code, old_headers, old_rows, new_headers, new_rows, expected_delta
):
    # 合法跨行非括号单位拆字（如['金额万','元'] vs ['金额（万元）']）与人民币元整体识别必须正常比较且零 issues
    old = make_book(old_rows, headers=old_headers)
    new = make_book(new_rows, headers=new_headers)
    result = compare_cost_reports(old, new, inventory(code))
    assert result['reports'][0]['status'] == 'changed', (case_id, result['reports'][0]['issues'])
    assert result['reports'][0]['issues'] == [], case_id
    assert any(d['delta'] == expected_delta for d in result['diffs']), case_id
