# SoMark PDF 解析接入执行计划

> 生成日期:2026-09-21。本文档是给执行者的完整实施说明,目标仓库为 PaperReader
> (FastAPI 后端 `backend/app` + React/Vite 前端 `frontend/src`)。
> 已完成的调研结论全部写明,执行者无需重新调研,按清单逐项实施即可。

## 1. 目标与决策

- `pdf_parser` 增加 `"somark"` 选项,并设为**默认**云解析后端。
- `"mineru"` 与 `"local"` **保留可选**(已明确:保留 MinerU 备选,不删除任何
  MinerU 代码/配置)。已存 `settings.json` 中 `pdf_parser:"mineru"` 的用户不受影响。
- 下游管线(IR 构建、checkpoint、翻译、渲染)**零改动**:SoMark 服务适配到现有
  `MinerUResult` DTO 和 MinerU `content_list_v2` 形状的 `content_blocks`。

## 2. 背景:现有解析管线(已核实)

### 2.1 调用链

1. 上传 `POST /api/upload`(`backend/app/api/routes_upload.py:28-51`):校验 `.pdf`,
   `require_provider_settings(for_pdf=True)` 把关(缺 key 返回 409
   `{code:"config_required"}`),存盘后由 FastAPI BackgroundTasks 跑
   `process_document`(`backend/app/services/document_pipeline.py:995`)。
2. parse 阶段在 `document_pipeline.py:1126-1168`:按 `settings.pdf_parser` 分支:
   - `"mineru"` → `extract_structured_from_pdf(...)` 输出到 `data/outputs/<id>/mineru/`,
     **任何异常**记日志后回退 `extract_structured_from_pdf_local()` 到 `.../local/`。
   - 否则 → 本地解析 `extract_structured_from_pdf_local()`。
3. 结果存 `extraction-checkpoint.json`(schema `pdf-extraction-v2`),供 retry/resume。
4. `_build_ir_and_frames`(`document_pipeline.py:437`)优先用 `layout_payload`,
   否则用 `content_blocks`(`mineru_layout.blocks_to_ir`,
   `backend/app/services/mineru_layout.py:417`,`normalized_boxes=True`)。

### 2.2 适配接缝(唯一需要对齐的接口)

`backend/app/services/mineru_service.py:559` 的 `extract_structured_from_pdf`:

```python
def extract_structured_from_pdf(
    pdf_path: str,
    output_dir: Path,
    log_sink=None,          # list-like,append 字符串即进用户可见日志
    progress_cb=None,       # callable(frac: float, label: str)
    config=None,            # MinerUConfig;None 时用 _default_config()
) -> MinerUResult
```

`MinerUResult`(`mineru_service.py:37-53`):

```python
@dataclass
class MinerUResult:
    markdown: str
    mode_label: str
    extracted_files: list[Path] = field(default_factory=list)
    content_blocks: list[dict] | None = None   # 逐页 list,见 2.3
    layout_payload: dict | None = None          # SoMark 不用,置 None
    boxes_normalized: bool = True               # 0-1000 归一化坐标
    images_dir: Path | None = None
    two_column: bool = False
```

### 2.3 `content_blocks` 形状(MinerU `content_list_v2.json` 风格)

外层是**逐页 list**:`content_blocks[page_index] = [block, block, ...]`。
每个 block:`{"type": str, "bbox": [x0,y0,x1,y1], "content": {...}}`,
bbox 为 **0–1000 归一化坐标**(相对页宽/高)。`blocks_to_ir`
(`mineru_layout.py:417-626`)消费的类型与字段:

| block `type` | `content` 字段 | 说明 |
|---|---|---|
| `title` | `title_content: [{"type":"text","content": str}]`,`level: int` | `_title_text` 取 text 项拼接 |
| `paragraph` | `paragraph_content: [{"type":"text","content": str}]` | 纯文本 run;行内公式以 `$…$` / `\(...\)` 留在文本里,由 `_split_text_at_math` 拆分 |
| `list` | `list_type: str`,`list_items: [{"item_content": [...]}]` | 可选,SoMark 无对应类型,不产出 |
| `equation_interline` | `math_content: str`(LaTeX,不含定界符) | 行间公式 |
| `image` / `chart` | `image_source.path: str`(相对 extract_dir 的路径如 `images/xx.jpg`),`image_caption: [{"type":"text","content": str}]`,`image_footnote` 可选 | 无 path 的 image block 会被丢弃 |
| `table` | `html: str`(或 `table_body`),`table_caption: [...]`,`table_footnote` 可选,`image_source.path` 可选 | `html` 或 `path` 至少其一 |

注意 `_flatten_pages_with_positions` 会把逐页 list 展开成 `(page_index, block)`
序列;论文标题前的 paragraph 会被当作页眉丢弃;`blocks_to_ir` 里
`_has_typed_math` 检测到 `equation_interline` 存在时,纯文本中的裸 `$` 不再当公式
定界符(视为货币),与 MinerU 行为一致,无需特殊处理。

### 2.4 配置流(需要加 SoMark 字段的每一环)

```
.env → backend/app/core/config.py  (pydantic Settings,mineru_* 在 39-47 行一带)
     → backend/app/services/app_settings.py
        _PARSERS = {"local","mineru"} (28 行)
        AppSettings dataclass (35-54 行)
        _defaults() (57-69 行)
        update_settings() (128-218 行,含 base_url http(s) 校验)
        require_provider_settings() (221-233 行,upload 409 把关)
        serialize_settings() (236-257 行,key 只回 *_configured 布尔)
     → backend/app/api/routes_settings.py
        UpdateProviderSettingsRequest (25-39 行) + PUT /api/settings/me/providers (63-81 行)
     → backend/app/services/document_pipeline.py:1007-1022
        process_document 里按 provider_settings 构建 MinerUConfig
     → 前端 frontend/src/lib/api.ts (56-79, 131-138, 194-202 行的类型)
     → frontend/src/components/ProviderSettingsForm.tsx (78-131 行,解析器切换与 MinerU 字段)
     → frontend/src/components/SettingsModal.tsx (表单挂载处,一般无需改)
```

## 3. SoMark API 契约(已核实,来自 https://docs.somark.cn/api-reference)

- Base URL:中国大陆 `https://somark.cn/api/v1`(默认),海外 `https://somark.ai/api/v1`。
- 鉴权:所有接口在 **multipart/form-data 请求体**里带 `api_key`(`sk-***`)。
- 限制:单文件 ≤ 200MB,≤ 300 页,QPS 4。
- 统一响应 `{code, message, data}`,`code != 0` 即错误(`1107` = API key 无效)。

### 3.1 提交异步任务 `POST {base}/parse/async`

multipart 字段:

| 字段 | 值 |
|---|---|
| `api_key` | `sk-***` |
| `file` | PDF 二进制(与 `file_url` 二选一,本项目用 `file`) |
| `output_formats` | 重复字段:`markdown`、`json` |
| `element_formats` | JSON 字符串 `{"image":"url","formula":"latex","table":"html","cs":"image"}` |
| `feature_config` | JSON 字符串,见下 |

`feature_config` 建议值(未列出的用服务端默认 false):

```json
{
  "enable_title_level_recognition": true,
  "enable_inline_image": true,
  "enable_table_image": true,
  "enable_image_understanding": true,
  "keep_header_footer": false
}
```

返回:`data.task_id`,`data.status` 初始 `QUEUING`。

### 3.2 轮询 `POST {base}/parse/async_check`

multipart:`api_key`、`task_id`。建议间隔 3–5s。`data.status`:
`QUEUING / PROCESSING / SUCCESS / FAILED`。SUCCESS 时:

```
data.result.outputs.markdown            # str,全文 Markdown
data.result.outputs.json.pages[]        # 逐页结构化结果
  .page_num                             # 从 0 开始
  .page_size {w, h}                     # 页像素尺寸
  .blocks[]                             # 按阅读顺序
    .idx                                # 块索引(页内)
    .type                               # 元素类型标识符,见 3.3
    .bbox [x1,y1,x2,y2]                 # 像素坐标(相对 page_size)
    .content                            # 块内容(字符串;表格为 html,公式为 latex)
    .format                             # 内容格式标识
    .captions []                        # 关联图注/表注块的 idx 列表
    .img_url                            # 图片资源地址(image/table 等)
    .title_level                        # 仅 title 且开启层级识别时,1=H1...
data.metadata.page_num / file_type
```

### 3.3 SoMark 元素类型 → MinerU block 映射表

SoMark 共 21 种元素类型。映射规则(产出 2.3 节的形状):

| SoMark `type` | 映射 |
|---|---|
| `title` | `{"type":"title","content":{"title_content":[{"type":"text","content": text}],"level": title_level or 1}}` |
| `text`、`code`、`reference`、`footnote` 等散文类 | `{"type":"paragraph","content":{"paragraph_content":[{"type":"text","content": text}]}}` |
| `formula` / 化学方程式 | `{"type":"equation_interline","content":{"math_content": content}}` |
| `image`、`chart`、`qrcode`、`seal`、`cs`(化学结构式) | `{"type":"image","content":{"image_source":{"path":"images/<file>"},"image_caption":[{"type":"text","content": <caption 文本>}]}}`;图片下载失败则无 `image_source`,让下游按既有逻辑丢弃 |
| `table` | `{"type":"table","content":{"html": content,"table_caption":[...]}}`;若有表图(`img_url`)附 `"image_source":{"path": ...}` |
| 图注/表注块(被其他块 `captions` 引用的 idx,或类型为 `image_caption`/`table_caption`) | **不作为独立 block 输出**,文本并入所属 image/table 的 caption 字段 |
| 页眉/页脚 | `keep_header_footer:false` 时服务端已过滤,无需处理 |

bbox 换算:`nx = round(x / page_size.w * 1000)`,`ny = round(y / page_size.h * 1000)`,
产出 0–1000 归一化坐标,`boxes_normalized=True` 路径(`mineru_layout._to_page_rect`)
无需改动。

caption 处理顺序:先遍历全部 block 收集被引用 idx → 生成 image/table block 时查表
拼接(多段以空格连接)→ 输出时跳过被引用块。

## 4. 实施清单

### 4.1 新增 `backend/app/services/somark_service.py`

仿 `mineru_service.py` 风格(4 空格缩进、`requests`、typed functions),内容:

- `SoMarkConfig`(frozen dataclass):`api_key: str`、`base_url: str`、
  `poll_interval: float`、`timeout: float`。
- `_default_config() -> SoMarkConfig`:读 `app.core.config.settings` 的
  `somark_api_key / somark_base_url / somark_poll_interval / somark_timeout`。
- 内部函数:
  - `_submit_task(pdf_path, config) -> task_id`:`requests.post(
    f"{base}/parse/async", data={...}, files={"file": (name, bytes)})`,
    `output_formats` 用 list-of-tuples 形式传重复字段
    (`[("api_key", key), ("output_formats", "markdown"), ("output_formats", "json"),
    ("element_formats", json_str), ("feature_config", json_str)]`);
    HTTP 非 200 或 `code != 0` → `RuntimeError`(1107 时文案提示 API Key 无效);
    缺 `data.task_id` → `RuntimeError`。
  - `_poll_task(task_id, config, log_sink, progress_cb) -> data`:每
    `poll_interval`(默认 3s)POST `async_check`;`QUEUING` →
    `progress_cb(0.55, "SoMark 排队中")`,`PROCESSING` → `progress_cb(0.75, "SoMark
    解析中")`;`FAILED` → `RuntimeError`(带服务端 message);累计超过 `timeout`
    (默认 600s)→ `RuntimeError("SoMark 任务轮询超时: task_id=...")`;
    单次轮询 HTTP 失败可继续重试。
  - `_download_images(pages, images_dir, log_sink) -> dict`(url →
    `images/<filename>` 相对路径):文件名取 URL path 末段,去重;单张失败记日志跳过。
  - `_to_content_blocks(pages, image_paths) -> list[list[dict]]`:按 3.3 映射,
    含 caption 解析与 bbox 归一化。
- 公开函数(签名与 MinerU 版对齐):

```python
def extract_structured_from_pdf_somark(
    pdf_path: str,
    output_dir: Path,
    log_sink=None,
    progress_cb=None,
    config: SoMarkConfig | None = None,
) -> MinerUResult
```

  流程:无 `api_key` → `RuntimeError`(行为对齐 MinerU 版)→ submit →
  `progress_cb(0.35, "SoMark 任务已提交")` → poll 成功 → `progress_cb(0.95, ...)`
  → 写 `output_dir/somark.md`(markdown 原文)与 `output_dir/somark.json`
  (完整 API 响应,`json.dumps(..., ensure_ascii=False, indent=2)`)→ 下载图片到
  `output_dir/images/` → 转换 blocks → 返回:

```python
MinerUResult(
    markdown=markdown,
    mode_label="somark",
    extracted_files=[md_path, json_path],
    content_blocks=blocks,
    layout_payload=None,
    boxes_normalized=True,
    images_dir=images_dir if images_dir.exists() else None,
    two_column=False,
)
```

  markdown 为空但 json 有内容时仍返回(下游 clean 阶段会对空文本报错,行为与
  MinerU 一致即可,不要自行兜底)。

### 4.2 `backend/app/core/config.py`

- 新增 Settings 字段(仿 `mineru_*` 的 env alias 写法,见 39-47 行):
  - `somark_api_key: str = ""`(env `SOMARK_API_KEY`)
  - `somark_base_url: str = "https://somark.cn/api/v1"`(env `SOMARK_BASE_URL`)
  - `somark_poll_interval: float = 3.0`(env `SOMARK_POLL_INTERVAL`)
  - `somark_timeout: float = 600.0`(env `SOMARK_TIMEOUT`)
- `pdf_parser` 默认值由 `"mineru"` 改为 `"somark"`(约 20-21 行的 default 逻辑)。
- `.env.example`:补 `SOMARK_API_KEY` / `SOMARK_BASE_URL` 注释,`PDF_PARSER`
  说明更新为 `somark | mineru | local`。

### 4.3 `backend/app/services/app_settings.py`

- `_PARSERS = {"local", "mineru", "somark"}`(28 行)。
- `AppSettings` 增加 `somark_api_key: str = ""`、`somark_base_url: str = ""`。
- `_defaults()`:增加 `somark_base_url=settings.somark_base_url`。
- `update_settings()`:签名增加 `somark_api_key: str | None = None`、
  `clear_somark_api_key: bool = False`、`somark_base_url: str | None = None`;
  处理逻辑仿 mineru_api_key/clear_mineru_api_key/mineru_base_url;校验段增加
  `if not current.somark_base_url: current.somark_base_url = settings.somark_base_url`
  和 http(s) 校验(文案 `"SoMark Base URL must start with http:// or https://"`)。
- `require_provider_settings()`:mineru 检查之后追加:

```python
if for_pdf and provider.pdf_parser == "somark" and not provider.somark_api_key:
    raise HTTPException(
        status_code=409,
        detail={"code": "config_required", "message": "当前选择了 SoMark,请先在设置中配置 SoMark API Key。"},
    )
```

- `serialize_settings()`:增加 `"somark_api_key_configured": bool(value.somark_api_key)`、
  `"somark_base_url": value.somark_base_url`。

### 4.4 `backend/app/api/routes_settings.py`

- `UpdateProviderSettingsRequest`(25-39 行)增加:
  `somark_api_key: str | None = None`、`clear_somark_api_key: bool = False`、
  `somark_base_url: str | None = None`。
- PUT handler(63-81 行)把三个新参数透传给 `update_settings()`。

### 4.5 `backend/app/services/document_pipeline.py`

- import:`from app.services.somark_service import SoMarkConfig, extract_structured_from_pdf_somark`。
- `process_document()` 中 `mineru_config` 构建(1008-1022 行)之后,仿造构建:

```python
somark_config = (
    SoMarkConfig(
        api_key=provider_settings.somark_api_key,
        base_url=provider_settings.somark_base_url,
        poll_interval=settings.somark_poll_interval,
        timeout=settings.somark_timeout,
    )
    if provider_settings
    else None
)
```

- parse 分支(1134 行 `if parser == "mineru":` 之前)插入:

```python
if parser == "somark":
    extract_dir = output_dir / "somark"
    record.logs.append("Submitting PDF to SoMark")
    try:
        mineru_result = extract_structured_from_pdf_somark(
            str(record.source_path),
            extract_dir,
            log_sink=record.logs,
            progress_cb=lambda frac, label: set_stage_progress(record, "parse", frac, label),
            config=somark_config,
        )
    except Exception as somark_exc:
        record.logs.append(
            f"SoMark unavailable ({somark_exc}); falling back to local PDF parsing"
        )
        extract_dir = output_dir / "local"
        try:
            mineru_result = extract_structured_from_pdf_local(
                str(record.source_path), extract_dir, log_sink=record.logs
            )
        except Exception as local_exc:
            raise RuntimeError(
                f"SoMark parsing failed: {somark_exc}; local fallback also failed: {local_exc}"
            ) from local_exc
elif parser == "mineru":
    ...
```

(局部变量名 `mineru_result` 沿用现状,不改名,避免无关 diff。)

### 4.6 前端

- `frontend/src/lib/api.ts`:
  - `pdf_parser` 类型字面量 → `'local' | 'mineru' | 'somark'`(settings 响应与
    更新请求两处)。
  - settings 响应类型增加 `somark_api_key_configured: boolean`、`somark_base_url: string`。
  - 更新请求类型增加 `somark_api_key?: string`、`clear_somark_api_key?: boolean`、
    `somark_base_url?: string`。
- `frontend/src/components/ProviderSettingsForm.tsx`(78-131 行):
  - 解析器选项增加 `"SoMark 云解析"`(放首位,为默认值)。
  - 选中 `somark` 时渲染:API Key 输入(placeholder 提示在 somark.cn 控制台
    "API Workbench → APIKey" 获取;已配置时显示已配置状态,仿 mineru key 的
    交互)+ Base URL 输入(默认值 `https://somark.cn/api/v1`,帮助文案
    "中国大陆用 somark.cn,海外用 https://somark.ai/api/v1")。
  - MinerU 字段组仅在选择 `mineru` 时渲染(现状即如此,保持)。

### 4.7 测试

新增 `backend/tests/test_somark_service.py`(mock `app.services.somark_service.requests`,
仿 `backend/tests/test_mineru_service.py` 的 mock 方式):

1. **happy path**:submit 返回 `task_id`;第一次 poll 返回 `PROCESSING`,第二次返回
   `SUCCESS`,fixture 含一页:`title(title_level=2)`、`text`、含 `captions:[1]`
   的 `image`(idx 0,有 `img_url`)、`image_caption`(idx 1)、`formula`、`table`
   (html content)。断言:
   - `result.mode_label == "somark"`,`markdown` 与 fixture 一致;
   - `content_blocks[0]` 各 block 类型/字段符合 3.3 映射(title.level==2);
   - image block 的 `image_caption` 文本来自 idx 1 块,且 idx 1 不再独立出现;
   - bbox 已按 `page_size` 归一化到 0–1000(用已知像素值断言换算结果);
   - 图片已写入 `output_dir/images/`,`images_dir` 指向它;
   - `somark.md` / `somark.json` 已落盘且在 `extracted_files` 中。
2. `FAILED` 状态 → `RuntimeError`,信息含服务端 message。
3. 无 api_key → `RuntimeError`,不发任何 HTTP。
4. poll 持续 `PROCESSING` 且 `timeout` 极小 → 超时 `RuntimeError`。
5. submit 返回 `code == 1107` → `RuntimeError` 文案含 API Key 提示。

更新既有测试:

- `backend/tests/test_provider_settings.py`:默认 `pdf_parser == "somark"`;
  somark 字段写入/掩码(`somark_api_key_configured`)/`clear_somark_api_key`/
  非法 base_url 400。
- `backend/tests/test_routes_upload.py`:`pdf_parser="somark"` 且无 somark key →
  409 `config_required`;检查因默认值变化受影响的断言(mineru 路径仍应通过)。
- 其余 pipeline 测试 monkeypatch 的是 extract 函数 / `MinerUResult` fixture,
  预期不受影响;全量回归确认。

### 4.8 文档与脚本

- `docs/DEVELOPMENT.md` 的 MinerU 配置一节(约 1105-1116 行):前面插入 SoMark 小节
  (key 获取路径 somark.cn "API Workbench → APIKey"、base url 国内/海外、限额
  200MB / 300 页 / QPS 4、`PDF_PARSER=somark` 为默认);MinerU 改为"可选备选"。
- `scripts/start_web.sh`(约 73 行)环境提示补 `SOMARK_API_KEY`。
- README / 用户文档顺手把"MinerU 云解析"表述改为"SoMark 云解析(默认),可选
  MinerU";历史 release notes 不动。

## 5. 验证(必须全部通过才算完成)

1. `python -m compileall backend/app` — 语法检查。
2. `pytest` — 全量后端测试。
3. `npm --prefix frontend run build` — 前端类型检查 + 构建。
4. 手动端到端(需要有效 `SOMARK_API_KEY`;先用 `POST {base}/usage` 验证 key 有效):
   设置页选 SoMark、填入 key → 上传一篇论文 PDF → 观察 parse 阶段日志出现
   "Submitting PDF to SoMark" 与进度推进 → 完成后确认翻译/渲染产物中图片、
   行间公式、表格正常。若执行环境无有效 key,在完成报告中如实说明未做真实
   API 验证,不得声称已验证。

## 6. 明确不做(防止范围蔓延)

- 不删除/重命名 MinerU 任何代码、配置项、`MinerUResult`、`mineru_layout.py`、
  artifact kind `"mineru_output"`、`extraction-checkpoint.json` 格式。
- 不改 DB schema、上传接口契约、Celery worker。
- 不使用 SoMark 的 `zip` 输出格式(逐张下载 `img_url` 已满足 `images/` 需求)。
- 不实现 `layout_payload`(`content_blocks` 路径已足够)。
- 图片 URL 过期问题不在本次处理(解析时即下载落盘,天然规避)。
