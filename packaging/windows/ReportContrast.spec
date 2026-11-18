# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

root = Path(SPECPATH).resolve().parents[1]
frontend = root / "frontend" / "dist"
datas = [(str(frontend / "index.html"), "frontend/dist"), (str(root / "LICENSE"), ".")]
datas += [(str(path), str(path.parent.relative_to(root)))
          for path in sorted((frontend / "assets").rglob("*")) if path.is_file()]
# Only the six committed synthetic documents exposed by the example API.
for directory, extension in (("examples", "xlsx"), ("examples/pdf/text", "pdf"),
                             ("examples/pdf/scan", "pdf")):
    datas += [(str(root / directory / f"{side}.{extension}"), directory) for side in ("old", "new")]

datas += collect_data_files("rapidocr_onnxruntime", includes=["config.yaml", "models/*.onnx"])
datas += collect_data_files("pdfminer", includes=["cmap/*"])
datas += collect_data_files("pypdfium2")
datas += collect_data_files("pypdfium2_raw")
binaries = collect_dynamic_libs("pypdfium2_raw") + collect_dynamic_libs("onnxruntime")
if sys.platform == "win32":
    crt_dir = Path(os.environ["REPORTCONTRAST_BUILD_CRT_DIR"])
    # App-local MSVC libraries are required by the CPU ONNX Runtime and other wheels.
    binaries += [(str(dll), ".") for dll in sorted(crt_dir.glob("*.dll"))]
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        datas += [(str(python_license), "python-license")]

analysis = Analysis(
    [str(Path(SPECPATH) / "launcher.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on"],
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="ReportContrast",
          console=False, upx=False)
app = COLLECT(exe, analysis.binaries, analysis.datas, name="ReportContrast", upx=False)
