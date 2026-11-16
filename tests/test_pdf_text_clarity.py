from test_pdf_appearance import changes, compare, make_book, title


def test_distant_amount_marks_pair_without_changing_unchanged_labels(tmp_path):
    def paint(new):
        def draw(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 12)
            for label, y in [('Subtotal', 300), ('Total', 250)]:
                pdf.drawString(100, y, label)
                pdf.drawString(500, y - 9 if new else y, '—' if new else '-')
        return draw
    old = make_book(tmp_path, 'separate-marks-old', [paint(False)])
    new = make_book(tmp_path, 'separate-marks-new', [paint(True)])
    content = changes(compare(old, new), 'content')
    assert len(content) == 2
    assert [(d['old_value'], d['new_value']) for d in content] == [('-', '—'), ('-', '—')]
    for diff in content:
        for side in ('old', 'new'):
            box = diff[f'{side}_source']['bbox']
            assert box[0] > .8 and box[2] - box[0] < .03
    assert content[0]['old_source']['bbox'][1] < content[1]['old_source']['bbox'][1]
    assert content[0]['new_source']['bbox'][1] < content[1]['new_source']['bbox'][1]


def test_independent_header_fields_do_not_share_change_box(tmp_path):
    def paint(new):
        def draw(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 12)
            pdf.drawString(40, 680, 'Project: Beta' if new else 'Project: Alpha')
            pdf.drawString(450, 680, 'Page 1')
        return draw
    old = make_book(tmp_path, 'separate-fields-old', [paint(False)])
    new = make_book(tmp_path, 'separate-fields-new', [paint(True)])
    content = changes(compare(old, new), 'content')
    assert len(content) == 1
    assert content[0]['old_value'] == 'Project: Alpha'
    assert content[0]['new_value'] == 'Project: Beta'
    assert all(content[0][f'{side}_source']['bbox'][2] < .4 for side in ('old', 'new'))


def test_actual_amount_change_in_another_column_is_retained(tmp_path):
    def paint(new):
        def draw(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 12)
            pdf.drawString(40, 680, 'Total')
            pdf.drawString(450, 680, '120' if new else '100')
        return draw
    old = make_book(tmp_path, 'amount-column-old', [paint(False)])
    new = make_book(tmp_path, 'amount-column-new', [paint(True)])
    content = changes(compare(old, new), 'content')
    assert [(d['old_value'], d['new_value']) for d in content] == [('100', '120')]
    assert all(content[0][f'{side}_source']['bbox'][0] > .7 for side in ('old', 'new'))


def test_row_baseline_offset_alone_does_not_change_content(tmp_path):
    def paint(new):
        def draw(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 12)
            pdf.drawString(40, 680, 'Total')
            pdf.drawString(450, 671 if new else 680, '-')
        return draw
    old = make_book(tmp_path, 'baseline-only-old', [paint(False)])
    new = make_book(tmp_path, 'baseline-only-new', [paint(True)])
    assert not changes(compare(old, new), 'content')


def test_reflowed_page_counter_does_not_absorb_project_field_or_unchanged_words(tmp_path):
    from scripts.make_pdf_examples import register_font
    font, _ = register_font(None)

    def paint(new):
        def draw(pdf):
            pdf.setFont(font, 10)
            pdf.drawString(40, 680, '单位工程名称：' if new else '单位工程名称：房屋建筑与装饰工程')
            if new:
                pdf.drawString(450, 680, '第页')
                pdf.drawString(450, 665, '共页')
            else:
                pdf.drawString(450, 680, '第1页 共1页')
        return draw
    old = make_book(tmp_path, 'header-reflow-old', [paint(False)])
    new = make_book(tmp_path, 'header-reflow-new', [paint(True)])
    content = changes(compare(old, new), 'content')
    project = [d for d in content if '单位工程' in (d['old_value'] or '')]
    assert len(project) == 1
    assert project[0]['old_value'] == '单位工程名称：房屋建筑与装饰工程'
    assert project[0]['new_value'] == '单位工程名称：'
    assert project[0]['old_source']['bbox'][2] < .5
    counters = [d for d in content if d not in project]
    assert [(d['old_value'], d['new_value']) for d in counters] == [('1', None), ('1', None)]
    assert all(d['old_source']['bbox'][0] > .7 for d in counters)
