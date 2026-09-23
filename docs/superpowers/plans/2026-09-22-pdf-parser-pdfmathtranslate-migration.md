# PDFMathTranslate-next 迁移执行计划

> 生成日期：2026-09-22。本文档是给执行者的完整实施说明，目标仓库为 PaperReader
> （FastAPI 后端 `backend/app` + React/Vite 前端 `frontend/src` +
> 桌面打包 `desktop/`）。
> 已完成的调研结论全部写明，执行者无需重新调研，按清单逐项实施即可。

## 1. 目标与决策

把 PDF 解析、翻译和译文 PDF 排版统一交给一个独立的 PDFMathTranslate-next
worker，主 backend 不再直接导入 `pdf2zh_next` / BabelDOC。

已确定的产品决策：

- PDFMathTranslate-next 通过独立 worker/subprocess 运行；
- PDFMathTranslate-next 是唯一译文 PDF 排版器；
- 所有 PDF 都交给 PDFMathTranslate-next；
- 删除 SoMark、MinerU、本地 parser 选项及相关配置；
- 旧配置、旧产物和旧 checkpoint 全部清理；
- 旧文档重新解析，不保留旧 checkpoint 兼容层。

数据流：

```text
上传 PDF
→ PaperReader backend
→ PDFMathTranslate-next worker/subprocess
→ 翻译 PDF + 稳定的解析结果清单（manifest）
→ PaperReader 保存产物、目录、引用和阅读状态
```

## 2. 现状（已核实）

### 2.1 解析调用链

1. `POST /api/upload`（`backend/app/api/routes_upload.py:28-51`）校验 `.pdf`，
   `require_provider_settings(for_pdf=True)` 把关，存盘后由 BackgroundTasks 跑
   `process_document`。
2. parse 阶段在 `backend/app/services/document_pipeline.py:1138-1212` 按
   `settings.pdf_parser` 三分支：`somark` / `mineru` / 其他（本地）。
   云解析失败一律回退 `extract_structured_from_pdf_local()`。
3. 结果写 `extraction-checkpoint.json`（schema `pdf-extraction-v3`），供 retry/resume。
4. `_build_ir_and_frames` 优先用 `layout_payload`，否则用 `content_blocks`
   （`mineru_layout.blocks_to_ir`，`normalized_boxes=True`）。

### 2.2 译文 PDF 排版路径

`_translate_and_render`（`document_pipeline.py:856`）用
`layout_fit.plan_document` + `layout_render.render_document` 在原文页面上重排译文，
产出 `layout-plan.json` 与译文 PDF。这条路径整体退出主流程。

### 2.3 需要保留的阅读器能力

删除排版路径前必须确认仍被以下能力使用，并迁移到中立模块：

- 目录（outline）与图表列表：`document_structure.build_document_structure`
- 引用：`_extract_references_from_text` / `record.references`
- 定位与搜索：`alignment_service`、`routes_document` 的 alignment blocks、
  `routes_discovery` 的全文检索
- 注释 PDF：`annotation_render.render_annotated_pdf`
- 文档详情接口：`record.extracted_text` / `record.translated_text`

## 3. worker 边界

新增 `workers/pdfmathtranslate/`，主 backend 只做进程管理、进度映射、错误转换和
产物登记。

### 3.1 任务输入

```json
{
  "job_id": "document-id",
  "input_pdf": "/path/to/source.pdf",
  "output_dir": "/path/to/output",
  "work_dir": "/path/to/scratch",
  "translation": {"api_key": "...", "base_url": "...", "model": "..."},
  "options": {"output": "mono", "no_watermark": true, "debug": false},
  "glossary": "/path/to/domain-glossary.csv"
}
```

`work_dir` 由后端指定，worker 用后即清；`options.debug` 表示是否保留翻译器自己的
调试产物，不控制 manifest 的生成（manifest 始终生成）。

### 3.2 事件协议

worker 在标准输出上逐行输出 JSON 事件：

- `stage_summary`：将要执行的阶段清单及其权重
- `progress_start` / `progress_update` / `progress_end`：带 `stage`、`group`
  （`parse` / `translate` / `render`）、`progress`、`overall`、`current`、`total`
- `finish`：成功，携带产物路径
- `error`：失败，携带 `message` 与 `stage`

`finish` 至少返回：

```json
{
  "status": "finished",
  "translated_pdf": "...",
  "mono_pdf_path": "...",
  "no_watermark_mono_pdf_path": "...",
  "glossary_path": "...",
  "manifest_path": "...",
  "extraction_dir": "...",
  "debug_dir": "...",
  "mode_label": "...",
  "page_count": 9
}
```

后端按 `group` 把事件映射到 `parse` / `translate` / `render` 三个阶段；`stage` 保留
翻译器自己的阶段名用于展示。

### 3.3 产物布局

```text
data/outputs/<document_id>/
├── translated.pdf                 # worker 输出，后端按文档名发布
├── original.pdf                   # 源 PDF 副本
├── extraction/
│   ├── manifest.json              # 稳定清单，始终生成
│   ├── glossary.csv               # 翻译器抽取的术语，可能不存在
│   └── debug/                     # 仅在 options.debug 时保留
├── <stem>_Chinese_ver.pdf         # 发布的译文 PDF
├── <stem>_原文标注.pdf             # 注释 PDF
└── alignment.json                 # 双语对齐索引
```

### 3.4 worker 要求

- 固定 PDFMathTranslate-next `2.9.0` 与 BabelDOC `0.6.2`；
- 使用独立工作目录；
- 把需要持久化的 debug 输出复制到 `data/outputs/<document_id>/extraction/`；
- 不把临时目录路径写入长期记录；
- 对 API 错误、进程退出、超时、输出文件缺失返回明确错误；
- 支持取消和重试；
- 不记录 API key。

## 4. 中立的解析结果契约

`mineru_layout.py` 中仍有价值的 PaperReader IR 抽离为中立模块：

- `backend/app/services/document_ir.py`：IR 节点定义与 span/块工具；
- `backend/app/services/document_manifest.py`：manifest 解析、校验、
  由 manifest 生成 IR 与页面几何；
- `backend/app/services/pdf_extraction.py`：中立的提取结果对象。

供应商命名（`MinerUResult`）改为中立对象：

```python
@dataclass
class PdfTranslationResult:
    source_pdf: Path
    translated_pdf: Path
    manifest_path: Path
    glossary_path: Path | None
    extraction_dir: Path
    mode_label: str
```

manifest 由 worker 统一产出，PaperReader 下游只依赖 manifest，不读 BabelDOC 内部
JSON。manifest 至少包含：`schema_version`、`page_count`、页面尺寸、文本对象及
bbox、对象类型、原文和译文文本、公式/数字/URL/引用保护信息、图表与图注关系、
表格结构、逻辑对象 ID 与片段顺序。

坐标约定：bbox 为 PDF 文档坐标（原点在页面左下角），单位 point，页面尺寸取自
mediabox。`boxes_normalized: false` 声明的即为此约定。页内顺序按栏分组后自上而下，
即先比较左边界所在栏、再比较上边缘。

manifest 在 worker 侧完成，包括：

- `fallback_line` 之类的逐行度量产物不进入正文；落在表格区域内的行作为表格单元格保留；
- 译文里的 `{vN}` 富文本占位符与 `<style id='…'>` 标签在写入前剥离；
- 标题层级由 `doc_title` 与章节编号推导（`1.` 为二级、`3.1.` 为三级）；
- 参考文献由 “References / Bibliography” 标题定位，条目按 `[n]` 标记切分并与译文配对。

## 5. 流水线改造

`document_pipeline.py` 改为：

```text
upload
→ start_pdfmathtranslate_job
→ consume_worker_events
→ register translated PDF
→ save manifest/extraction metadata
→ build document structure
→ publish artifacts
```

移除：`SoMarkConfig`、`MinerUConfig`、`extract_structured_from_pdf_somark`、
`extract_structured_from_pdf`、`extract_structured_from_pdf_local`、
`parser == "somark" / "mineru" / "local"` 分支、本地 parser fallback、
`mineru` / `somark` 输出目录、`mineru_output` artifact 类型、
`_save_extraction_checkpoint` / `_load_extraction_checkpoint`。

保留并改造：上传、文档状态、后台任务、重试、取消、进度、译文 PDF artifact、
glossary artifact、extraction/manifest artifact、阅读进度、目录与引用、注释 PDF。

默认 `translated_pdf` 为 PDFMathTranslate-next 的无水印单语输出；双语输出只作为
额外 artifact，不引入第二套排版器。

## 6. 配置与 API

删除：`PDF_PARSER`、`SOMARK_*`、`MINERU_*`、`LAYOUT_DEBUG`。

新增 worker 配置：

| 变量 | 含义 |
| --- | --- |
| `PDFMATHTRANSLATE_WORKER` | worker 启动命令（开发环境覆盖） |
| `PDFMATHTRANSLATE_PYTHON` | 运行 worker 的解释器 |
| `PDFMATHTRANSLATE_VERSION` | 锁定的 PDFMathTranslate-next 版本 |
| `PDFMATHTRANSLATE_TIMEOUT` | 单次任务超时秒数 |
| `PDFMATHTRANSLATE_WORKING_DIR` | worker 临时工作目录根 |
| `PDFMATHTRANSLATE_DEBUG` | 是否保留 BabelDOC debug 输出 |
| `PDFMATHTRANSLATE_OUTPUT_MODE` | 输出模式（默认 `mono`） |
| `PDFMATHTRANSLATE_QPS` | 同时翻译的段落数（默认 `4`）；耗时几乎全在翻译阶段，与该值成反比 |

Provider settings 只保留：翻译 API key、翻译 base URL、翻译模型、视觉检查配置、
主题和阅读偏好、翻译领域、收藏项。

`/api/settings` 请求与响应删除所有 parser/provider 专用字段；上传接口不再要求选择
parser，只验证翻译服务配置和 worker 可用性。

前端删除 parser 三选一、SoMark 表单、MinerU 表单及其字段，保留统一的 PDF 翻译设置
入口。

## 7. 删除旧 IR renderer 依赖

`layout_render.py`、`layout_fit.py`、`layout_model.py` 整体退出主流程。
`annotation_render.py` 与 `document_structure.py` 改为只依赖 manifest 提供的
几何与结构化数据；`alignment_service.py` 改为直接用 manifest 的原文/译文对。

删除前逐项确认目录、图表列表、引用定位、阅读器搜索、注释 PDF、文档详情接口不再
引用被删符号。

## 8. 迁移和清理

一次性移除：

- `backend/app/services/somark_service.py`、`backend/app/services/mineru_service.py`
- SoMark/MinerU 专用测试
- SoMark/MinerU 配置字段
- 前端 parser/provider UI
- README 与开发文档中的旧配置
- 依赖旧 parser 名称的脚本
- requirements 中不再使用的依赖

清理运行数据：`settings.json` 中的 SoMark/MinerU 字段（`app_settings.purge_removed_keys()`
在启动时重写）；每个文档目录下的 `somark/`、`mineru/`、旧 `local/` 产物；
`extraction-checkpoint.json`；`translation-checkpoint.json`；`layout-plan.json`、
旧 renderer 调试产物与旧译文 PDF。

`scripts/cleanup_legacy_artifacts.py` 是一次性清理入口：先以只读方式列出将删除的
文件与将重置的文档，加 `--apply` 后执行。旧文档不做在线兼容迁移：保留源 PDF，
记录的产物与文本被清空，状态置为 `failed` 并给出「请重新处理」的失败原因，用户在
阅读器里 reprocess 即可。

## 9. 桌面打包

worker 依赖不混入主 backend 运行环境，作为发行包中的独立运行组件：

- macOS：worker Python 环境或冻结后的 worker executable
- Windows：worker executable 或独立 Python runtime
- 开发环境：`PDFMATHTRANSLATE_WORKER` 指定命令
- 打包环境：launcher 按 bundle root 解析 worker 路径

需要更新 `desktop/requirements-build.txt`、`desktop/PaperReader.spec`、
`desktop/setup_macos.py`、`desktop/build_macos.sh`、`desktop/build_portable.ps1`、
`desktop/launcher.py`、`desktop/README_macos_zh.md`、`desktop/README_zh.md`。

打包验证覆盖：worker 能启动；PDFMathTranslate-next 依赖可导入；字体资产可访问；
worker 能创建临时目录；译文 PDF 能回传主 backend；主窗口启动不受 worker 失败影响；
worker 失败时文档进入 `failed`，不停留在 `processing`。

## 10. 测试与验收

### 10.1 单元测试

worker 命令构造；JSON 事件解析；progress 映射；finish 产物校验；worker 超时；
worker 非零退出；缺失 PDF 输出；API key 不进入日志；manifest schema 校验；
extraction artifact 注册；retry 从解析阶段重新开始；删除旧 checkpoint 后重新解析；
文档结构从新 manifest 生成；glossary artifact 注册。删除所有直接构造
`MinerUResult` 的测试。

### 10.2 集成测试

用 `samples/` 的 6 篇论文验证：输出 PDF 可打开；页数与页面尺寸与原文一致；每页有
中文文本层；公式、图片、双栏正文可渲染；表格与算法区域进入人工复核清单；参考文献
与稀疏页面不被误判为空；译文 PDF、manifest、glossary 可通过文档接口下载；retry 不
读取旧 SoMark/MinerU checkpoint；`document_structure`、目录和引用接口可用；前端
阅读器能打开新译文 PDF。

### 10.3 仓库检查

```bash
python -m compileall backend/app
pytest
npm --prefix frontend run build
rg -n "SoMark|somark|MinerU|mineru|PDF_PARSER|pdf_parser" backend frontend scripts desktop docs
```

目标是生产代码、配置、前端设置和当前文档中不再存在 SoMark/MinerU 运行依赖；历史
迁移说明若保留，必须明确标记为历史内容。

## 11. 验收标准

- backend 不再导入 SoMark/MinerU；
- frontend 不再提供 parser 选择；
- 新上传 PDF 只经过 PDFMathTranslate-next worker；
- 译文 PDF 只由 PDFMathTranslate-next 生成；
- 不存在本地 parser fallback；
- 旧 checkpoint 不会被新流程读取；
- worker 错误能准确反映到文档状态；
- 6 篇样本全部生成可打开的译文 PDF；
- 目录、引用、搜索和阅读器基本功能继续可用；
- 桌面打包和开发启动路径都能找到 worker；
- 测试、编译和前端构建通过后，才可声称改造完成。

## 12. 假设

- PDFMathTranslate-next 固定为 `2.9.0`，BabelDOC 固定为 `0.6.2`；
- 翻译服务复用 PaperReader 当前的翻译 API 配置；
- 默认输出为无水印单语 PDF；
- 扫描 PDF 的 OCR 由 PDFMathTranslate-next 负责；
- 旧文档保留源 PDF 即可，旧解析缓存和旧译文不作为新流程输入；
- 分阶段实施：先建立 worker 和 manifest，再切换流水线，最后删除旧代码、配置、
  依赖和文档。
