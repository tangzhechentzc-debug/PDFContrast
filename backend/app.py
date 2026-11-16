import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
import pdfplumber
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .compare import compare_workbooks
from .excel import extract_workbook, sheet_columns, sheet_region, suggest_pairs
from .models import CompareConfig, ReviewAction
from .pdf import extract_pdf, compare_pdfs, suggest_pdf_pairs, MAX_PAGE_PIXELS, _RENDER_LOCK
from .review import apply_reviews, review_catalog, structure_tables, structure_table, prepare_structure_event

ROOT = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
logger = logging.getLogger("reportcontrast")


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def public_pdf_pages(book):
    return [{key: value for key, value in page.items() if key != "appearance"} for page in book.get("pages", [])]


def structure_revision(events):
    return max((event["revision"] for event in events if event["action"] == "table_update" or
                event["action"] == "cell_update" and re.fullmatch(r"[A-Z]+1", event.get("cell", ""))), default=0)


class Store:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.database = folder / "tasks.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS diffs (
                    run_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
                    search TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id, seq));
                CREATE INDEX IF NOT EXISTS diff_kind ON diffs(run_id, kind, seq);
                CREATE TABLE IF NOT EXISTS review_events (
                    job_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(job_id, revision));
                CREATE TABLE IF NOT EXISTS imports (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, metadata TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def job(self, job_id: str):
        with self.connect() as db:
            row = db.execute("SELECT metadata FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "找不到此比较任务，请重新导入文件")
        return json.loads(row[0])

    def import_status(self, import_id):
        with self.connect() as db:
            row = db.execute("SELECT metadata FROM imports WHERE id=?", (import_id,)).fetchone()
        if not row:
            raise HTTPException(404, "找不到此导入任务，请重新选择文件")
        return json.loads(row[0])

    def update_import(self, import_id, **changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT metadata FROM imports WHERE id=?", (import_id,)).fetchone()
            if not row:
                raise ValueError("找不到此导入任务")
            metadata = json.loads(row[0])
            metadata.update(changes, updated_at=datetime.now(timezone.utc).isoformat())
            db.execute("UPDATE imports SET status=?,metadata=? WHERE id=?",
                       (metadata["status"], encode(metadata), import_id))
        return metadata

    def recover_imports(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,metadata FROM imports WHERE status IN ('queued','processing')").fetchall()
            for import_id, payload in rows:
                metadata = json.loads(payload)
                ready = db.execute("SELECT 1 FROM jobs WHERE id=?", (import_id,)).fetchone() is not None
                metadata.update(status="ready" if ready else "failed", updated_at=datetime.now(timezone.utc).isoformat())
                metadata["progress"]["stage"] = "completed" if ready else "failed"
                if ready:
                    metadata["job_id"] = import_id
                else:
                    metadata["error"] = "服务已重启，之前的导入已中断。请重新选择文件并导入。"
                db.execute("UPDATE imports SET status=?,metadata=? WHERE id=?",
                           (metadata["status"], encode(metadata), import_id))

    def run(self, run_id: str):
        with self.connect() as db:
            row = db.execute("SELECT metadata FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "找不到此比较结果")
        return json.loads(row[0])

    def workbook(self, job_id: str, side: str):
        self.job(job_id)
        book = json.loads((self.folder / job_id / f"{side}.json").read_text())
        book["review_namespace"] = f"{job_id}:{side}"
        return book

    def reviews(self, job_id: str, revision: int | None = None):
        self.job(job_id)
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM review_events WHERE job_id=? AND revision<=? ORDER BY revision",
                              (job_id, revision if revision is not None else 9223372036854775807)).fetchall()
        events = [json.loads(row[0]) for row in rows]
        return (events[-1]["revision"] if events else 0), events

    def reviewed_book(self, job_id, side, events):
        return apply_reviews(self.workbook(job_id, side), [e for e in events if e["side"] == side])

    def current_job(self, job_id):
        job = self.job(job_id)
        if job.get("kind") != "pdf":
            return job
        revision, events = self.reviews(job_id)
        books = {side: self.reviewed_book(job_id, side, events) for side in ("old", "new")}
        for side, book in books.items():
            job["files"][side]["sheets"] = [{k: v for k, v in sheet.items() if k not in ("cells", "merged_ranges")}
                                              for sheet in book["sheets"]]
            job["files"][side]["pages"] = public_pdf_pages(book)
            if book.get("report_inventory_version"):
                job["files"][side]["reports"] = book.get("reports", [])
                job["files"][side]["non_report_regions"] = book.get("non_report_regions", [])
                job["report_inventory_version"] = book["report_inventory_version"]
        job.update(review_revision=revision,
                   structure_revision=structure_revision(events),
                   suggested_pairs=suggest_pdf_pairs(books["old"], books["new"]),
                   warnings=books["old"].get("warnings", []) + books["new"].get("warnings", []))
        return job

    def save_review(self, job_id: str, request: ReviewAction):
        if self.job(job_id).get("kind") != "pdf":
            raise ValueError("仅 PDF 任务支持 OCR 校正与核验")
        original = self.workbook(job_id, request.side)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT payload FROM review_events WHERE job_id=? ORDER BY revision", (job_id,)).fetchall()
            events = [json.loads(row[0]) for row in rows]
            revision = events[-1]["revision"] if events else 0
            if request.expected_revision != revision:
                raise HTTPException(409, "当前内容已被其他操作更新，请加载最新状态后重新提交；草稿未保存")
            side_events = [e for e in events if e["side"] == request.side]
            current = apply_reviews(original, side_events)
            event = request.model_dump(exclude={"expected_revision"}, exclude_none=True)
            event.update(revision=revision + 1, created_at=datetime.now(timezone.utc).isoformat())
            if request.action == "table_update":
                event = prepare_structure_event(current, event)
            elif request.action.startswith("cell_"):
                target = next((item for item in review_catalog(current) if item["id"] == request.target_id), None)
                if target is None:
                    raise ValueError("没有找到此任务及版本对应的 OCR 单元格")
                if request.page is not None and request.page != target["page"]:
                    raise ValueError("目标单元格与页面不一致")
                if request.action == "cell_update" and request.value is None:
                    raise ValueError("请提供校正文本；清空请提交空字符串")
                event.update(page=target["page"], sheet=target["sheet"], cell=target["cell"],
                             original_text=target["original_text"], before_text=target["current_text"],
                             after_text=request.value if request.action == "cell_update" else target["current_text"])
            elif request.target_id is not None or request.page is None:
                raise ValueError("整页核验必须提供页面，且不能指定单元格")
            if request.action != "cell_update" and request.value is not None:
                raise ValueError("只有校正操作可以提交文本")
            apply_reviews(original, [*side_events, event])
            db.execute("INSERT INTO review_events VALUES (?,?,?)", (job_id, event["revision"], encode(event)))
        return {"revision": event["revision"], "event": event}


def validate_xlsx(path: Path):
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 20000 or sum(x.file_size for x in entries) > MAX_UNPACKED_BYTES:
                raise ValueError("工作簿解压后过大，请减少工作表或另存较小的文件")
            names = {x.filename for x in entries}
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise ValueError("文件不是有效的 .xlsx 工作簿")
            if any("vbaproject" in x.lower() for x in names):
                raise ValueError("当前版本仅支持不含宏的 .xlsx 工作簿")
            content_types = archive.read("[Content_Types].xml")
            if b"macroEnabled" in content_types or b"spreadsheetml.sheet.main+xml" not in content_types:
                raise ValueError("当前版本仅支持 .xlsx，请先将文件另存为 .xlsx")
    except zipfile.BadZipFile:
        raise ValueError("无法读取工作簿；文件可能已损坏、加密或不是 .xlsx 格式")


async def save_upload(file: UploadFile, destination: Path):
    name = (file.filename or "").replace("\\", "/").split("/")[-1]
    if Path(name).suffix.lower() not in (".xlsx", ".pdf"):
        raise HTTPException(400, "请选择两份 .xlsx 或两份 .pdf 文件；暂不支持混合类型、.xls 或含宏工作簿")
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as stream:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise HTTPException(413, "每份文件不能超过 50 MB")
            digest.update(chunk)
            stream.write(chunk)
    await file.close()
    if not size:
        raise HTTPException(400, "上传的文件为空")
    return {"name": name, "sha256": digest.hexdigest(), "size": size}


def ingest(store, job_id, files, progress=None, parent_job_id=None):
    folder = store.folder / job_id
    books = {}
    kind = files["old"]["kind"]
    for side in ("old", "new"):
        path = folder / f"{side}.{kind}"
        if progress:
            progress(side, {"stage": "opening", "unit": "page" if kind == "pdf" else "sheet",
                            "current": 0, "total": None, "completed": 0})
        if kind == "xlsx":
            validate_xlsx(path)
        elif not path.read_bytes()[:1024].lstrip().startswith(b"%PDF-"):
            raise ValueError("文件不是有效的 PDF，请检查文件内容")
        try:
            options = {"progress": lambda detail, side=side: progress(side, detail)} if progress else {}
            books[side] = extract_pdf(path, folder / f"{side}-pages", **options) if kind == "pdf" else extract_workbook(path, **options)
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("Document parsing failed (%s)", type(exc).__name__)
            raise ValueError("无法解析文件，请检查是否损坏、加密或包含不支持的内容") from exc
        (folder / f"{side}.json").write_text(encode(books[side]))
        files[side]["sheets"] = [
            {k: v for k, v in sheet.items() if k not in ("cells", "merged_ranges")}
            for sheet in books[side]["sheets"]
        ]
        if kind == "pdf":
            files[side]["pages"] = public_pdf_pages(books[side])
            if books[side].get("report_inventory_version"):
                files[side]["reports"] = books[side].get("reports", [])
                files[side]["non_report_regions"] = books[side].get("non_report_regions", [])
    if progress:
        progress(None, {"stage": "finalizing"})
    metadata = {
        "id": job_id, "kind": kind, "status": "ready", "files": files,
        "suggested_pairs": (suggest_pdf_pairs if kind == "pdf" else suggest_pairs)(books["old"], books["new"]),
        "warnings": books["old"].get("warnings", []) + books["new"].get("warnings", []),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if parent_job_id:
        metadata["parent_job_id"] = parent_job_id
    if kind == "pdf" and all(book.get("report_inventory_version") for book in books.values()):
        metadata["report_inventory_version"] = min(book["report_inventory_version"] for book in books.values())
    with store.connect() as db:
        db.execute("INSERT INTO jobs VALUES (?,?)", (job_id, encode(metadata)))
    return metadata


def process_import(store, job_id, files, parent_job_id=None):
    def progress(side, detail):
        previous = store.import_status(job_id)["progress"]
        current = previous if side is None or previous["side"] == side else {
            "side": side, "file_index": 1 if side == "old" else 2, "file_count": 2,
            "stage": "opening", "unit": None, "current": 0, "total": None, "completed": 0}
        store.update_import(job_id, status="processing", progress={**current, **detail})

    try:
        ingest(store, job_id, files, progress=progress, **({"parent_job_id": parent_job_id} if parent_job_id else {}))
        current = store.import_status(job_id)["progress"]
        store.update_import(job_id, status="ready", job_id=job_id, progress={**current, "stage": "completed"})
    except Exception as exc:
        logger.exception("Background import failed (%s)", job_id)
        current = store.import_status(job_id)["progress"]
        message = str(exc) if isinstance(exc, ValueError) else "读取失败，请重新导入；详细原因已记录在本机服务日志。"
        store.update_import(job_id, status="failed", error=message, progress={**current, "stage": "failed"})


def execute_comparison(store, job_id, config):
    job = store.job(job_id)
    is_pdf = job.get("kind") == "pdf"
    revision, events = store.reviews(job_id) if is_pdf else (0, [])
    current_structure_revision = structure_revision(events)
    configured_revision = config.get("review_revision")
    if current_structure_revision and (configured_revision is None or not current_structure_revision <= configured_revision <= revision):
        raise HTTPException(409, "识别内容或结构已调整，请加载最新修订并核对报表对应关系后再比较" if job.get("report_inventory_version", 0) >= 2 else
                            "表格结构已调整，请加载最新结构并重新确认比较范围、列映射和业务键后再比较")
    books = {side: store.reviewed_book(job_id, side, events) if is_pdf else store.workbook(job_id, side)
             for side in ("old", "new")}
    if is_pdf and all(book.get("report_inventory_version", 0) >= 2 for book in books.values()):
        config = {**config, "compare_formatting": True, "compare_formulas": False,
                  "ignore_outer_whitespace": False, "absolute_tolerance": "0"}
    output = (compare_pdfs if is_pdf else compare_workbooks)(books["old"], books["new"], config)
    run_id = uuid.uuid4().hex
    coverage = output["coverage"]
    incomplete = bool(output["summary"]["issues"] or coverage.get("warnings") or
                      any(coverage.get("excluded_sheets", {}).values()) or output.get("issues") or
                      any(r.get("confirmation") == "pending" or r.get("content_status") == "pending"
                          for r in output.get("reports", [])))
    result = {
        "id": run_id, "job_id": job_id,
        "status": "completed_with_issues" if incomplete else "completed",
        "summary": output["summary"], "coverage": coverage, "config": config,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if is_pdf:
        result.update(review_revision=revision, structure_revision=current_structure_revision,
                      review_pages={side: public_pdf_pages(books[side]) for side in books})
        for key in ("reports", "issues", "type_inventory", "non_report_regions", "report_inventory_version"):
            if key in output:
                result[key] = output[key]
    with store.connect() as db:
        db.execute("INSERT INTO runs VALUES (?,?,?)", (run_id, job_id, encode(result)))
        db.executemany("INSERT INTO diffs VALUES (?,?,?,?,?)", [
            (run_id, i, diff["kind"], " ".join(str(diff.get(k, "")) for k in
             ("table_name", "row_key", "field", "old_value", "new_value", "message")).casefold(), encode(diff))
            for i, diff in enumerate(output["diffs"])
        ])
    return result


def source_label(source):
    if not source:
        return ""
    if source.get("kind") == "pdf":
        if source.get("manual"):
            return f"第 {source['page']} 页 · {source.get('sheet', '整页')} · 人工补录，仅定位整页"
        return f"第 {source['page']} 页 · {source.get('sheet', '原页')} · 区域 {source.get('bbox', [0, 0, 1, 1])}"
    return f"{source['sheet']}!{source.get('range') or source['cell']}"


def export_report(store, run_id):
    run = store.run(run_id)
    job = store.job(run["job_id"])
    full_report = run.get("report_inventory_version", 0) >= 2
    category_labels = {"content": "内容", "structure": "结构", "format": "格式", "layout": "版式", "completeness": "完整性"}
    book = Workbook()
    summary = book.active
    summary.title = "摘要"
    summary.append(["项目", "内容"])
    for label, value in [
        ("旧版", job["files"]["old"]["name"]), ("新版", job["files"]["new"]["name"]),
        ("状态", "部分完成/待核对" if run["status"] == "completed_with_issues" else "比较完成"),
        ("变化事件", run["summary"]["changes"]), ("待核对事件", run["summary"]["issues"]),
        ("旧版 SHA-256", job["files"]["old"]["sha256"]), ("新版 SHA-256", job["files"]["new"]["sha256"]),
        ("说明", "以完整业务报表比较内容、结构、格式与版式；原始物理尺寸和渲染证据保留，未验证属性单列，不等于一致；原件未修改" if full_report else
         "PDF 按提取内容比较；OCR 结果和疑似差异需核对原图，未比较图形外观；原件未修改" if job.get("kind") == "pdf" else "公式结果来自文件缓存，未重新计算；原件未修改"),
    ]:
        summary.append([label, value])
    detail = book.create_sheet("全部差异")
    issues = book.create_sheet("待核对")
    header = ["类型", "报表", "项目/业务键", "字段", "旧值", "新值", "差额", "旧位置", "新位置", "说明"]
    if full_report:
        header = ["类别", "判定", *header]
    detail.append(header)
    issues.append(header)
    reliable = None
    category_sheets = {}
    if "reports" in run:
        inventory = book.create_sheet("报表完整性")
        inventory.append(["报表类型", "报表名称", "工程对象", "归类", "判定", "旧页范围", "新页范围", "依据", "阻塞原因", "内容比较状态", "全部变化数" if full_report else "内容变化数"] +
                         (["内容摘要", "结构摘要", "格式摘要", "版式摘要"] if full_report else []))
        presence_labels = {"shared": "共有", "old_only": "仅旧版", "new_only": "仅新版", "uncertain": "待确认"}
        for report in run["reports"]:
            sides = [report.get(side) or {} for side in ("old", "new")]
            single_sided = bool(report.get("old")) != bool(report.get("new"))
            confirmation = ("确认缺失" if report.get("confirmation") == "confirmed" else "待确认缺失") if single_sided else (
                "已确认" if report.get("confirmation") == "confirmed" else "待确认")
            inventory.append([report.get("type_code"), report.get("title"),
                              encode(report.get("object")) if isinstance(report.get("object"), dict) else report.get("object"),
                              presence_labels.get(report.get("presence"), report.get("presence")),
                              confirmation,
                              ", ".join(map(str, sides[0].get("pages", []))), ", ".join(map(str, sides[1].get("pages", []))),
                              report.get("basis"), "；".join(report.get("blocking_reasons", [])),
                              report.get("content_status"), report.get("change_count", 0)] +
                             ([report.get("dimensions", {}).get(category, {}).get("summary", "当次未计算")
                               for category in ("content", "structure", "format", "layout")] if full_report else []))
        types = book.create_sheet("报表类型覆盖")
        types.append(["归类", "类型代码", "名称", "旧版页", "新版页"])
        for category, entries in run.get("type_inventory", {}).items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                types.append([presence_labels.get(category, category), entry.get("type_code"), entry.get("title"),
                              ", ".join(map(str, entry.get("old_pages", []))), ", ".join(map(str, entry.get("new_pages", [])))])
        pending = book.create_sheet("提取与配对问题")
        pending.append(["问题类型", "说明", "影响报表", "版本", "页", "来源", "处理入口"])
        for issue in run.get("issues", []):
            pending.append([issue.get("type"), issue.get("message"), ", ".join(issue.get("report_ids", [])),
                            issue.get("side"), ", ".join(map(str, issue.get("pages", []))),
                            "；".join(source_label(source) for source in issue.get("sources", [])),
                            ", ".join(issue.get("actions", []))])
        if full_report:
            for category in ("content", "structure", "format", "layout"):
                category_sheets[category] = book.create_sheet(f"{category_labels[category]}差异")
                category_sheets[category].append(header)
            validation = book.create_sheet("属性验证范围")
            validation.append(["报表编号", "报表名称", "类别", "状态", "变化数", "待核验数", "摘要", "限制与未支持属性"])
            page_pairs = book.create_sheet("原页对应")
            page_pairs.append(["报表编号", "报表名称", "旧版物理页", "新版物理页", "配页状态", "依据"])
            paragraph_evidence = None
            if any("paragraph_checks" in report for report in run["reports"]):
                paragraph_evidence = book.create_sheet("段落测量证据")
                paragraph_evidence.append(["报表编号", "报表名称", "属性", "状态", "旧侧结果", "新侧结果", "旧位置", "新位置", "依据与限制", "原始测量证据"])
            dimensions_labels = {"changed": "存在差异", "unchanged": "已比较属性未发现变化", "partial": "部分已验证",
                                 "unverified": "待确认／未验证", "not_applicable": "不适用"}
            for report in run["reports"]:
                for category in ("content", "structure", "format", "layout"):
                    dimension = report.get("dimensions", {}).get(category, {})
                    validation.append([report.get("type_code"), report.get("title"), category_labels[category],
                                       dimensions_labels.get(dimension.get("status"), "当次未计算"), dimension.get("changes"), dimension.get("issues"),
                                       dimension.get("summary"), "；".join(dimension.get("limitations", []))])
                for pair in report.get("page_pairs", []):
                    page_pairs.append([report.get("type_code"), report.get("title"), pair.get("old_page"), pair.get("new_page"),
                                       "已对应" if pair.get("status") == "confirmed" else "待确认", pair.get("basis")])
                for check in report.get("paragraph_checks", []):
                    paragraph_evidence.append([
                        report.get("type_code"), report.get("title"),
                        {"line_spacing": "实际段落行距", "alignment": "左／中／右对齐"}.get(check.get("attribute"), check.get("attribute")),
                        dimensions_labels.get(check.get("status"), "待确认／未验证"), check.get("old_value"), check.get("new_value"),
                        source_label(check.get("old_source")), source_label(check.get("new_source")), check.get("message"),
                        encode(check.get("measurements", {}))])
        else:
            reliable = book.create_sheet("可靠共有报表内容差异")
            reliable.append(header)
        scopes = book.create_sheet("非报表文档范围")
        scopes.append(["版本", "范围类别", "物理页", "依据", "来源"])
        scope_labels = {"blank_page": "空白或仅页码", "document_section": "文档正文", "continuation_note": "报表续注", "unclassified_region": "未确定文档区域"}
        for region in run.get("non_report_regions", []):
            scopes.append(["旧版" if region.get("side") == "old" else "新版",
                           scope_labels.get(region.get("type"), region.get("type")),
                           ", ".join(map(str, region.get("pages", []))), region.get("basis"), source_label(region.get("source"))])
    elif job.get("kind") == "pdf":
        summary.append(["报表完整性", "该历史结果尚无业务报表完整性判定；请重新提取后比较"])
    with store.connect() as db:
        for (payload,) in db.execute("SELECT payload FROM diffs WHERE run_id=? ORDER BY seq", (run_id,)):
            d = json.loads(payload)
            row = [d["kind"], d["table_name"], d["row_key"], d["field"], d["old_value"], d["new_value"],
                   d["delta"], source_label(d["old_source"]), source_label(d["new_source"]), d["message"]]
            if full_report:
                row = [category_labels.get(d.get("category"), "待确认"), "待核验" if d["severity"] == "issue" else "存在差异", *row]
            detail.append(row)
            if d["severity"] == "issue":
                issues.append(row)
            if reliable is not None and d.get("layer") == "content" and d["severity"] != "issue":
                reliable.append(row)
            if d.get("category") in category_sheets:
                category_sheets[d["category"]].append(row)
    rules = book.create_sheet("范围与规则")
    rules.append(["类别", "设置"])

    def append_rule(path, value):
        if isinstance(value, dict) and value:
            for key, child in value.items():
                append_rule(f"{path}.{key}", child)
        elif isinstance(value, list) and value:
            for index, child in enumerate(value):
                append_rule(f"{path}[{index}]", child)
        else:
            text = encode(value)
            if len(text) <= 30000:
                rules.append([path, text])
            else:
                parts = [text[i:i + 30000] for i in range(0, len(text), 30000)]
                for index, part in enumerate(parts, 1):
                    rules.append([f"{path}（片段 {index}/{len(parts)}，按顺序拼接）", part])

    for name in ("config", "coverage"):
        append_rule(name, run[name])
    for name in ("reports", "issues", "type_inventory", "non_report_regions"):
        if name in run:
            append_rule(name, run[name])
    if job.get("kind") == "pdf":
        revision = run.get("review_revision", 0)
        summary.append(["OCR 核验修订号", revision])
        if job.get("parent_job_id"):
            summary.append(["重新提取来源任务", job["parent_job_id"]])
            summary.append(["历史修订说明", "原任务的人工修订及历史结果仍保留；重新提取后的区域需重新核验，未自动迁移旧坐标修订"])
        history = book.create_sheet("OCR修改记录")
        history.append(["修订", "版本", "操作", "页面", "表", "单元格", "原始文本", "修改前", "修改后", "原因", "时间"])
        _, events = store.reviews(job["id"], revision)
        for event in events:
            history.append([event.get(key, "") for key in ("revision", "side", "action", "page", "sheet", "cell",
                                                            "original_text", "before_text", "after_text", "reason", "created_at")])
        structures = [event for event in events if event["action"] == "table_update"]
        if structures:
            structural_detail = book.create_sheet("结构修改明细")
            structural_detail.append(["修订", "版本", "页面", "表", "阶段", "坐标", "当前文本", "原始识别文本", "稳定标识", "来源", "原因", "时间"])
            for event in structures:
                for field, phase in (("table_before", "修改前"), ("table_after", "修改后")):
                    snapshot = event[field]
                    for row_index, row in enumerate(snapshot["rows"], 1):
                        for column_index, cell in enumerate(row, 1):
                            source = {**cell.get("source", {}), "sheet": snapshot["sheet"]}
                            structural_detail.append([event["revision"], event["side"], event["page"], snapshot["sheet"],
                                                      phase, f"{get_column_letter(column_index)}{row_index}", cell["text"],
                                                      cell.get("original_text", ""), cell["id"], source_label(source),
                                                      event["reason"], event["created_at"]])
    for sheet in book:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for row in sheet:
            for cell in row:
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


def create_app(data_dir: Path | None = None):
    store = Store(data_dir or Path(os.environ.get("REPORTCONTRAST_DATA_DIR", str(ROOT / ".data"))))
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="report-import")

    @asynccontextmanager
    async def lifespan(_app):
        store.recover_imports()
        try:
            yield
        finally:
            await run_in_threadpool(executor.shutdown, wait=True)

    app = FastAPI(title="报表对比", version="0.2.0", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            allowed = {request.headers.get("host"), "localhost:5173", "127.0.0.1:5173"}
            if origin and urlsplit(origin).netloc not in allowed:
                return JSONResponse({"detail": "请从本机报表对比页面发起操作"}, status_code=403)
            try:
                if int(request.headers.get("content-length", "0")) > MAX_FILE_BYTES * 2 + 1024 * 1024:
                    return JSONResponse({"detail": "上传文件超过大小限制"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "请求大小无效"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def input_error(_request, exc):
        errors = [f"{'.'.join(str(x) for x in e['loc'] if x != 'body')}: {e['msg']}" for e in exc.errors()]
        return JSONResponse({"detail": "输入配置有误：" + "；".join(errors)}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(_request, exc):
        logger.exception("Request failed", exc_info=exc)
        return JSONResponse({"detail": "处理失败，请检查文件并重试；详细错误已记录在本机服务日志"}, status_code=500)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.2.0"}

    @app.post("/api/jobs", status_code=201)
    async def upload(old_file: UploadFile = File(...), new_file: UploadFile = File(...), background: bool = False):
        old_kind = Path(old_file.filename or "").suffix.lower().lstrip(".")
        new_kind = Path(new_file.filename or "").suffix.lower().lstrip(".")
        if old_kind not in ("xlsx", "pdf") or old_kind != new_kind:
            await old_file.close()
            await new_file.close()
            raise HTTPException(400, "请选择两份同类型文件：两份 .xlsx 或两份 .pdf")
        job_id = uuid.uuid4().hex
        folder = store.folder / job_id
        folder.mkdir()
        try:
            files = {"old": await save_upload(old_file, folder / f"old.{old_kind}"),
                     "new": await save_upload(new_file, folder / f"new.{old_kind}")}
            for file in files.values():
                file["kind"] = old_kind
            if background:
                now = datetime.now(timezone.utc).isoformat()
                status = {"id": job_id, "status": "queued", "created_at": now, "updated_at": now,
                          "files": {side: {"name": file["name"]} for side, file in files.items()},
                          "progress": {"side": None, "file_index": 0, "file_count": 2, "stage": "queued",
                                       "unit": None, "current": 0, "total": None, "completed": 0}}
                with store.connect() as db:
                    db.execute("INSERT INTO imports VALUES (?,?,?)", (job_id, "queued", encode(status)))
                executor.submit(process_import, store, job_id, files)
                return JSONResponse(status, status_code=202)
            return await run_in_threadpool(ingest, store, job_id, files)
        except Exception:
            shutil.rmtree(folder)
            raise
        finally:
            await old_file.close()
            await new_file.close()

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return store.current_job(job_id)

    @app.post("/api/jobs/{job_id}/reprocess", status_code=202)
    async def reprocess(job_id: str):
        original = store.job(job_id)
        if original.get("kind") != "pdf":
            raise ValueError("仅 PDF 任务支持重新提取")
        new_id = uuid.uuid4().hex
        folder = store.folder / new_id
        folder.mkdir()
        files = {}
        try:
            for side in ("old", "new"):
                source = store.folder / job_id / f"{side}.pdf"
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if digest != original["files"][side]["sha256"]:
                    raise ValueError("原件校验值与导入记录不一致，请检查原文件；未开始重新提取")
                shutil.copyfile(source, folder / f"{side}.pdf")
                files[side] = {key: original["files"][side][key] for key in ("name", "sha256", "size")}
                files[side]["kind"] = "pdf"
            now = datetime.now(timezone.utc).isoformat()
            status = {"id": new_id, "status": "queued", "parent_job_id": job_id,
                      "created_at": now, "updated_at": now,
                      "files": {side: {"name": file["name"]} for side, file in files.items()},
                      "progress": {"side": None, "file_index": 0, "file_count": 2, "stage": "queued",
                                   "unit": None, "current": 0, "total": None, "completed": 0}}
            with store.connect() as db:
                db.execute("INSERT INTO imports VALUES (?,?,?)", (new_id, "queued", encode(status)))
            executor.submit(process_import, store, new_id, files, job_id)
            return status
        except Exception:
            shutil.rmtree(folder)
            raise

    @app.get("/api/imports/{import_id}")
    def import_status(import_id: str):
        return store.import_status(import_id)

    @app.get("/api/jobs/{job_id}/columns")
    def columns(job_id: str, side: Literal["old", "new"], sheet: str,
                header_start: int = Query(1, ge=1), header_end: int = Query(1, ge=1),
                cell_range: str | None = Query(None, alias="range"), review_revision: int | None = Query(None, ge=0)):
        _, events = store.reviews(job_id, review_revision)
        book = store.reviewed_book(job_id, side, events) if store.job(job_id).get("kind") == "pdf" else store.workbook(job_id, side)
        return sheet_columns(book, sheet, header_start, header_end, cell_range or None)

    @app.get("/api/jobs/{job_id}/review")
    def review(job_id: str, side: Literal["old", "new"], page: int | None = Query(None, ge=1),
               offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        if store.job(job_id).get("kind") != "pdf":
            raise ValueError("仅 PDF 任务支持 OCR 校正与核验")
        revision, events = store.reviews(job_id)
        book = store.reviewed_book(job_id, side, events)
        items = [item for item in review_catalog(book) if page is None or item["page"] == page]
        return {"revision": revision,
                "structure_revision": structure_revision(events),
                "items": items[offset:offset + limit], "total": len(items),
                "offset": offset, "limit": limit, "pages": book["pages"]}

    @app.get("/api/jobs/{job_id}/review-tables")
    def review_tables(job_id: str, side: Literal["old", "new"], page: int | None = Query(None, ge=1),
                      sheet: str | None = None):
        if store.job(job_id).get("kind") != "pdf":
            raise ValueError("仅 PDF 任务支持 OCR 表格结构调整")
        revision, events = store.reviews(job_id)
        book = store.reviewed_book(job_id, side, events)
        tables = [item for item in structure_tables(book) if page is None or item["page"] == page]
        result = {"revision": revision,
                  "structure_revision": structure_revision(events),
                  "tables": tables}
        if sheet is not None:
            table = structure_table(book, sheet)
            if page is not None and page != table["page"]:
                raise ValueError("目标物理表与页面不一致")
            result["table"] = table
        return result

    @app.post("/api/jobs/{job_id}/review")
    async def save_review(job_id: str, action: ReviewAction):
        return await run_in_threadpool(store.save_review, job_id, action)

    @app.get("/api/jobs/{job_id}/review-history")
    def review_history(job_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        revision, events = store.reviews(job_id)
        return {"revision": revision, "items": list(reversed(events))[offset:offset + limit],
                "total": len(events), "offset": offset, "limit": limit}

    @app.get("/api/jobs/{job_id}/runs")
    def runs(job_id: str):
        store.job(job_id)
        with store.connect() as db:
            rows = db.execute("SELECT metadata FROM runs WHERE job_id=? ORDER BY rowid DESC", (job_id,)).fetchall()
        return {"items": [json.loads(row[0]) for row in rows]}

    @app.post("/api/jobs/{job_id}/runs", status_code=201)
    async def compare(job_id: str, config: CompareConfig):
        store.job(job_id)
        return await run_in_threadpool(execute_comparison, store, job_id, config.model_dump())

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        return store.run(run_id)

    @app.get("/api/runs/{run_id}/diffs")
    def differences(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                    kind: str = "all", q: str = "", report_id: str | None = None,
                    layer: Literal["all", "completeness", "content", "structure", "format", "layout", "uncertainty"] = "all",
                    category: Literal["all", "completeness", "content", "structure", "format", "layout"] = "all"):
        store.run(run_id)
        clauses = ["run_id=?"]
        args = [run_id]
        if kind != "all":
            clauses.append("kind=?")
            args.append(kind)
        if q:
            clauses.append("instr(search,?)>0")
            args.append(q.strip().casefold())
        if report_id:
            clauses.append("json_extract(payload, '$.report_id')=?")
            args.append(report_id)
        if layer != "all":
            clauses.append("json_extract(payload, '$.layer')=?")
            args.append(layer)
        if category != "all":
            clauses.append("json_extract(payload, '$.category')=?")
            args.append(category)
        where = " AND ".join(clauses)
        with store.connect() as db:
            total = db.execute(f"SELECT count(*) FROM diffs WHERE {where}", args).fetchone()[0]
            rows = db.execute(f"SELECT payload FROM diffs WHERE {where} ORDER BY seq LIMIT ? OFFSET ?",
                              [*args, limit, offset]).fetchall()
        return {"items": [json.loads(r[0]) for r in rows], "total": total, "offset": offset, "limit": limit}

    @app.get("/api/jobs/{job_id}/sheet-region")
    def region(job_id: str, side: Literal["old", "new"], sheet: str,
               start_row: int = Query(1, ge=1), start_column: int = Query(1, ge=1),
               rows: int = Query(25, ge=1, le=100), columns: int = Query(10, ge=1, le=50)):
        return sheet_region(store.workbook(job_id, side), sheet, start_row, start_column, rows, columns)

    @app.get("/api/jobs/{job_id}/files/{side}")
    def original(job_id: str, side: Literal["old", "new"]):
        job = store.job(job_id)
        kind = job.get("kind", "xlsx")
        return FileResponse(store.folder / job_id / f"{side}.{kind}", filename=job["files"][side]["name"],
                            media_type="application/pdf" if kind == "pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/jobs/{job_id}/pages/{side}/{page}.png")
    def pdf_page(job_id: str, side: Literal["old", "new"], page: int, dpi: int = Query(144)):
        if dpi not in (144, 288):
            raise HTTPException(422, "页面分辨率仅支持 144 或 288 dpi")
        job = store.job(job_id)
        pages = job["files"][side].get("pages", [])
        if job.get("kind") != "pdf" or page < 1 or page > len(pages):
            raise HTTPException(404, "找不到此 PDF 页面")
        path = store.folder / job_id / f"{side}-pages" / f"page-{page}.png"
        if not path.is_file():
            raise HTTPException(404, "页面图像不可用，请重新导入文件")
        if dpi == 288:
            path = path.with_name(f"page-{page}-288dpi.png")
            with _RENDER_LOCK:
                if not path.is_file():
                    with pdfplumber.open(store.folder / job_id / f"{side}.pdf") as document:
                        original_page = document.pages[page - 1]
                        area = float(original_page.width) * float(original_page.height)
                        resolution = min(dpi, 72 * math.sqrt(MAX_PAGE_PIXELS / area))
                        image = original_page.to_image(resolution=resolution, antialias=True, force_mediabox=True).original
                        temporary = path.with_suffix(".tmp")
                        image.save(temporary, format="PNG")
                        temporary.replace(path)
        return FileResponse(path, media_type="image/png")

    @app.get("/api/examples/pdf/{mode}/{side}")
    def pdf_example(mode: Literal["text", "scan"], side: Literal["old", "new"]):
        path = ROOT / "examples" / "pdf" / mode / f"{side}.pdf"
        if not path.is_file():
            raise HTTPException(404, "PDF 示例文件尚未生成")
        return FileResponse(path, filename=f"example-{mode}-{side}.pdf", media_type="application/pdf")

    @app.get("/api/runs/{run_id}/export")
    def export(run_id: str):
        buffer = export_report(store, run_id)
        return StreamingResponse(buffer, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": 'attachment; filename="report-differences.xlsx"'})

    @app.get("/api/examples/{side}")
    def example(side: Literal["old", "new"]):
        path = ROOT / "examples" / f"{side}.xlsx"
        if not path.is_file():
            raise HTTPException(404, "示例文件尚未生成")
        return FileResponse(path, filename=f"example-{side}.xlsx")

    dist = ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        @app.get("/")
        def frontend_pending():
            return JSONResponse({"detail": "前端尚未构建，请运行 ./run.sh 完成构建并启动"}, status_code=503)
    return app
