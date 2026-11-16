# OCR 手工结构修复约定

本阶段支持已有 OCR 物理表/文字块的网格编辑：补行、增删行列、重排行列、移动/交换单元格、编辑文字。文字块可重建为表格；不创建新的页面、不编辑文本型 PDF/Excel。原 PDF/JSON 不修改。

## 身份、来源与核验

- 已有格的稳定 review_id 不因行列移动而变化，保留 raw_text、原始来源框。删除格仅从当前结构移除，历史快照保留。
- 新格由服务端以任务/side/表名/修订/位置生成稳定 ID。raw_text 为空，manual=true，source.manual=true，source.location_precision=page，bbox=[0,0,1,1]，明确只定位整页，不能冒充 OCR 识别来源框。source.method 保持ocr，使未核验人工输入同样归入疑似。
- 保存结构必须填写原因，并显式确认该区域行列结构已按原图核对；该声明不是内容核验。结构修改撤销本页所有格及页面核验。
- 原先 structure_unconfirmed 的页：只有该页所有原始物理区域都已重建为 structure=table 且 structure_confirmed 后，解除结构限制；失败页仍不可修复/核验。已解除警告需准确更新，不能删除其他范围警告。
- 物理表重新建立 row/column/address/max_row/max_column。依当前表头重建逻辑续表候选，不能保留被删除行或把不兼容列拼接。既有复核格ID保留。

## API

GET `/api/jobs/{job}/review-tables?side=old&page=1&sheet=...`。
不传sheet只返回目录：`{revision,structure_revision,tables:[{sheet,page,structure,rows,columns,editable,edit_limit?}]}`。
传sheet返回上述字段加 `table:{sheet,page,structure,rows:[[{id,text,original_text,source,manual,reviewed}]],structure_confirmed}`。rows含首行表头，文字块的合成表头也显示，可在重建时改成真实表头。

POST `/api/jobs/{job}/review` 新 action `table_update`：
`{expected_revision,side,action,sheet,page,rows:[[{id?:string,text:string}]],structure_confirmed:true,reason}`。
只可引用当前选定物理表的id，一格只出现一次。没有id表示新格。矩阵矩形、至少1行1列、最多500行60列且总格数<=10000；单格文本<=10000，单次文本总量<=200万字符。超限不允许结构编辑，不隐式裁剪。
保存时服务端构建事件 `table_before`/`table_after`（同table返回形状），以及 before_text/after_text 尺寸概述、structure_revision。完整事件追加保存；快照由服务端产生，客户端不得提交来源/已核验字段。

引擎函数（backend/review.py）：
- `structure_tables(book)` -> 目录 list。
- `structure_table(book,sheet)` -> 上述table对象，验证OCR物理表。
- `prepare_structure_event(current_book,event)` -> 复制并补全 table_before/table_after 等字段，校验客户端rows，生成新id。
- `apply_reviews(original,events)` 支持 table_update（使用事件after快照或已验证rows重放），后续cell_update可通过不变id找到移动/新格。

GET job 返回当前生效的 sheets/pages/suggested_pairs，加 `review_revision` 与 `structure_revision`（任务全局最新 table_update 或第1行表头 cell_update 的 revision，默认0）。保留 Store.job 的原始元数据语义，响应由快照构建。
CompareConfig 增加 `review_revision?:number`。当存在结构修订时，只有 `latest_structure_revision <= config.review_revision <= snapshot_revision` 才允许比较，否则409提示重新确认结构和映射。Run仍固定review_revision及review_pages快照。

历史和导出保留全部结构前后快照；导出新增“结构修改明细”逐格表，包含修订、旧/新版、表、前/后、地址、文本、稳定ID、来源定位。

## 前端

ReviewWorkspace增加“补行与调整结构”模式/入口，进入独立 StructureEditor，仅允许无文字草稿时切换。
编辑器支持版本/页/物理表选择、网格编辑、插入/删除/移动行列、交换两格，原图同步定位；新增格显示“人工补录·整页定位”。草稿可撤销最近操作/放弃；保存原因与结构确认复选框必填。切换表/页/side、离开编辑器须先保存或放弃草稿；409保留草稿并加载最新结构，提示核对删除/移动冲突，不能自动丢弃。
历史可展开结构前后网格，查看增删/移位；记录不覆盖。结构保存后“按原配置重新比较”不可用，改为“确认结构与比较规则”。App刷新job最新元数据，以最新建议进入ConfigurationEditor，不复用旧列映射；提交时包含当前job.review_revision。普通文字校正仍可复用没有过时结构的原配置。
PdfViewer人工格显示整页定位且不得标为精确差异区域；已有格保持原定位。
