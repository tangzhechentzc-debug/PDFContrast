[CmdletBinding()]
param(
    [string]$Python = "py",
    [string]$IsccPath,
    [string]$VcRuntimeDir
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed (exit code $LASTEXITCODE)."
    }
}

if ($env:OS -ne "Windows_NT") { throw "Build on Windows x64." }
$repo = Split-Path -Parent $PSScriptRoot
$packaging = Join-Path $repo "packaging\windows"
$pythonArgs = if ($Python -eq "py") { @("-3.12-64") } else { @() }
Invoke-Checked $Python ($pythonArgs + @(
    "-c",
    "import platform, sys, tkinter; assert sys.platform == 'win32' and platform.machine() == 'AMD64' and sys.version_info[:2] == (3, 12) and not sys.executable.endswith('pythonw.exe'), 'Use Windows x64 Python 3.12 with Tcl/Tk'"
))
Invoke-Checked "node.exe" @("--version")
$npm = (Get-Command npm.cmd -CommandType Application).Source

if (-not $IsccPath) {
    $compiler = Get-Command ISCC.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($compiler) { $IsccPath = $compiler.Source }
    else { $IsccPath = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }
}
if (-not (Test-Path -LiteralPath $IsccPath -PathType Leaf)) {
    throw "Inno Setup 6.3+ is required. Supply -IsccPath with the path to ISCC.exe."
}
$IsccPath = (Resolve-Path -LiteralPath $IsccPath).Path

if (-not $VcRuntimeDir) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
        $runtime = & $vswhere -latest -products "*" -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find "VC\Redist\MSVC\**\x64\Microsoft.VC*.CRT\msvcp140.dll" |
            Sort-Object { [version]((Split-Path (Split-Path (Split-Path $_ -Parent) -Parent) -Parent) | Split-Path -Leaf) } -Descending |
            Select-Object -First 1
        if ($runtime) { $VcRuntimeDir = Split-Path -Parent $runtime }
    }
}
if (-not $VcRuntimeDir) {
    throw "Install the Visual Studio C++ desktop workload or supply -VcRuntimeDir (x64 Microsoft.VC*.CRT folder)."
}
foreach ($dll in @("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")) {
    if (-not (Test-Path -LiteralPath (Join-Path $VcRuntimeDir $dll) -PathType Leaf)) {
        throw "Missing $dll in $VcRuntimeDir. Use a current x64 Visual C++ CRT redistributable folder."
    }
}

$destination = Join-Path $repo "output\windows"
$work = Join-Path $destination ("build-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $work -Force | Out-Null
Write-Host "Build workspace: $work"
$previousCrt = $env:REPORTCONTRAST_BUILD_CRT_DIR
$env:REPORTCONTRAST_BUILD_CRT_DIR = (Resolve-Path -LiteralPath $VcRuntimeDir).Path
$succeeded = $false
Push-Location $repo
try {
    $venv = Join-Path $work "venv"
    Invoke-Checked $Python ($pythonArgs + @("-m", "venv", $venv))
    $buildPython = Join-Path $venv "Scripts\python.exe"
    Invoke-Checked $buildPython @("-m", "pip", "install", "--only-binary=:all:", "-r", (Join-Path $packaging "requirements-build.txt"))
    Invoke-Checked $buildPython @("-m", "pip", "check")

    Invoke-Checked $npm @("ci", "--prefix", "frontend", "--include=dev")
    Invoke-Checked $npm @("run", "build", "--prefix", "frontend")

    $dist = Join-Path $work "dist"
    Invoke-Checked $buildPython @("-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", $dist, "--workpath", (Join-Path $work "pyinstaller"),
        (Join-Path $packaging "ReportContrast.spec"))
    $bundle = Join-Path $dist "ReportContrast"
    foreach ($file in @(
        "ReportContrast.exe",
        "_internal\python312.dll",
        "_internal\_tkinter.pyd",
        "_internal\_tcl_data\init.tcl",
        "_internal\_tk_data\tk.tcl",
        "_internal\frontend\dist\index.html",
        "_internal\pypdfium2_raw\pdfium.dll",
        "_internal\onnxruntime\capi\onnxruntime_pybind11_state.pyd",
        "_internal\rapidocr_onnxruntime\config.yaml",
        "_internal\rapidocr_onnxruntime\models\ch_PP-OCRv4_det_infer.onnx",
        "_internal\rapidocr_onnxruntime\models\ch_ppocr_mobile_v2.0_cls_infer.onnx",
        "_internal\rapidocr_onnxruntime\models\ch_PP-OCRv4_rec_infer.onnx",
        "_internal\msvcp140.dll",
        "_internal\vcruntime140.dll",
        "_internal\vcruntime140_1.dll"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $bundle $file) -PathType Leaf)) {
            throw "Bundle is missing $file."
        }
    }

    Invoke-Checked $IsccPath @("/DAppBundle=$bundle", "/DSetupOutput=$work", (Join-Path $packaging "ReportContrast.iss"))
    $setup = Join-Path $destination "ReportContrast-Setup.exe"
    Copy-Item -LiteralPath (Join-Path $work "ReportContrast-Setup.exe") -Destination $setup -Force
    Get-FileHash -LiteralPath $setup -Algorithm SHA256 | Format-List Algorithm, Hash, Path
    $succeeded = $true
} finally {
    Pop-Location
    $env:REPORTCONTRAST_BUILD_CRT_DIR = $previousCrt
    if ($succeeded) { Remove-Item -LiteralPath $work -Recurse -Force }
}
