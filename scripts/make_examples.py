"""Create synthetic workbooks with known differences for local verification."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side


def make_example(destination, side):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "费用明细"
    sheet.merge_cells("A1:D1")
    sheet["A1"] = "项目费用报表 · 合成示例"
    sheet["A1"].font = Font(size=18, bold=True)
    sheet["A1"].alignment = Alignment(horizontal="center")
    sheet.row_dimensions[1].height = 32
    sheet.append([])
    for col, label in enumerate(["项目编码", "项目名称", "数量", "本期金额"], 1):
        cell = sheet.cell(3, col, label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="545E59")
    if side == "old":
        rows = [["A001", "设备采购", 2, 12000], ["A002", "临时租赁", 1, 3200],
                ["A003", "检验费用", 3, 860], ["0005", "精度核对", 1, 1.231]]
        formula, cached = "=SUM(D4:D7)", "16061.231"
    else:
        rows = [["A003", "检验费用", 3, 880], ["A004", "运输费用", 1, 700],
                ["A001", "设备采购", 2, 12800], ["0005", "精度核对", 1, 1.234],
                ["A006", "安装调试", 1, 500]]
        formula, cached = "=SUM(D4,D5,D6,D7,D8)", "14881.234"
    for row in rows:
        sheet.append(row)
    sheet.append(["TOTAL", "费用合计", None, formula])
    formula_address = f"D{sheet.max_row}"
    sheet.column_dimensions["A"].width = 18
    sheet.column_dimensions["B"].width = 26
    sheet.column_dimensions["C"].width = 12
    sheet.column_dimensions["D"].width = 23
    thin = Side(style="thin", color="A8AFAB")
    for row in sheet.iter_rows(min_row=3):
        for cell in row:
            cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            cell.alignment = Alignment(vertical="center")
        row[3].number_format = "#,##0.00"
    sheet.freeze_panes = "C4"
    hidden = workbook.create_sheet("历史记录")
    hidden.append(["记录编号", "说明"])
    hidden.append(["H001", "两版相同；此工作表已隐藏"])
    hidden.sheet_state = "hidden"
    workbook.save(destination)
    # These are explicit fixture caches, not a claim that openpyxl evaluates formulas.
    with ZipFile(destination) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(members["xl/worksheets/sheet1.xml"])
    cell = root.find(f".//s:c[@r='{formula_address}']", ns)
    cell.find("s:v", ns).text = cached
    members["xl/worksheets/sheet1.xml"] = ET.tostring(root)
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)


if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "examples"
    folder.mkdir(parents=True, exist_ok=True)
    for side in ("old", "new"):
        make_example(folder / f"{side}.xlsx", side)
    print(f"示例已生成：{folder}")

