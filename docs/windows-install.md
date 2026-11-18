# Windows x64 离线安装版

双击 `ReportContrast-Setup.exe` 按提示安装，再从开始菜单打开 **ReportContrast**。控制窗口出现后，程序会在本机服务就绪时打开默认浏览器，地址为 `http://127.0.0.1:8765/`。控制窗口可以最小化；再次从开始菜单打开会打开已有实例的页面。如果端口由其他程序占用，按提示关闭占用程序后重试。

退出时，在控制窗口点 **退出** 或关闭该窗口，等待其消失。程序会等待正在处理的本机任务结束并释放端口。关闭浏览器标签页不会停止服务。

安装程序按当前用户安装。默认程序目录是 `%LOCALAPPDATA%\Programs\ReportContrast`；上传原件、提取结果、SQLite（`tasks.sqlite3`）和比较历史在 `%LOCALAPPDATA%\ReportContrast`。日志为该数据目录下的 `logs\reportcontrast.log`。可在启动前设置用户环境变量 `REPORTCONTRAST_DATA_DIR`，使用程序安装目录之外的绝对路径；日志会跟随迁移到指定的目录，旧数据须自行复制。升级和默认卸载保留独立的数据目录。恢复历史时，重新打开原结果网址（含 `job` 和 `run` 参数）。

## 在 Windows x64 构建

仅构建机需要联网和下列工具：

- Windows 10/11 x64、标准 **Python 3.12 x64**（含 Tcl/Tk 及 `py` 启动器）、Node.js 22.12+ 与 npm。Python 3.13 不兼容仓库当前锁定的 RapidOCR 版本。
- **Inno Setup 6.3 或更新的 6.x**。
- Visual Studio 2022 或 Build Tools 的「使用 C++ 的桌面开发」工作负载。使用该安装提供的 [x64 Visual C++ CRT 可再发行 DLL](https://learn.microsoft.com/en-us/cpp/windows/choosing-a-deployment-method?view=msvc-170)；构建脚本把它们随应用部署。

在仓库根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-windows.ps1
Get-FileHash .\output\windows\ReportContrast-Setup.exe -Algorithm SHA256
```

若工具装在自定义位置，可加 `-Python 'C:\Path\python.exe'`、`-IsccPath 'C:\Path\ISCC.exe'` 或 `-VcRuntimeDir 'C:\Path\Redist\MSVC\14.xx.xxxxx\x64\Microsoft.VC143.CRT'`。脚本会新建独立 Python 环境，通过 `npm ci` 构建前端，再用 PyInstaller onedir 和 Inno Setup 制成单个安装包。每次成功构建输出到 **`output\windows\ReportContrast-Setup.exe`**；失败时保留脚本打印的构建工作目录。只发送该 EXE 即可；安装与运行不需要另装 Python、Node.js 或 OCR 模型。构建只打包生产前端、运行依赖和六份合成样例。

## 干净 Windows x64 的断网验收

使用干净的 Windows x64 虚拟机快照，标准用户、已设默认浏览器、无 Python、Node.js 和额外安装的 VC++ 再发行包。先复制 Setup.exe 并核对 SHA-256，再断开虚拟机网卡，保持断网到最后。只使用界面提供的合成样例。记录 Windows 版本、安装包哈希、每项实际结果、截图和本次应用日志。

| 场景 | 操作和预期 |
| --- | --- |
| 安装、首次启动、重复启动 | 双击 Setup.exe。开始菜单启动后，默认浏览器自动显示报表对比界面，健康检查返回 `status: ok`；地址为本机回环地址。再点开始菜单，应打开已有实例，只有一个服务监听。 |
| Excel | 下载并上传旧/新版 Excel 样例，费用明细选第 3 行表头、项目编码为键。执行比较，核对 A001 金额 12000 → 12800（旧 D4、新 D6），记录结果网址。 |
| 文本 PDF | 下载并上传旧/新版文本 PDF 样例，保留建议报表对应并比较。核对 A002 金额 1.231 → 1.234（差额 0.003），点击该差异确认能定位到两侧第 1 页的原表。 |
| 扫描 PDF | 换用旧/新版扫描样例。若没有建议配对，点击“添加完整报表配对”，对照标题和上下文，把该合成样例的两侧第 1 页对应、第 2 页对应，分别勾选人工确认并填写依据。执行比较，确认渲染出扫描原图，且文字内容中有 OCR 识别出的 1.231 → 1.234，仍须核验。只有整页占位或提取失败不算通过。 |
| XLSX 导出 | 在结果页点导出，确认下载的 `report-differences.xlsx` 有“摘要”“全部差异”“待核对”“范围与规则”工作表，且含已比较的金额记录。用 Excel/其他现有阅读器，或用下方 PowerShell 读取 XLSX 内的 XML。 |
| 退出和历史恢复 | 点控制窗口“退出”，等待窗口消失并确认没有 8765 监听。再次从开始菜单启动，打开之前保存的 Excel 结果网址；应恢复同一比较及差异。 |
| 端口冲突 | 退出后用下方的 TCP 监听命令占用 8765。尝试启动，预期提示端口占用，不打开占用者的页面。释放测试监听后正常启动。 |
| 覆盖安装和卸载 | 退出后再次运行同一路径的 Setup.exe，验收覆盖安装后原结果仍可打开。再退出，从“设置 → 应用 → 已安装的应用”卸载。开始菜单项和程序移除，但数据目录、上传文件、`tasks.sqlite3` 和日志仍在。可再次离线安装并打开原结果网址确认。 |

运行时和退出后分别检查监听，并在卸载前后检查数据（启动后才会生成）：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
$data = Join-Path $env:LOCALAPPDATA 'ReportContrast'
Get-ChildItem -LiteralPath $data
Get-FileHash (Join-Path $data 'tasks.sqlite3') -Algorithm SHA256
Get-Content (Join-Path $data 'logs\reportcontrast.log') -Encoding UTF8 -Tail 30
```

端口冲突场景（按 Enter 释放测试监听）：

```powershell
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 8765)
try { $listener.Start(); Read-Host 'Start ReportContrast from Start; then press Enter' }
finally { $listener.Stop() }
```

无 Office 时，使用 Windows PowerShell 读取导出的工作簿清单（需要时调整下载文件路径）。金额记录在对应差异表的 `xl/worksheets/sheet*.xml` 中：

```powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem
$xlsx = Join-Path $env:USERPROFILE 'Downloads\report-differences.xlsx'
$zip = [System.IO.Compression.ZipFile]::OpenRead($xlsx)
try {
    $reader = [System.IO.StreamReader]::new($zip.GetEntry('xl/workbook.xml').Open())
    try { ([xml]$reader.ReadToEnd()).workbook.sheets.sheet | Select-Object -ExpandProperty name }
    finally { $reader.Dispose() }
    $zip.Entries | Where-Object FullName -like 'xl/worksheets/sheet*.xml' | Select-Object FullName, Length
} finally { $zip.Dispose() }
```
