# PDF 路径与链接导入实施计划

> **For agentic workers:** Implement the checked tasks in order, keeping each change focused and verifying its behavior before moving on.

**Goal:** 在“新解析”中支持本地文件、运行 PaperReader 的机器上的 PDF 路径、PDF 网址和 arXiv 链接，并通过“界面设置”控制路径/链接入口。

**Architecture:** 保留现有 `/api/upload` 文件上传。增加一个接收文本来源的导入 API：本地路径复制到 `data/uploads`，网址下载到 `data/uploads`，arXiv 摘要链接转为 PDF 下载地址；然后复用现有文档记录和解析队列。设置项仅控制文本来源入口，默认关闭；开启后“新解析”显示本地文件与路径/链接两种选择。

**Tech Stack:** FastAPI、requests、pytest、React、TypeScript、Vite。

**Spec:** 2026-09-27 用户在本对话中的需求；具体边界以仓库 `AGENTS.md` 为准。

## 约定

- “本地路径”指后端所在机器的文件路径；浏览器不能读取任意输入的本机路径。文件选择器仍通过现有上传 API 读取浏览器本机文件。
- 网址支持 `http://` 和 `https://` 的直达 PDF 地址；arXiv 支持 `arxiv.org/abs/...` 和 `arxiv.org/pdf/...`。下载发生在每次导入时，不复用旧文件。
- 新入口默认关闭，以保留当前“新解析”直接选择本地文件的行为；开启后仍可选择本地文件。
- 使用现有 `requests` 依赖；不增加新的运行时依赖、哈希或额外兼容层。
- 下载失败、路径不存在、来源不是 PDF 时返回明确错误，不建立文档记录，并删除未完成的目标文件。

## Task 1：保存界面开关

**Files:** `backend/app/services/app_settings.py`、`backend/app/api/routes_settings.py`、`frontend/src/lib/api.ts`、`frontend/src/components/SettingsModal.tsx`、`backend/tests/test_provider_settings.py`

- [x] 在 `AppSettings` 增加默认 `False` 的 `enable_source_links: bool`，接入 `update_settings()`、`serialize_settings()` 和 `/api/settings/me` 请求模型。
- [x] 在 `UserSettings` 中增加同名字段；“界面设置”加入开关，保存当前页时一并提交。
- [x] 增加设置 API 测试：默认关闭、开启后跨客户端保留、再次关闭生效；更新 `_SETTINGS_KEYS`。
- [x] 运行 `pytest -q backend/tests/test_provider_settings.py`，确认设置序列化及持久化正确。

## Task 2：导入路径和网址

**Files:** `backend/app/api/routes_upload.py`、`backend/tests/test_routes_upload.py`

- [x] 增加 `POST /api/import`，请求体为 `{ "source": "..." }`，响应沿用 `UploadResponse`。仅在 `enable_source_links` 打开时接受请求。
- [x] 复用现有模型配置和 worker 就绪检查。若 `source` 是本地路径，要求它指向现有 `.pdf` 文件，将内容复制到 `data/uploads`；若是 HTTP(S) 地址，则以 `requests` 流式下载。对 arXiv 摘要页先构造 PDF 地址。
- [x] 根据路径或 URL 生成安全的 `.pdf` 文件名；写入时检查 PDF 文件头，拒绝误返回的 HTML/非 PDF 内容。发生读取、网络或写入错误时清理目标文件；成功后调用 `create_document_record()`，排入 `_run_pipeline`。
- [x] 用隔离目录和模拟 HTTP 响应测试本地路径、直达 PDF、arXiv 摘要页、开关关闭、无效路径/URL、非 PDF 响应及下载中断；检查文档记录、实际字节和临时文件清理。
- [x] 运行 `pytest -q backend/tests/test_routes_upload.py`，确认现有文件上传未受影响。

## Task 3：接入“新解析”交互

**Files:** `frontend/src/pages/ReaderPage.tsx`、`frontend/src/components/Sidebar.tsx`、`frontend/src/components/SourceImportDialog.tsx`（新增）、`frontend/src/lib/api.ts`、`frontend/src/styles.css`

- [x] 在 API helper 增加 `importSource(source: string)`，发送 `POST /api/import`。
- [x] 关闭开关时保持“新解析”直接调起文件选择器；打开后弹出简单对话框，提供“选择本地 PDF”和路径/链接输入框。空白页入口使用同一交互。
- [x] 文本导入复用 `ReaderPage` 的 `uploading`、成功后选中新文档/刷新列表、失败提示和配置/worker 错误处理；请求期间禁用重复提交。
- [x] 对话框使用当前 modal 样式，提供关闭、Enter 提交及明确的来源示例；检查窄屏布局。
- [x] 运行 `npm --prefix frontend run build`，并手动检查开关前后文件选择、路径输入、URL/arXiv 输入以及失败提示。

## 最终验证

- [x] 运行 `pytest -q backend/tests`：发现设置或导入改动对其它后端行为的回归时修复。
- [x] 运行 `python -m compileall -q backend/app` 和 `npm --prefix frontend run build`：分别捕获 Python 语法与前端类型/打包错误。
