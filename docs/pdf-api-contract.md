# PDF 与 OCR 接口扩展

沿用本地任务、配置、持久化、分页和导出接口，保留旧版 Excel 任务兼容性。两份文件必须同为 `.xlsx` 或同为 `.pdf`。

## 提取与坐标

`Job.kind` 为 `xlsx` 或 `pdf`，旧任务缺省为 `xlsx`。PDF 的 `files[side].sheets` 表示提取出的表格或文字区域，仍使用现有列映射与抽取行号配置；这些抽取行列不是 PDF 页面坐标。

PDF 文件元数据额外包含 `pages: [{number,width,height,image_width,image_height,method}]`。

Source 保留 `sheet/cell` 以追踪抽取记录，并增加：

```json
{"kind":"pdf","page":2,"bbox":[0.1,0.2,0.4,0.25],"method":"ocr","confidence":0.98}
```

`bbox` 为对应页面 PNG 内的归一化左上坐标 `[x0,y0,x1,y1]`。图片和文字来源使用同一渲染几何；前端用百分比叠加矩形，缩放不改变坐标。与最初设计的 PDF.js 默认用户空间协议不同，本轮采用同源静态原页渲染，以避免浏览器 viewport 与后端坐标重复旋转；不将此坐标用于第三方 PDF 注释写入。

- `GET /api/jobs/{id}/pages/{old|new}/{page}.png` 返回该侧原页面渲染，页码从 1 开始。
- `GET /api/jobs/{id}/files/{side}` 返回原文件副本，PDF MIME 为 `application/pdf`。
- `GET /api/examples/pdf/{text|scan}/{old|new}` 返回合成测试样例。

## 识别状态

OCR 采用本机 RapidOCR / ONNX Runtime 执行 Paddle OCR 模型，模型由安装包携带，不向远端发送文件。这是对原设计 PP-StructureV3 重型管线的实现调整；规则网格表通过线条几何与文字框归属重建，没有可靠表格结构的区域必须保留结构限制提示。

`ocr_suspected` 表示按 OCR 值检出的疑似差异；`ocr_review` 表示包括同值内容在内的扫描核验入口；`extraction_unavailable` 表示提取失败或空提取，均为 `severity=issue`。高置信度不等于人工核验。

整页核验来源可省略 `sheet/cell`。两侧未提取出表格时允许 PDF `pairs=[]`，返回提取问题并保留原页浏览；Excel 仍要求至少一个范围。

含任意有效栅格图像的页面保守走整页 OCR，避免小图片正文被漏掉。含 OCR 的正文按水平行对齐，并仅在位置模式下忽略空白差异；比较使用副本，差异显示保留原始文本，覆盖信息披露此规则。

每侧来源独立，允许旧、新页码不同。缺少对应记录的侧不得借用另一侧的框。未提取、未比较范围不能写成整份文件一致。导出完整结果时同时保留页码、区域、规则与限制。

PDF 的数值比较和表格匹配复用 DataComPy/Decimal 引擎；PDF 不支持公式或 Excel 样式比较。原件保持只读。
