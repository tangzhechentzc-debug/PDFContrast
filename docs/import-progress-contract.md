# 导入与OCR进度

旧POST /api/jobs同步协议保留。新界面用 POST /api/jobs?background=true，文件上传完成后立即202返回ImportStatus，后台按顺序读取两份文件。GET /api/imports/{id}轮询；id与最终job id相同。

ImportStatus: `{id,status:'queued'|'processing'|'ready'|'failed',files:{old:{name},new:{name}},progress:{side:'old'|'new'|null,file_index:0|1|2,file_count:2,stage,unit:'page'|'sheet'|null,current:number,total:number|null,completed:number,item_name?:string},job_id?:string,error?:string,created_at,updated_at}`。
stage可为 queued/opening/rendering/text_extract/ocr/table_extract/reading_sheet/file_completed/finalizing/completed/failed。current表示正在处理的页/工作表，completed表示已完成数量；未知total用null而非虚构100%。切换文件重新从0开始。ready在完整job写入后才返回。failed保留最后file/current/total，显示错误。

后端提取函数新增可选 `progress=None` 回调（dict单参数），不传时保持行为/现有测试兼容：
- extract_pdf(path,asset_dir,progress=None)：打开后已知页数，opening(current=0,total=N,completed=0,unit=page)；每页渲染前rendering、文字读取text_extract、识别前ocr、结构提取table_extract；页完成后completed更新（stage可用table_extract）。识别失败被转为warning的页也算已处理，不伪称识别成功。最后file_completed current=N completed=N。
- extract_workbook(path,progress=None)：打开前opening(unit=sheet,total=null)，知晓sheet数后各sheet处理前reading_sheet(current=i,total=N,completed=i-1,item_name=name)，处理后completed=i，结束file_completed。
- 回调抛错不要被OCR失败catch误吞；除可选callback，不改变识别算法。

导入状态写入独立SQLite imports表。后台worker串行处理，页面轮询不阻塞。异常转failed，服务重启时未完成任务明确标为中断（若完整job已经写入则恢复ready）；保留原文件。

前端UploadPanel上传期间不显示伪进度；收到202后URL写?import=id并轮询0.5~1秒，显示旧/新版、真实文件名、第几份/2、当前阶段、第几页/共几页、已完成页数及页数进度条（不是时间百分比）。xlsx显示工作表计数和名字。轮询网络失败保留任务并可重试，不重复上传。终态ready再GET job并进入配置，failed可返回选文件重试。刷新带import参数可恢复。卸载停止轮询请求。App恢复流程兼容既有job/run URL，上传完成清理import参数。
