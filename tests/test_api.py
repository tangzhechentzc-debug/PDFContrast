import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

import backend.app as app_module
from backend.app import create_app


def xlsx_bytes(rows, title="明细"):
    book = Workbook()
    sheet = book.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as test_client:
        yield test_client


def upload(client, old, new):
    response = client.post("/api/jobs", files={
        "old_file": ("旧版.xlsx", old), "new_file": ("新版.xlsx", new)})
    assert response.status_code == 201, response.text
    return response.json()


def config(job):
    pairs = job["suggested_pairs"]
    for pair in pairs:
        pair["mode"] = "key"
        for column in pair["columns"]:
            column["is_key"] = column["old_column"] == "A"
    return {"pairs": pairs, "absolute_tolerance": "0", "ignore_outer_whitespace": True,
            "compare_formulas": True, "compare_formatting": False}


def test_upload_compare_locate_and_original_hash(client):
    old = xlsx_bytes([["编号", "金额"], ["001", 12000], ["002", 300]])
    new = xlsx_bytes([["编号", "金额"], ["002", 300], ["001", 12800]])
    job = upload(client, old, new)
    assert job["files"]["old"]["sha256"] == hashlib.sha256(old).hexdigest()
    response = client.post(f"/api/jobs/{job['id']}/runs", json=config(job))
    assert response.status_code == 201, response.text
    run = response.json()
    result = client.get(f"/api/runs/{run['id']}/diffs").json()
    value_diffs = [d for d in result["items"] if d["kind"] == "value_changed"]
    assert len(value_diffs) == 1
    difference = value_diffs[0]
    assert difference["old_source"]["cell"] == "B2"
    assert difference["new_source"]["cell"] == "B3"
    assert float(difference["delta"]) == 800
    region = client.get(f"/api/jobs/{job['id']}/sheet-region", params={
        "side": "new", "sheet": "明细", "start_row": 2, "start_column": 1})
    assert region.status_code == 200
    target = next(c for c in region.json()["cells"] if c["address"] == "B3")
    assert str(target["value"]) in ("12800", "12800.0")
    assert client.get(f"/api/jobs/{job['id']}/files/old").content == old
    assert client.get(f"/api/jobs/{job['id']}/files/new").content == new


def test_all_differences_pagination_search_and_safe_export(client):
    old = xlsx_bytes([["编号", "金额"]] + [[f"K{i:04d}", 100] for i in range(125)])
    new = xlsx_bytes([["编号", "金额"]] + [[f"K{i:04d}", 101] for i in range(125)])
    job = upload(client, old, new)
    run = client.post(f"/api/jobs/{job['id']}/runs", json=config(job)).json()
    first = client.get(f"/api/runs/{run['id']}/diffs", params={"limit": 100}).json()
    second = client.get(f"/api/runs/{run['id']}/diffs", params={"offset": 100, "limit": 100}).json()
    assert first["total"] == 125
    assert len(first["items"]) == 100 and len(second["items"]) == 25
    assert not {x["id"] for x in first["items"]} & {x["id"] for x in second["items"]}
    match = client.get(f"/api/runs/{run['id']}/diffs", params={"q": "K0124"}).json()
    assert match["total"] == 1
    response = client.get(f"/api/runs/{run['id']}/export")
    assert response.status_code == 200
    report = load_workbook(BytesIO(response.content))
    assert report["全部差异"].max_row == 126
    assert all(cell.data_type != "f" for sheet in report for row in sheet for cell in row)


def test_formula_strings_remain_text_in_export(client):
    old = xlsx_bytes([["编号", "金额"], ["X", "=1+1"]])
    new = xlsx_bytes([["编号", "金额"], ["X", "=2+1"]])
    job = upload(client, old, new)
    run_response = client.post(f"/api/jobs/{job['id']}/runs", json=config(job))
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()
    report = load_workbook(BytesIO(client.get(f"/api/runs/{run['id']}/export").content))
    strings = [c for row in report["全部差异"] for c in row if c.value == "=1+1"]
    assert strings and all(c.data_type == "s" for c in strings)
    assert run["summary"]["issues"] > 0


def test_reject_bad_file_and_foreign_origin(client):
    book = xlsx_bytes([["编号", "金额"], ["1", 2]])
    response = client.post("/api/jobs", files={"old_file": ("old.pdf", b"%PDF"),
                                               "new_file": ("new.xlsx", book)})
    assert response.status_code == 400
    response = client.post("/api/jobs", files={"old_file": ("old.xlsx", b"not-an-excel-file"),
                                               "new_file": ("new.xlsx", book)})
    assert response.status_code == 422
    assert client.post("/api/jobs", headers={"Origin": "https://example.org"}).status_code == 403
    assert client.get("/api/jobs/not-found").status_code == 404


def test_saved_job_and_run_survive_new_app(tmp_path):
    data = tmp_path / "persistent"
    old = xlsx_bytes([["编号", "金额"], ["1", 10]])
    new = xlsx_bytes([["编号", "金额"], ["1", 20]])
    with TestClient(create_app(data)) as first:
        job = upload(first, old, new)
        run = first.post(f"/api/jobs/{job['id']}/runs", json=config(job)).json()
    with TestClient(create_app(data)) as second:
        assert second.get(f"/api/jobs/{job['id']}").json()["id"] == job["id"]
        assert second.get(f"/api/runs/{run['id']}/diffs").json()["total"] == 1


def test_export_preserves_large_configuration(client):
    headers = ["编号"] + [f"字段{i:03d}" for i in range(1, 400)]
    old = xlsx_bytes([headers, ["row-1"] + list(range(1, 400))])
    new = xlsx_bytes([headers, ["row-1"] + list(range(2, 401))])
    job = upload(client, old, new)
    response = client.post(f"/api/jobs/{job['id']}/runs", json=config(job))
    assert response.status_code == 201, response.text
    run = response.json()
    report = load_workbook(BytesIO(client.get(f"/api/runs/{run['id']}/export").content))
    values = dict(report["范围与规则"].iter_rows(min_row=2, values_only=True))
    assert values["config.pairs[0].columns[399].name"] == '"字段399"'
    assert all(len(str(v)) <= 32767 for v in values.values())


def test_sheet_region_columns_limit(client):
    headers = [f"列{i}" for i in range(1, 33)]
    row = list(range(1, 33))
    book = xlsx_bytes([headers, row])
    job = upload(client, book, book)
    ok = client.get(f"/api/jobs/{job['id']}/sheet-region", params={
        "side": "old", "sheet": "明细", "start_row": 1, "start_column": 1, "rows": 5, "columns": 30,
    })
    assert ok.status_code == 200
    assert ok.json()["end_column"] == 30
    assert client.get(f"/api/jobs/{job['id']}/sheet-region", params={
        "side": "old", "sheet": "明细", "columns": 31,
    }).status_code == 422
    assert client.get(f"/api/jobs/{job['id']}/sheet-region", params={
        "side": "old", "sheet": "明细", "columns": 0,
    }).status_code == 422


def test_csrf_origin_policy_depends_on_frontend_dist(tmp_path, monkeypatch):
    book = xlsx_bytes([["编号", "金额"], ["1", 2]])

    def post_job(test_client, origin):
        return test_client.post(
            "/api/jobs",
            headers={"Origin": origin},
            files={"old_file": ("old.xlsx", book), "new_file": ("new.xlsx", book)},
        )

    # 1. 开发态（无 frontend/dist）：放行 localhost:5173 与 127.0.0.1:5173，拦截跨站 Origin
    dev_root = tmp_path / "dev_root"
    dev_root.mkdir()
    monkeypatch.setattr(app_module, "ROOT", dev_root)
    with TestClient(create_app(tmp_path / "dev_data")) as dev_client:
        assert post_job(dev_client, "http://localhost:5173").status_code == 201
        assert post_job(dev_client, "http://127.0.0.1:5173").status_code == 201
        assert post_job(dev_client, "https://example.org").status_code == 403

    # 2. 生产静态态（存在 frontend/dist）：拒绝 5173 跨端口 Origin，仅放行同源 Host
    prod_root = tmp_path / "prod_root"
    (prod_root / "frontend" / "dist").mkdir(parents=True)
    monkeypatch.setattr(app_module, "ROOT", prod_root)
    with TestClient(create_app(tmp_path / "prod_data")) as prod_client:
        assert post_job(prod_client, "http://localhost:5173").status_code == 403
        assert post_job(prod_client, "http://127.0.0.1:5173").status_code == 403
        assert post_job(prod_client, "http://testserver").status_code == 201


def test_run_sh_syntax_and_hash_skip_behavior(tmp_path):
    source_run_sh = app_module.ROOT / "run.sh"
    subprocess.run(["bash", "-n", str(source_run_sh)], check=True)

    sandbox = tmp_path / "sandbox"
    (sandbox / ".venv" / "bin").mkdir(parents=True)
    (sandbox / "bin").mkdir(parents=True)
    (sandbox / "frontend" / "src").mkdir(parents=True)
    shutil.copy2(source_run_sh, sandbox / "run.sh")
    (sandbox / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
    (sandbox / "frontend" / "package.json").write_text('{"name":"frontend"}\n', encoding="utf-8")
    (sandbox / "frontend" / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (sandbox / "frontend" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (sandbox / "frontend" / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (sandbox / "frontend" / "vite.config.ts").write_text("export default {};\n", encoding="utf-8")
    (sandbox / "frontend" / "src" / "main.tsx").write_text("console.log('v1');\n", encoding="utf-8")

    log_file = tmp_path / "calls.log"
    python_wrapper = sandbox / ".venv" / "bin" / "python"
    python_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \"${1:-}\" == \"-m\" ]] && [[ \"${2:-}\" == \"pip\" || \"${2:-}\" == \"uvicorn\" ]]; then\n"
        "  printf 'python %s\\n' \"$*\" >> \"$RUN_SH_LOG\"\n"
        "  exit 0\n"
        "fi\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python_wrapper.chmod(0o755)

    npm_stub = sandbox / "bin" / "npm"
    npm_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf 'npm %s\\n' \"$*\" >> \"$RUN_SH_LOG\"\n"
        "case \" $* \" in\n"
        "  *\" ci \"*)\n"
        "    mkdir -p frontend/node_modules\n"
        "    ;;\n"
        "  *\" run build \"*)\n"
        "    mkdir -p frontend/dist\n"
        "    printf '<html>dist</html>\\n' > frontend/dist/index.html\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    npm_stub.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{sandbox / 'bin'}:{os.environ['PATH']}",
        "RUN_SH_LOG": str(log_file),
    }

    def run_once():
        log_file.write_text("", encoding="utf-8")
        proc = subprocess.run(["bash", "run.sh"], cwd=sandbox, env=env, capture_output=True, text=True, check=True)
        calls = [line for line in log_file.read_text(encoding="utf-8").splitlines() if line]
        return calls, proc.stdout

    # 1. 冷启动：安装依赖、npm ci、npm run build 并启动 uvicorn
    calls_1, _ = run_once()
    assert any("python -m pip " in line for line in calls_1)
    assert any("npm ci --prefix frontend" in line for line in calls_1)
    assert any("npm run build --prefix frontend" in line for line in calls_1)
    assert any("python -m uvicorn " in line for line in calls_1)

    # 2. 二次运行且产物未变：跳过 pip、npm ci、npm run build
    calls_2, out_2 = run_once()
    assert calls_2 == ["python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8765"]
    assert "跳过 npm ci" in out_2 and "跳过 npm run build" in out_2

    # 修改构建中间文件 *.tsbuildinfo 不应触发重复构建
    (sandbox / "frontend" / "tsconfig.tsbuildinfo").write_text('{"version":"1"}\n', encoding="utf-8")
    calls_tsbuild, _ = run_once()
    assert calls_tsbuild == ["python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8765"]

    # 3. 修改前端源码：仅重建前端，跳过 pip 与 npm ci
    (sandbox / "frontend" / "src" / "main.tsx").write_text("console.log('v2');\n", encoding="utf-8")
    calls_3, _ = run_once()
    assert calls_3 == [
        "npm run build --prefix frontend",
        "python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8765",
    ]

    # 4. 修改 package-lock.json：触发 npm ci 与 npm run build，跳过 pip
    (sandbox / "frontend" / "package-lock.json").write_text('{"lockfileVersion":3,"rev":2}\n', encoding="utf-8")
    calls_4, _ = run_once()
    assert calls_4 == [
        "npm ci --prefix frontend",
        "npm run build --prefix frontend",
        "python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8765",
    ]

    # 5. 删除 dist/index.html：仅触发 npm run build
    (sandbox / "frontend" / "dist" / "index.html").unlink()
    calls_5, _ = run_once()
    assert calls_5 == [
        "npm run build --prefix frontend",
        "python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8765",
    ]

    # 6. 删除 frontend/node_modules：仅触发 npm ci（dist 仍最新）
    shutil.rmtree(sandbox / "frontend" / "node_modules")
    calls_6, _ = run_once()
    assert calls_6 == [
        "npm ci --prefix frontend",
        "python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8765",
    ]

