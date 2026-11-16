# Excel 可运行版本接口约定

实施范围：两份 .xlsx，本地运行。保留已确认原型的配色、左侧差异列表、双原文视图和底部详情。实际导入/配置替代示例模式切换；PDF/OCR 后续接入。

## 共享模型

Source = `{sheet: string, cell: string, range?: string}`，每侧独立；缺失侧为 null。

Cell = `{address, row, column, value, display, value_type, formula, cached_value, cache_status, number_format, merged_range?, style?, hidden_row?, hidden_column?}`。
value 为 JSON 安全原值，数字用字符串；display 为可读字符串，不执行公式；value_type 为 number/string/boolean/date/blank/error/formula。
formula 为公式文本或 null；cache_status 为 not_formula/available/missing。

Sheet = `{name,max_row,max_column,hidden,suggested_header_row,nonempty_count,cells:{[address]:Cell},merged_ranges:string[]}`。
Workbook = `{sheets:Sheet[], warnings:string[]}`，文件存储于服务端，仅区域读取接口返回 cells。

SheetMeta 是 Sheet 去掉 cells/merged_ranges，上传后返回给前端。

ColumnsResponse = `{columns:[{column:"A",label:"项目编码"}], preview:[{row:2,values:{A:"001",B:"设备"}}]}`。
表头使用所选 start/end 行，合并格标题沿锚点展开，只作用于表头；多级表头以 `/` 拼接。可选 range 限制解析区域。

PairConfig = `{id,old_sheet:string|null,new_sheet:string|null,old_header_start:number,old_header_end:number,new_header_start:number,new_header_end:number,old_range:string|null,new_range:string|null,mode:"key"|"position",columns:[{id,name,old_column:string|null,new_column:string|null,is_key:boolean}]}`。
映射 ID 在 pair 内唯一，列不可重复映射；键列必须两侧都有；key 模式至少一个键列。
未映射的非空列必须作为列新增/删除或覆盖告警展示，不能静默忽略。
未配置的工作表在 coverage 中显示未比较，不能报整份文件一致。可配置单侧工作表，产生 sheet_added/removed。

CompareConfig = `{pairs:PairConfig[],absolute_tolerance:string,ignore_outer_whitespace:boolean,compare_formulas:boolean,compare_formatting:boolean}`。
默认容差 `"0"`，不根据显示格式舍入；相对容差固定为零。

Diff = `{id,kind,table_id,table_name,row_key:string,field:string,old_value:string|null,new_value:string|null,delta:string|null,message:string,severity:"change"|"issue",old_source:Source|null,new_source:Source|null}`。
kind = value_changed/formula_changed/format_changed/row_added/row_removed/column_added/column_removed/sheet_added/sheet_removed/match_ambiguous/formula_result_unavailable。

Summary = `{total,changes,issues,by_kind:{[kind]:count},matched_rows,unchanged_rows,compared_cells}`。
Coverage = `{compared_sheets:{old:string[],new:string[]},excluded_sheets:{old:string[],new:string[]},warnings:string[]}`。
Summary 统计完整结果；issues 或未比较范围存在时为 completed_with_issues，否则 completed。

## HTTP

- `GET /api/health` → `{status:"ok",version}`。
- `POST /api/jobs` multipart old_file/new_file → `{id,status:"ready",files:{old:{name,sha256,sheets:SheetMeta[]},new:{name,sha256,sheets:SheetMeta[]}},suggested_pairs:PairConfig[],warnings:string[]}`。上传过程中真实提取，服务端在工作线程执行；前端显示“读取工作簿…”。
- `GET /api/jobs/{id}` → 同上，支持刷新恢复。
- `GET /api/jobs/{id}/columns?side=old&sheet=...&header_start=1&header_end=1&range=...` → ColumnsResponse。
- `POST /api/jobs/{id}/runs` JSON CompareConfig → `{id,job_id,status,summary,coverage,config}`。当前阶段比较在工作线程执行并等待返回，前端显示忙碌状态并禁用重复提交。
- `GET /api/runs/{id}` → 同上，可刷新恢复。
- `GET /api/runs/{id}/diffs?offset=0&limit=100&kind=all&q=...` → `{items:Diff[],total,offset,limit}`，稳定次序，最多 500 条/页。
- `GET /api/jobs/{id}/sheet-region?side=old&sheet=...&start_row=1&start_column=1&rows=25&columns=10` → `{sheet,start_row,start_column,end_row,end_column,max_row,max_column,cells:Cell[],merged_ranges:string[],hidden:boolean}`。请求区域截断在工作表范围；合并格元数据用于定位。
- `GET /api/jobs/{id}/files/{side}` → 原文件副本下载。
- `GET /api/runs/{id}/export` → 独立差异明细 XLSX（完整事件、范围、规则），文本公式不可执行。
- `GET /api/examples/{side}` → 随应用附带的示例工作簿下载；UI 可提供“下载示例旧版/新版”，不假装已上传真实文件。

错误：`{detail:中文可理解错误信息}`，400/413/422 输入错误，404 不存在，500 解析/比较内部错误不得向用户暴露服务器路径。

## Python 模块职责（并行边界）

`backend/excel.py`：`extract_workbook(path:Path)->Workbook`；`sheet_columns(workbook,sheet_name,header_start,header_end,cell_range=None)->ColumnsResponse`；`suggest_pairs(old,new)->list[PairConfig]`；`sheet_region(workbook,sheet_name,start_row,start_column,rows,columns)->dict`。

`backend/compare.py`：`compare_workbooks(old,new,config)->{diffs:list[Diff],summary:Summary,coverage:Coverage}`，输入格式非法 raise ValueError。

`backend/app.py`（主代理）：FastAPI、文件读写和校验、SQLite 任务/结果存储、API、导出、静态前端、启动入口。

`frontend/`（前端代理）：React+Vite+TypeScript，调用上述 API，构建产物 frontend/dist 由 FastAPI 同源托管；Vite 开发代理 /api → 127.0.0.1:8765。读取响应并实时显示，不使用固定假数据。

`tests/test_excel.py`、`tests/test_compare.py`（引擎代理）：真实 openpyxl 工作簿单元测试。
`tests/test_api.py`、`scripts/`、README、依赖安装及端到端验证（主代理）。

后端数字比较必须 Decimal 精确处理；DataComPy 确实参与已对齐表的比较，不将其 report 的样例行作为全量明细。根据实际安装版本适配 API。重复键/空键不强制配对；单侧不存在来源为 null。原文件只读，不重算公式。
