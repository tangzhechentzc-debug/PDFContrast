import pytest

from test_pdf_appearance import changes, compare, make_book, title


def test_fixed_row_labels_keep_swapped_amounts_as_two_changes(tmp_path):
    def draw(values):
        def paint(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 12)
            for y, label, value in zip((650, 600), ('Subtotal', 'Tax'), values):
                pdf.drawString(40, y, label)
                pdf.drawString(450, y, value)
        return paint
    old = make_book(tmp_path, 'swap-old', [draw(('100', '200'))])
    new = make_book(tmp_path, 'swap-new', [draw(('200', '100'))])
    result = changes(compare(old, new), 'content')
    assert [(d['old_value'], d['new_value']) for d in result] == [('100', '200'), ('200', '100')]
    assert all(d['measurements']['old']['field_context'] == d['measurements']['new']['field_context'] for d in result)


def test_amount_cannot_match_equal_string_under_another_row_label(tmp_path):
    def draw(values):
        def paint(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 12)
            for y, label, value in zip((650, 600), ('Subtotal', 'Tax'), values):
                pdf.drawString(40, y, label)
                pdf.drawString(450, y, value)
        return paint
    old = make_book(tmp_path, 'near-old', [draw(('100', '101'))])
    new = make_book(tmp_path, 'near-new', [draw(('101', '102'))])
    assert [(d['old_value'], d['new_value']) for d in changes(compare(old, new), 'content')] == [('100', '101'), ('101', '102')]


def test_reflowed_paragraph_reports_only_modified_word(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 12)
            pdf.drawString(40, 680, 'Alpha BETA' if new else 'Alpha beta gamma delta')
            if new:
                pdf.drawString(40, 660, 'gamma delta')
        return paint
    old = make_book(tmp_path, 'reflow-old', [draw(False)])
    new = make_book(tmp_path, 'reflow-new', [draw(True)])
    result = changes(compare(old, new), 'content')
    assert [(d['old_value'], d['new_value']) for d in result] == [('beta', 'BETA')]
    assert all(d[f'{side}_source']['bbox'][2]-d[f'{side}_source']['bbox'][0] < .1 for d in result for side in ('old','new'))


def test_cell_boundaries_keep_adjacent_wrapped_headers_separate(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 10)
            for x in (40, 110, 180, 250):
                pdf.line(x, 600, x, 680)
            for y in (600, 640, 680):
                pdf.line(40, y, 250, y)
            pdf.drawString(45, 658, 'Code')
            pdf.drawString(115, 666 if new else 658, 'Material' if new else 'Material name')
            if new:
                pdf.drawString(115, 652, 'name')
            pdf.drawString(185, 658, 'Quantity')
        return paint
    old = make_book(tmp_path, 'headers-old', [draw(False)])
    new = make_book(tmp_path, 'headers-new', [draw(True)])
    result = compare(old, new)
    assert not changes(result, 'content')
    whitespace = [d for d in result['diffs'] if d['kind'] == 'text_whitespace_changed' and d.get('measurements')]
    assert whitespace and whitespace[0]['measurements']['old']['cell']['column'] == 2


def test_table_row_key_precedes_low_similarity_and_does_not_pick_last_row(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 10)
            for x in (40, 110, 350):
                pdf.line(x, 560, x, 680)
            for y in (560, 590, 620, 650, 680):
                pdf.line(40, y, 350, y)
            pdf.drawString(45, 660, 'No.')
            pdf.drawString(115, 660, 'Project')
            for i in range(3 if new else 1):
                pdf.drawString(45, 630-i*30, str(i+1))
                pdf.drawString(115, 630-i*30, f'Project {i+1}' if new else 'Original building')
        return paint
    old = make_book(tmp_path, 'rowkey-old', [draw(False)])
    new = make_book(tmp_path, 'rowkey-new', [draw(True)])
    result = changes(compare(old, new), 'content')
    changed = next(d for d in result if d['old_value'] == 'Original building')
    assert changed['new_value'] == 'Project 1'
    assert changed['measurements']['old']['row_key'] == changed['measurements']['new']['row_key'] == '1'


def test_similar_work_description_cannot_borrow_project_name_column(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 10)
            for x in (40, 100, 240, 480):
                pdf.line(x, 600, x, 680)
            for y in (600, 640, 680):
                pdf.line(40, y, 480, y)
            for x, text in [(45,'No.'),(105,'Name'),(245,'Work description'),(45,'1')]:
                pdf.drawString(x, 620 if text == '1' else 660, text)
            pdf.drawString(105, 620, 'Safety measures')
            if not new:
                pdf.drawString(245, 620, 'All safety measures required on site')
        return paint
    old = make_book(tmp_path, 'columns-old', [draw(False)])
    new = make_book(tmp_path, 'columns-new', [draw(True)])
    result = changes(compare(old, new), 'content')
    assert [(d['old_value'],d['new_value']) for d in result] == [('All safety measures required on site',None)]
    assert result[0]['old_source']['bbox'][0] > .4
    assert result[0]['new_source']['location_precision'] == 'page'


def test_note_continuing_on_added_page_is_not_reported_as_removed(tmp_path):
    def first(pdf, new=False):
        title(pdf)
        pdf.setFont('Helvetica', 12)
        for i in range(6):
            pdf.drawString(40, 680-i*30, f'Fixed content line {i} remains unchanged')
        pdf.drawString(40, 400, '1. First note stays here.')
        if not new:
            pdf.drawString(40, 370, '2. Manufacturer instructions remain applicable.')
    def second(pdf):
        pdf.setFont('Helvetica', 12)
        pdf.drawString(40, 700, '3. Manufacturer instructions remain applicable.')
    old = make_book(tmp_path, 'continued-old', [first])
    new = make_book(tmp_path, 'continued-new', [lambda p:first(p,True),second])
    result = changes(compare(old, new),'content')
    note = next(d for d in result if 'Manufacturer' in (d['old_value'] or ''))
    assert note['new_value'] == '3. Manufacturer instructions remain applicable.'
    assert note['old_source']['page'] == 1 and note['new_source']['page'] == 2
    assert not any(d['new_value'] is None and 'Manufacturer' in (d['old_value'] or '') for d in result)


def test_unmatched_new_note_is_unverified_when_old_text_has_bad_unicode(tmp_path):
    import copy
    def paint(pdf):
        title(pdf)
        pdf.setFont('Helvetica', 12)
        pdf.drawString(40, 300, 'Original note')
    old=make_book(tmp_path,'bad-old',[paint])
    new=copy.deepcopy(old)
    for char in old['pages'][0]['appearance']['chars']:
        if char['bbox'][1] > .5:
            char.update(text='(cid:0)',bad_unicode=True,reliable=False)
    for char in new['pages'][0]['appearance']['chars']:
        if char['bbox'][1] > .5:
            char['text']='X'
    result=compare(old,new)
    additions=[d for d in result['diffs'] if d['kind']=='text_changed' and d['old_value'] is None]
    assert additions and all(d['severity']=='issue' and d['old_source']['location_precision']=='page' for d in additions)


def test_bad_footer_does_not_hide_reliable_new_table_row(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 10)
            for x in (40, 110, 350):
                pdf.line(x, 560, x, 680)
            for y in (560, 600, 640, 680):
                pdf.line(40, y, 350, y)
            pdf.drawString(45, 660, 'No.')
            pdf.drawString(115, 660, 'Project')
            pdf.drawString(45, 620, '1')
            pdf.drawString(115, 620, 'Stable project')
            if new:
                pdf.drawString(45, 580, '2')
                pdf.drawString(115, 580, 'New project')
            pdf.drawString(40, 100, 'Footnote')
        return paint
    old=make_book(tmp_path,'footer-old',[draw(False)])
    new=make_book(tmp_path,'footer-new',[draw(True)])
    for char in old['pages'][0]['appearance']['chars']:
        if char['bbox'][1] > .8:
            char.update(text='(cid:0)',bad_unicode=True,reliable=False)
    result=compare(old,new)
    addition=next(d for d in result['diffs'] if d['new_value']=='New project')
    assert addition['severity']=='change'
    assert addition['measurements']['new']['cell']['row']==3
    assert any(d['severity']=='issue' for d in result['diffs'])


def test_identical_multpage_table_headers_do_not_become_additions_and_removals(tmp_path):
    def draw(number):
        def paint(pdf):
            title(pdf)
            pdf.setFont('Helvetica', 10)
            pdf.drawString(40, 720, f'Page {number}')
            for x in (40, 110, 350):
                pdf.line(x, 600, x, 680)
            for y in (600, 640, 680):
                pdf.line(40, y, 350, y)
            pdf.drawString(45, 660, 'ID')
            pdf.drawString(115, 660, 'Name')
        return paint
    book=make_book(tmp_path,'repeated-headers',[draw(1),draw(2)])
    result=compare(book,book)
    assert len(result['page_pairs'])==2 and all(p['status']=='confirmed' for p in result['page_pairs'])
    assert not changes(result)


def test_stable_project_name_anchors_row_when_printed_sequence_changes(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf);pdf.setFont('Helvetica',10)
            for x in (40,100,350):pdf.line(x,580,x,680)
            for y in (580,620,650,680):pdf.line(40,y,350,y)
            pdf.drawString(45,660,'No.');pdf.drawString(105,660,'Item')
            pdf.drawString(45,630,'3.1' if new else '4.1');pdf.drawString(105,630,'Provisional sum')
        return paint
    old=make_book(tmp_path,'renumber-old',[draw(False)]);new=make_book(tmp_path,'renumber-new',[draw(True)])
    result=changes(compare(old,new),'content')
    assert [(d['old_value'],d['new_value']) for d in result]==[('4.1','3.1')]


def test_merged_total_does_not_inherit_unrelated_column_heading(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf);pdf.setFont('Helvetica',10)
            for x in (40,320):pdf.line(x,580,x,680)
            for y in (580,610,640,680):pdf.line(40,y,320,y)
            middle=200 if new else 140
            pdf.line(middle,610,middle,680)
            pdf.drawString(50,660,'Name');pdf.drawString(middle+5,660,'Amount')
            pdf.drawString(150,590,'Total')
        return paint
    old=make_book(tmp_path,'total-old',[draw(False)]);new=make_book(tmp_path,'total-new',[draw(True)])
    assert not changes(compare(old,new),'content')


def test_centered_subtitle_stays_independent_when_indent_changes(tmp_path):
    def draw(new):
        def paint(pdf):
            pdf.setFont('Helvetica',12)
            pdf.drawString(100,700,'A.9.2 Price adjustment report')
            pdf.drawString(180 if new else 110,680,'(Price index method)')
        return paint
    old=make_book(tmp_path,'subtitle-old',[draw(False)]);new=make_book(tmp_path,'subtitle-new',[draw(True)])
    assert not changes(compare(old,new),'content')


def test_wrapped_signature_closing_bracket_does_not_absorb_next_label(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf);pdf.setFont('Helvetica',12)
            pdf.drawString(40,650,'Prepared by:');pdf.drawString(300,650,'(sign' if new else '(signature)')
            if new:
                pdf.drawString(40,630,'ature)Representative')
            else:
                pdf.drawString(40,630,'Representative')
        return paint
    old=make_book(tmp_path,'signature-old',[draw(False)]);new=make_book(tmp_path,'signature-new',[draw(True)])
    result=compare(old,new)
    assert not changes(result,'content')
    assert any(d['kind']=='text_whitespace_changed' for d in result['diffs'])


def test_page_counter_last_glyph_over_table_border_does_not_join_header(tmp_path):
    from scripts.make_pdf_examples import register_font
    font,_=register_font(None)
    def draw(new):
        def paint(pdf):
            pdf.setFont(font,10);pdf.drawString(40,750,'A.1 比较报表')
            for x in (40,350,550):pdf.line(x,580,x,680)
            for y in (580,630,680):pdf.line(40,y,550,y)
            pdf.drawString(60,650,'名称');pdf.drawString(410,650,'备注')
            pdf.drawString(450,705,'第页共' if new else '第1页共1页')
            if new:pdf.drawString(470,680,'页')
        return paint
    old=make_book(tmp_path,'border-counter-old',[draw(False)]);new=make_book(tmp_path,'border-counter-new',[draw(True)])
    result=changes(compare(old,new),'content')
    assert [(d['old_value'],d['new_value']) for d in result]==[('1',None),('1',None)]
    assert all('备注' not in (d['old_value'] or '')+(d['new_value'] or '') for d in result)


def test_title_tail_before_field_is_joined_only_with_complete_other_title_evidence(tmp_path):
    from scripts.make_pdf_examples import register_font
    font,_=register_font(None)
    def draw(new):
        def paint(pdf):
            pdf.setFont(font,10)
            pdf.drawString(200,700,'C.5.1项目汇总' if new else 'C.5.1项目汇总表')
            pdf.drawString(40,680,'表工程名称:' if new else '工程名称:')
        return paint
    old=make_book(tmp_path,'tail-old',[draw(False)]);new=make_book(tmp_path,'tail-new',[draw(True)])
    result=compare(old,new)
    assert not changes(result,'content')
    title_changes=[d for d in result['diffs'] if d['kind'] in ('text_whitespace_changed','line_break_changed') and 'C.5.1' in d.get('measurements',{}).get('new',{}).get('fragment_text','')]
    assert title_changes
    for diff in title_changes:
        source=diff['new_source']
        boxes=source['highlight_boxes']
        assert len(boxes)==2
        assert source['bbox'][0]<.1 and source['bbox'][2]>.4
        assert boxes[1][2]<.09
        assert all(not (box[0]<=.15<=box[2] and box[1]<=.145<=box[3]) for box in boxes)


def test_cell_header_overlapping_grid_edge_uses_complete_counterpart_evidence(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf);pdf.setFont('Helvetica',10)
            for x in (40,130,260):pdf.line(x,580,x,700)
            for y in (580,620,660,700):pdf.line(40,y,260,y)
            pdf.drawString(45,680,'Code');pdf.drawString(140,680,'Rate')
            pdf.drawString(140,640 if new else 660,'Exempt')
            pdf.drawString(140,625 if new else 640,'Unit')
        return paint
    old=make_book(tmp_path,'edge-old',[draw(False)]);new=make_book(tmp_path,'edge-new',[draw(True)])
    assert not changes(compare(old,new),'content')


def test_unreadable_row_anchor_downgrades_both_sides_of_content_change(tmp_path):
    def draw(value):
        def paint(pdf):
            title(pdf);pdf.setFont('Helvetica',10)
            for x in (40,130,350):pdf.line(x,580,x,680)
            for y in (580,630,680):pdf.line(40,y,350,y)
            pdf.drawString(45,650,'Code');pdf.drawString(140,650,'Project')
            pdf.drawString(45,600,'1');pdf.drawString(140,600,value)
        return paint
    old=make_book(tmp_path,'bad-anchor-old',[draw('Original project')])
    new=make_book(tmp_path,'bad-anchor-new',[draw('Replacement project')])
    old['pages'][0]['appearance']['tables'][0]['text'][1][0]='(cid:0)'
    result=compare(old,new)
    affected=[d for d in result['diffs'] if d['kind']=='text_changed' and any('project' in (d[side+'_value'] or '') for side in ('old','new'))]
    assert affected and all(d['severity']=='issue' for d in affected)


def test_table_whitespace_and_wrap_evidence_stays_in_each_cell(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf);pdf.setFont('Helvetica',10)
            for x in (40,130,350):pdf.line(x,560,x,680)
            for y in (560,620,680):pdf.line(40,y,350,y)
            pdf.drawString(45,650,'No.');pdf.drawString(140,650,'Project')
            pdf.drawString(45,590,'1  2' if new else '1 2')
            pdf.drawString(140,600,'Alpha' if new else 'Alpha Beta')
            if new:pdf.drawString(140,580,'Beta')
        return paint
    old=make_book(tmp_path,'cell-format-old',[draw(False)])
    new=make_book(tmp_path,'cell-format-new',[draw(True)])
    result=compare(old,new)
    assert not changes(result,'content')
    formatting=[d for d in result['diffs'] if d['kind'] in ('text_whitespace_changed','line_break_changed','character_spacing_changed')]
    assert formatting
    for diff in formatting:
        for side in ('old','new'):
            cell=diff['measurements'][side]['cell']
            assert cell
            box=diff[side+'_source']['bbox']
            assert cell['bbox'][0]<=box[0]<box[2]<=cell['bbox'][2]
    wrap=next(d for d in formatting if d['kind']=='line_break_changed')
    assert len(wrap['new_source']['highlight_boxes'])==2


def test_unreliable_matching_text_does_not_claim_character_format_changes(tmp_path):
    old=make_book(tmp_path,'unreliable-format-old',[lambda pdf:(title(pdf),pdf.drawString(40,500,'Original note'))])
    new=make_book(tmp_path,'unreliable-format-new',[lambda pdf:(title(pdf),pdf.drawString(40,500,'Original note'))])
    for char in old['pages'][0]['appearance']['chars']:
        if char['bbox'][1]>.3:
            char['reliable']=False
    for char in new['pages'][0]['appearance']['chars']:
        if char['bbox'][1]>.3:
            char['font']='OtherFont';char['size_pt']=18
    result=compare(old,new)
    assert not changes(result,'format')
    assert any(d['kind']=='attribute_unverified' and d['severity']=='issue' for d in result['diffs'])


@pytest.mark.parametrize('cross_page',[False,True])
def test_numbered_cjk_note_with_literal_spaces_remains_independent(tmp_path,cross_page):
    from scripts.make_pdf_examples import register_font
    font,_=register_font(None)
    body='有厂家特别说明要求的，可在备注栏列明。'
    def first(pdf,new=False):
        title(pdf);pdf.setFont(font,10)
        pdf.drawString(40,400,'注：1 相同项目应分别列项'+(';' if new else '；'))
        if not new or not cross_page:
            pdf.drawString(40,350 if new else 380,('3 ' if new else '2   ')+body)
    def continuation(pdf):
        pdf.setFont(font,10);pdf.drawString(40,700,'3 '+body)
    old=make_book(tmp_path,'numbered-note-old',[first])
    new=make_book(tmp_path,'numbered-note-new',[lambda pdf:first(pdf,True)]+([continuation] if cross_page else []))
    content=changes(compare(old,new),'content')
    note=next(d for d in content if body in (d['old_value'] or ''))
    assert note['old_value']=='2   '+body and note['new_value']=='3 '+body
    assert note['new_source']['page']==(2 if cross_page else 1)
    assert not any(body in (d['new_value'] or '') and d['old_value'] is None for d in content)


def test_moved_table_border_styles_use_same_outer_edge_role(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf);pdf.setFont('Helvetica',10)
            x0,y0,x1,y1=(100,450,500,680) if new else (40,500,500,700)
            pdf.setLineWidth(2 if new else 1);pdf.rect(x0,y0,x1-x0,y1-y0,stroke=1,fill=0)
            pdf.line(x0,y1-40,x1,y1-40);pdf.drawString(x0+5,y1-20,'Project')
        return paint
    old=make_book(tmp_path,'outer-border-old',[draw(False)])
    new=make_book(tmp_path,'outer-border-new',[draw(True)])
    diff=next(d for d in compare(old,new)['diffs'] if d['kind']=='border_style_changed')
    assert diff['severity']=='change' and diff['measurements']['boundary_role']=='top'
    assert diff['old_source']['bbox'][1]==pytest.approx(100/800-.001)
    assert diff['new_source']['bbox'][1]==pytest.approx(120/800-.001)
    assert '唯一闭合外框' in diff['measurements']['match_basis']


def test_internal_border_can_follow_unique_cell_edge_when_row_height_changes(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf);pdf.setFont('Helvetica',10)
            pdf.setLineWidth(1);pdf.rect(40,500,460,200,stroke=1,fill=0)
            pdf.setLineWidth(4 if new else 1);pdf.line(40,620 if new else 650,500,620 if new else 650)
            pdf.drawString(50,675,'Header');pdf.drawString(50,550,'Unique item')
        return paint
    old=make_book(tmp_path,'cell-border-old',[draw(False)])
    new=make_book(tmp_path,'cell-border-new',[draw(True)])
    diff=next(d for d in compare(old,new)['diffs'] if d['kind']=='border_style_changed')
    assert diff['severity']=='change'
    assert diff['measurements']['boundary_role']=='bottom'
    assert diff['measurements']['context']['old_text']==diff['measurements']['context']['new_text']=='Header'
    assert diff['old_source']['bbox'][1]==pytest.approx(150/800-.001)
    assert diff['new_source']['bbox'][1]==pytest.approx(180/800-.001)


def test_unlabelled_internal_lines_are_not_paired_by_nearest_position(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf);pdf.setLineWidth(1);pdf.rect(40,500,460,200,stroke=1,fill=0)
            pdf.setLineWidth(4 if new else 1);pdf.line(40,620 if new else 650,500,620 if new else 650)
        return paint
    old=make_book(tmp_path,'unknown-border-old',[draw(False)])
    new=make_book(tmp_path,'unknown-border-new',[draw(True)])
    diffs=compare(old,new)['diffs']
    assert not any(d['kind']=='border_style_changed' for d in diffs)
    issue=next(d for d in diffs if d['kind']=='border_correspondence_unverified')
    assert issue['severity']=='issue'
    assert issue['old_source']['location_precision']==issue['new_source']['location_precision']=='corresponding_region'


@pytest.mark.parametrize('before,after',[('金额（元）','金额(元)'),('第1页共1页','第页共页')])
def test_character_spacing_does_not_bridge_unmatched_glyphs(tmp_path,before,after):
    from scripts.make_pdf_examples import register_font
    font,_=register_font(None)
    def draw(text):
        def paint(pdf):
            title(pdf);pdf.setFont(font,12);pdf.drawString(40,500,text)
        return paint
    old=make_book(tmp_path,'skip-spacing-old',[draw(before)])
    new=make_book(tmp_path,'skip-spacing-new',[draw(after)])
    result=compare(old,new)
    assert changes(result,'content')
    assert not any(d['kind']=='character_spacing_changed' for d in result['diffs'])


@pytest.mark.parametrize('overhang',[-.36,.36])
def test_frame_size_uses_vertical_axes_not_horizontal_endpoints(tmp_path,overhang):
    def paint(pdf):
        title(pdf);pdf.setLineWidth(.72)
        pdf.line(100,500,100,700);pdf.line(400,500,400,700)
        pdf.line(100-overhang,700,400+overhang,700)
        pdf.line(100+overhang,500,400-overhang,500)
    book=make_book(tmp_path,'centre-frame',[paint])
    appearance=book['pages'][0]['appearance']
    assert len(appearance['frames'])==1
    frame=appearance['frames'][0]
    assert (frame[2]-frame[0])*appearance['width_pt']==pytest.approx(300)
    assert (frame[3]-frame[1])*appearance['height_pt']==pytest.approx(200)


def test_filling_line_style_uses_label_instead_of_nearest_amount_line(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf);pdf.setFont('Helvetica',12)
            for label,y,width in [('Upper:',650 if new else 600,4 if new else 1),('Lower:',600 if new else 500,1)]:
                pdf.drawString(40,y,label);pdf.setLineWidth(width);pdf.line(100,y-2,400,y-2)
        return paint
    old=make_book(tmp_path,'amount-line-old',[draw(False)])
    new=make_book(tmp_path,'amount-line-new',[draw(True)])
    result=compare(old,new)
    diff=next(d for d in result['diffs'] if d['kind']=='border_style_changed')
    assert diff['measurements']['context']['field_label']=='Upper'
    assert diff['old_source']['bbox'][1]==pytest.approx(202/800-.001)
    assert diff['new_source']['bbox'][1]==pytest.approx(152/800-.001)
    pairs=diff['measurements']['confirmed_pairs']
    assert len(pairs)==2 and all(pair['context']['field_label'] in ('Upper','Lower') for pair in pairs)
    geometry=[d for d in result['diffs'] if d['kind']=='filling_line_geometry_changed']
    assert len(geometry)==2
    assert {d['measurements']['context']['field_label'] for d in geometry}=={'Upper','Lower'}
    assert all(d['old_source']['bbox'][1]>d['new_source']['bbox'][1] for d in geometry)


def test_verified_glyph_box_changes_display_source_without_changing_layout_geometry():
    from backend.pdf_appearance import _text_source
    char={'id':'c1','text':'字','bbox':[.2,.3,.23,.32],'glyph_bbox':[.2,.28,.23,.315],'size_pt':14}
    view={'number':1,'scopes':[[0,0,1,1]],'appearance':{'width_pt':600,'height_pt':800}}
    source=_text_source(view,[{'char':char,'text':'字','line':0}])
    assert source['bbox']==char['glyph_bbox']
    assert source['layout_bbox']==char['bbox']==[.2,.3,.23,.32]
    assert source['glyph_geometry_corrected_count']==1 and 'PDFium' in source['geometry_basis']


def test_same_ordinal_cannot_anchor_border_of_different_named_rows(tmp_path):
    def draw(new):
        def paint(pdf):
            title(pdf);pdf.setFont('Helvetica',10);pdf.setLineWidth(1)
            pdf.rect(40,500,460,200,stroke=1,fill=0);pdf.line(100,500,100,700);pdf.line(40,650,500,650)
            pdf.drawString(45,675,'No.');pdf.drawString(110,675,'Name')
            pdf.drawString(45,625,'1');pdf.drawString(110,625,'Beta' if new else 'Alpha')
            pdf.setLineWidth(4 if new else 1);pdf.line(40,570 if new else 600,500,570 if new else 600)
        return paint
    old=make_book(tmp_path,'ordinal-border-old',[draw(False)])
    new=make_book(tmp_path,'ordinal-border-new',[draw(True)])
    diffs=compare(old,new)['diffs']
    assert not any(d['kind']=='border_style_changed' for d in diffs)
    assert any(d['kind']=='border_correspondence_unverified' and d['severity']=='issue' for d in diffs)
