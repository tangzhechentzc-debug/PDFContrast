# OCR 校正与核验实施约定

原始 PDF、原页图像、原始提取 JSON 不修改。SQLite 追加事件形成任务全局修订号；当前内容通过原始提取和截至指定修订号的事件重放得到。每次比较记录独立 review_revision；不原地刷新旧结果。

## 操作语义

- cell_update：修改文字，必填原因；保存不等于核验，撤销该格及所属页的核验状态。允许空字符串清空、或改回原始文字，仍保留事件。
- cell_verify：确认当前识别值与原图一致，不确认该页没有漏识别。
- cell_unverify：撤销该格核验，并撤销所属页完整性确认。
- page_verify：用户显式声明已逐项核对且无漏字、漏行、错列后，确认整页和其全部识别格。失败页、结构不完整页不能通过此操作变成完整。
- page_unverify：撤销整页及其识别格核验。

物理页的原始 sheet/cell 标识生成稳定 canonical id。跨页逻辑候选中的复制单元格使用相同身份和校正；不按差异序号存储修改。金额按既有 Decimal 规则转值，编号前导零保留。

## API

GET `/api/jobs/{id}/review?side=old&page=1&offset=0&limit=100`：
`{revision,items:[{id,sheet,cell,page,bbox,source,original_text,current_text,value_type,structure,reviewed,correction_revision?}],total,offset,limit,pages:[{number,method,status,reviewed,reviewable,reviewed_cells,total_cells,structure_unconfirmed?}]}`。
`pages` 仅所选 side；status 为 ok/unavailable，structure 为 table/text_blocks。

POST `/api/jobs/{id}/review`：
`{expected_revision,side,action,target_id?,page?,value?,reason}` → `{revision,event}`。
expected_revision 为任务全局版本；旧版本保存返回409，不写事件。target/page/可核验状态由服务端检查。

GET `/api/jobs/{id}/review-history?offset&limit`：
`{revision,items:[{revision,side,action,target_id?,page,sheet?,cell?,original_text?,before_text?,after_text?,reason,created_at}],total,offset,limit}`。

GET `/api/jobs/{id}/runs` → `{items:Run[]}`。
POST runs 仍使用原 CompareConfig，自动捕获最新已提交修订。Run 增加 `review_revision` 与 `review_pages:{old:PdfPage[],new:PdfPage[]}`。结果页使用当次快照，编辑页显示当前状态。

导出固定使用该 run 的 revision，附 OCR修改记录 表，不能混入之后的校正。旧 run 没有 review_revision 时按0解释。

## 核验后的比较

涉及的 OCR 值、业务键以及相关页面完整性都已确认后，才允许把对应疑似差异升级为内容变化。未核验内容继续保留问题；已确认页不再产生待核验入口，但仍保持 method=ocr 以保存来源事实。核验不消除未比较范围、结构异常或提取失败。

## 交互

PDF结果页入口“校正与核验 OCR”。支持旧/新版、页选择、全部物理识别格列表（包含相同值）；选择项高亮原页。显示原始/当前文本，修改原因，独立保存与核验操作。409保留草稿并提示加载最新状态。显式整页核验复选框。

保存后提示当前比较未更新，可按原配置重新比较。历史记录显示修订、前后值、原因和时间；历史结果链接可恢复旧快照。
