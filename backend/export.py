import io
import json

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

EXCEL_EXPORT_MAX_ROWS = 1048576


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _clean_cell_value(value):
    # 写入前剔除 XML 不允许的控制字符（如 \x0c、\x0b），保留非字符串原值类型
    return ILLEGAL_CHARACTERS_RE.sub("", value) if isinstance(value, str) else value


def _append_row(sheet, row):
    sheet.append([_clean_cell_value(value) for value in row])


def source_label(source):
    if not source:
        return ""
    if source.get("kind") == "pdf":
        if source.get("manual"):
            return f"第 {source['page']} 页 · {source.get('sheet', '整页')} · 人工补录，仅定位整页"
        return f"第 {source['page']} 页 · {source.get('sheet', '原页')} · 区域 {source.get('bbox', [0, 0, 1, 1])}"
    return f"{source['sheet']}!{source.get('range') or source['cell']}"


def export_report(store, run_id: str, *, max_rows: int | None = None):
    # 表头固定占用第 1 行，每张工作表至少保留 1 行表头和 1 行数据
    row_limit = max(2, EXCEL_EXPORT_MAX_ROWS if max_rows is None else max_rows)
    run = store.run(run_id)
    job = store.job(run["job_id"])
    full_report = run.get("report_inventory_version", 0) >= 2
    category_labels = {"content": "内容", "structure": "结构", "format": "格式", "layout": "版式", "completeness": "完整性", "business": "业务数据"}
    book = Workbook()
    summary = book.active
    summary.title = "摘要"
    _append_row(summary, ["项目", "内容"])
    for label, value in [
        ("旧版", job["files"]["old"]["name"]), ("新版", job["files"]["new"]["name"]),
        ("状态", "部分完成/待核对" if run["status"] == "completed_with_issues" else "比较完成"),
        ("变化事件", run["summary"]["changes"]), ("待核对事件", run["summary"]["issues"]),
        ("旧版 SHA-256", job["files"]["old"]["sha256"]), ("新版 SHA-256", job["files"]["new"]["sha256"]),
        ("说明", "以完整业务报表比较内容、结构、格式与版式；原始物理尺寸和渲染证据保留，未验证属性单列，不等于一致；原件未修改" if full_report else
         "PDF 按提取内容比较；OCR 结果和疑似差异需核对原图，未比较图形外观；原件未修改" if job.get("kind") == "pdf" else "公式结果来自文件缓存，未重新计算；原件未修改"),
    ]:
        _append_row(summary, [label, value])
    detail = book.create_sheet("全部差异")
    issues = book.create_sheet("待核对")
    header = ["类型", "报表", "项目/业务键", "字段", "旧值", "新值", "差额", "旧位置", "新位置", "说明"]
    if full_report:
        header = ["类别", "判定", *header]
    _append_row(detail, header)
    _append_row(issues, header)
    reliable = None
    category_sheets = {}
    if "reports" in run:
        inventory = book.create_sheet("报表完整性")
        _append_row(inventory, ["报表类型", "报表名称", "工程对象", "归类", "判定", "旧页范围", "新页范围", "依据", "阻塞原因", "内容比较状态", "全部变化数" if full_report else "内容变化数"] +
                    (["内容摘要", "结构摘要", "格式摘要", "版式摘要"] if full_report else []))
        presence_labels = {"shared": "共有", "old_only": "仅旧版", "new_only": "仅新版", "uncertain": "待确认"}
        for report in run["reports"]:
            sides = [report.get(side) or {} for side in ("old", "new")]
            single_sided = bool(report.get("old")) != bool(report.get("new"))
            confirmation = ("确认缺失" if report.get("confirmation") == "confirmed" else "待确认缺失") if single_sided else (
                "已确认" if report.get("confirmation") == "confirmed" else "待确认")
            _append_row(inventory, [report.get("type_code"), report.get("title"),
                                    _encode(report.get("object")) if isinstance(report.get("object"), dict) else report.get("object"),
                                    presence_labels.get(report.get("presence"), report.get("presence")),
                                    confirmation,
                                    ", ".join(map(str, sides[0].get("pages", []))), ", ".join(map(str, sides[1].get("pages", []))),
                                    report.get("basis"), "；".join(report.get("blocking_reasons", [])),
                                    report.get("content_status"), report.get("change_count", 0)] +
                                   ([report.get("dimensions", {}).get(category, {}).get("summary", "当次未计算")
                                     for category in ("content", "structure", "format", "layout")] if full_report else []))
        types = book.create_sheet("报表类型覆盖")
        _append_row(types, ["归类", "类型代码", "名称", "旧版页", "新版页"])
        for category, entries in run.get("type_inventory", {}).items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                _append_row(types, [presence_labels.get(category, category), entry.get("type_code"), entry.get("title"),
                                    ", ".join(map(str, entry.get("old_pages", []))), ", ".join(map(str, entry.get("new_pages", [])))])
        pending = book.create_sheet("提取与配对问题")
        _append_row(pending, ["问题类型", "说明", "影响报表", "版本", "页", "来源", "处理入口"])
        for issue in run.get("issues", []):
            _append_row(pending, [issue.get("type"), issue.get("message"), ", ".join(issue.get("report_ids", [])),
                                  issue.get("side"), ", ".join(map(str, issue.get("pages", []))),
                                  "；".join(source_label(source) for source in issue.get("sources", [])),
                                  ", ".join(issue.get("actions", []))])
        if full_report:
            for category in ("content", "structure", "format", "layout", "business"):
                category_sheets[category] = book.create_sheet(f"{category_labels[category]}差异")
                _append_row(category_sheets[category], header)
            validation = book.create_sheet("属性验证范围")
            _append_row(validation, ["报表编号", "报表名称", "类别", "状态", "变化数", "待核验数", "摘要", "限制与未支持属性"])
            page_pairs = book.create_sheet("原页对应")
            _append_row(page_pairs, ["报表编号", "报表名称", "旧版物理页", "新版物理页", "配页状态", "依据"])
            paragraph_evidence = None
            if any("paragraph_checks" in report for report in run["reports"]):
                paragraph_evidence = book.create_sheet("段落测量证据")
                _append_row(paragraph_evidence, ["报表编号", "报表名称", "属性", "状态", "旧侧结果", "新侧结果", "旧位置", "新位置", "依据与限制", "原始测量证据"])
            dimensions_labels = {"changed": "存在差异", "unchanged": "已比较属性未发现变化", "partial": "部分已验证",
                                 "unverified": "待确认／未验证", "not_applicable": "不适用"}
            for report in run["reports"]:
                if business := report.get("business"):
                    _append_row(validation, [report.get("type_code"), report.get("title"), "业务数据",
                                             {"changed": "存在差异", "unchanged": "已比较字段未发现变化", "pending": "待核对"}.get(business.get("status"), "待核对"),
                                             business.get("changes", 0), len(business.get("issues", [])),
                                             f"已比较 {business.get('compared_rows', 0)} 行、{business.get('compared_fields', 0)} 个字段",
                                             "；".join(business.get("issues", []))])
                for category in ("content", "structure", "format", "layout"):
                    dimension = report.get("dimensions", {}).get(category, {})
                    _append_row(validation, [report.get("type_code"), report.get("title"), category_labels[category],
                                             dimensions_labels.get(dimension.get("status"), "当次未计算"), dimension.get("changes"), dimension.get("issues"),
                                             dimension.get("summary"), "；".join(dimension.get("limitations", []))])
                for pair in report.get("page_pairs", []):
                    _append_row(page_pairs, [report.get("type_code"), report.get("title"), pair.get("old_page"), pair.get("new_page"),
                                             "已对应" if pair.get("status") == "confirmed" else "待确认", pair.get("basis")])
                for check in report.get("paragraph_checks", []):
                    measurement = _encode(check.get("measurements", {}))
                    parts = [measurement[index:index + 30000] for index in range(0, len(measurement), 30000)]
                    for index, part in enumerate(parts, 1):
                        message = check.get("message", "")
                        if len(parts) > 1:
                            message += f" 原始测量证据片段 {index}/{len(parts)}，按行顺序拼接末列。"
                        _append_row(paragraph_evidence, [
                            report.get("type_code"), report.get("title"),
                            {"line_spacing": "实际段落行距", "alignment": "左／中／右对齐"}.get(check.get("attribute"), check.get("attribute")),
                            dimensions_labels.get(check.get("status"), "待确认／未验证"), check.get("old_value"), check.get("new_value"),
                            source_label(check.get("old_source")), source_label(check.get("new_source")), message, part])
        else:
            reliable = book.create_sheet("可靠共有报表内容差异")
            _append_row(reliable, header)
        scopes = book.create_sheet("非报表文档范围")
        _append_row(scopes, ["版本", "范围类别", "物理页", "依据", "来源"])
        scope_labels = {"blank_page": "空白或仅页码", "document_section": "文档正文", "continuation_note": "报表续注", "unclassified_region": "未确定文档区域"}
        for region in run.get("non_report_regions", []):
            _append_row(scopes, ["旧版" if region.get("side") == "old" else "新版",
                                 scope_labels.get(region.get("type"), region.get("type")),
                                 ", ".join(map(str, region.get("pages", []))), region.get("basis"), source_label(region.get("source"))])
    elif job.get("kind") == "pdf":
        _append_row(summary, ["报表完整性", "该历史结果尚无业务报表完整性判定；请重新提取后比较"])
    with store.connect() as db:
        for (payload,) in db.execute("SELECT payload FROM diffs WHERE run_id=? ORDER BY seq", (run_id,)):
            d = json.loads(payload)
            row = [d["kind"], d["table_name"], d["row_key"], d["field"], d["old_value"], d["new_value"],
                   d["delta"], source_label(d["old_source"]), source_label(d["new_source"]), d["message"]]
            if full_report:
                row = [category_labels.get(d.get("category"), "待确认"), "待核验" if d["severity"] == "issue" else "存在差异", *row]
            _append_row(detail, row)
            if d["severity"] == "issue":
                _append_row(issues, row)
            if reliable is not None and d.get("layer") == "content" and d["severity"] != "issue":
                _append_row(reliable, row)
            if d.get("category") in category_sheets:
                _append_row(category_sheets[d["category"]], row)
    rules = book.create_sheet("范围与规则")
    _append_row(rules, ["类别", "设置"])
    rule_sheet_number = 1
    rule_sheet_rows = 1

    def append_rule_row(path, text):
        nonlocal rules, rule_sheet_number, rule_sheet_rows
        if rule_sheet_rows >= row_limit:
            rule_sheet_number += 1
            rules = book.create_sheet(f"范围与规则 {rule_sheet_number}")
            _append_row(rules, ["类别", "设置"])
            rule_sheet_rows = 1
        _append_row(rules, [path, text])
        rule_sheet_rows += 1

    def append_rule(path, value, compact=False):
        if not compact and isinstance(value, dict) and value:
            for key, child in value.items():
                append_rule(f"{path}.{key}", child)
        elif not compact and isinstance(value, list) and value:
            for index, child in enumerate(value):
                append_rule(f"{path}[{index}]", child, compact=path.endswith(".paragraph_checks"))
        else:
            text = _encode(value)
            if len(text) <= 30000:
                append_rule_row(path, text)
            else:
                parts = [text[i:i + 30000] for i in range(0, len(text), 30000)]
                for index, part in enumerate(parts, 1):
                    append_rule_row(f"{path}（片段 {index}/{len(parts)}，按顺序拼接）", part)

    for name in ("config", "coverage"):
        append_rule(name, run[name])
    for name in ("reports", "issues", "type_inventory", "non_report_regions"):
        if name in run:
            append_rule(name, run[name])
    if job.get("kind") == "pdf":
        revision = run.get("review_revision", 0)
        _append_row(summary, ["OCR 核验修订号", revision])
        if job.get("parent_job_id"):
            _append_row(summary, ["重新提取来源任务", job["parent_job_id"]])
            _append_row(summary, ["历史修订说明", "原任务的人工修订及历史结果仍保留；重新提取后的区域需重新核验，未自动迁移旧坐标修订"])
        history = book.create_sheet("OCR修改记录")
        _append_row(history, ["修订", "版本", "操作", "页面", "表", "单元格", "原始文本", "修改前", "修改后", "原因", "时间"])
        _, events = store.reviews(job["id"], revision)
        for event in events:
            _append_row(history, [event.get(key, "") for key in ("revision", "side", "action", "page", "sheet", "cell",
                                                                  "original_text", "before_text", "after_text", "reason", "created_at")])
        structures = [event for event in events if event["action"] == "table_update"]
        if structures:
            structural_detail = book.create_sheet("结构修改明细")
            _append_row(structural_detail, ["修订", "版本", "页面", "表", "阶段", "坐标", "当前文本", "原始识别文本", "稳定标识", "来源", "原因", "时间"])
            for event in structures:
                for field, phase in (("table_before", "修改前"), ("table_after", "修改后")):
                    snapshot = event[field]
                    for row_index, row in enumerate(snapshot["rows"], 1):
                        for column_index, cell in enumerate(row, 1):
                            source = {**cell.get("source", {}), "sheet": snapshot["sheet"]}
                            _append_row(structural_detail, [event["revision"], event["side"], event["page"], snapshot["sheet"],
                                                            phase, f"{get_column_letter(column_index)}{row_index}", cell["text"],
                                                            cell.get("original_text", ""), cell["id"], source_label(source),
                                                            event["reason"], event["created_at"]])
    for sheet in book:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for row in sheet:
            for cell in row:
                # 强制字符串单元格保持文本类型，防止带前导 = 被 Excel 作为公式执行
                if isinstance(cell.value, str):
                    cell.data_type = "s"
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="545E59")
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = min(
                65, max(14, max(len(str(c.value or "")) for c in column) + 2))
    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return buffer
