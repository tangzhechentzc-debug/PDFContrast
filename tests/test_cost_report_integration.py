"""Real PDF extraction through business comparison, API filtering and export."""
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from reportlab.pdfgen import canvas

from backend.app import create_app, export_report
from scripts.make_pdf_examples import register_font


def cost_pdf(path, changed=False):
    font, _ = register_font(None)
    pdf = canvas.Canvas(str(path), pagesize=(1000, 700))
    amount = "120.00" if changed else "100.00"
    reports = [
        ("D.1 建设项目最高投标限价汇总表", "建设项目", ["序号", "单项工程名称", "金额(元)"],
         [["1", "一号楼", amount], ["", "合计", amount]]),
        ("D.2 单项工程最高投标限价汇总表", "一号楼", ["序号", "单位工程名称", "金额(元)"],
         [["1", "土建工程", amount], ["", "合计", amount]]),
        ("D.3 单位工程最高投标限价汇总表", "土建工程", ["序号", "汇总内容", "金额(元)"],
         [["1", "分部分项工程费", amount], ["2", "措施项目费", "0.00"],
          ["3", "不可竞争费", "0.00"], ["4", "其他项目", "0.00"], ["5", "税金", "0.00"]]),
        ("E.1 分部分项工程量清单计价表", "土建工程",
         ["序号", "项目编码", "项目名称", "项目特征描述", "计量单位", "工程量", "综合单价", "合价"],
         [["1", "001001", "混凝土", "C30", "m3", "12" if changed else "10", "10.0" if changed else "10.00", amount],
          ["2", "001002", "模板", "木模板", "m2", "2", "5", "10"]]),
    ]
    for title, name, headers, rows in reports:
        pdf.setFont(font, 14)
        pdf.drawString(40, 650, title)
        pdf.drawString(40, 620, "工程名称：" + name)
        pdf.setFont(font, 11)
        pdf.drawString(40, 595, "第1页 共1页")
        width = 920 / len(headers)
        bottom = 550 - 40 * (len(rows) + 1)
        for column in range(len(headers) + 1):
            x = 40 + width * column
            pdf.line(x, bottom, x, 550)
        for row in range(len(rows) + 2):
            y = 550 - row * 40
            pdf.line(40, y, 960, y)
        for row, values in enumerate([headers, *rows]):
            for column, value in enumerate(values):
                pdf.drawString(45 + column * width, 525 - row * 40, value)
        pdf.showPage()
    pdf.save()
    return path


def test_real_cost_pdf_api_business_diff_sources_and_export(tmp_path):
    paths = {side: cost_pdf(tmp_path / f"{side}.pdf", side == "new") for side in ("old", "new")}
    with TestClient(create_app(tmp_path / "store")) as client:
        response = client.post("/api/jobs", files={
            f"{side}_file": (path.name, path.read_bytes(), "application/pdf") for side, path in paths.items()})
        assert response.status_code == 201, response.text
        job = response.json()
        response = client.post(f"/api/jobs/{job['id']}/runs", json={"pairs": []})
        assert response.status_code == 201, response.text
        run = response.json()
        summaries = {r["type_code"]: r.get("business") for r in run["reports"]}
        assert set(summaries) == {"D.1", "D.2", "D.3", "E.1"}
        assert all(item and item["status"] == "changed" for item in summaries.values()), summaries
        response = client.get(f"/api/runs/{run['id']}/diffs", params={"category": "business", "limit": 100})
        assert response.status_code == 200, response.text
        diffs = response.json()["items"]
        quantity = next(d for d in diffs if d["field"] == "工程量")
        assert quantity["row_key"] == "001001"
        assert quantity["delta"] == "2"
        assert not any(d["field"] == "综合单价" for d in diffs)
        for side in ("old", "new"):
            source = quantity[f"{side}_source"]
            assert source["page"] == 4
            x0, y0, x1, y1 = source["bbox"]
            assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
            assert source["location_granularity"] in {"cell", "text"}
        export = load_workbook(BytesIO(export_report(client.app.state.store, run["id"]).getvalue()))
        assert export["业务数据差异"].max_row == len(diffs) + 1
        assert any(row[2] == "业务数据" for row in export["属性验证范围"].iter_rows(min_row=2, values_only=True))
