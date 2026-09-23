# Translation Layout V2 Implementation Plan

> **Historical.** This plan describes PaperReader's own translated-PDF renderer
> (`layout_model` / `layout_fit` / `layout_render`), which has been replaced by
> the PDFMathTranslate-next worker. See
> `2026-09-22-pdf-parser-pdfmathtranslate-migration.md` and `docs/DEVELOPMENT.md`
> §19 for the current design. Kept as the record of how that renderer worked.

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前定位式 PDF 翻译链路升级为固定中文模板、图表注可翻译、公式不丢失、严格一页对应一页并带结构化回退诊断的 Translation Layout V2。

**Architecture:** 保留现有 MinerU IR → 翻译 → `PagePlan` → 原页文字移除与覆盖渲染主链路。扩展 IR 的语义角色、内置字体注册、图表注与首页信息翻译槽位；将逐块绝对定位拟合改为“不可移动区域 + 同栏文字链 + 全页统一字号”的页面求解；删除会增加页数的自由重排回退，统一回退到原页并输出结构化问题。

**Tech Stack:** Python 3、FastAPI、dataclasses、ReportLab 4.2.5、pypdf 4.3.1、pypdfium2 4.30.0、React 18、TypeScript、Vite。

**Spec:** `docs/translation-layout.md`

## Global Constraints

- 一页原文严格对应一页译文，不允许生成额外译文页。
- 页面尺寸、图片、表格线条和独立公式位置保持不变。
- 单栏正文 10.5pt，双栏正文 9pt，全页正文统一缩放，最低 6pt。
- 图表注翻译；参考文献条目保留原文；行内公式不得丢失。
- 翻译失败块保留原文，表格仅回退失败单元格，排版失败页完整回退原页。
- 回退超过 3 页且达到全文 20%，或全部页面回退时，整篇失败。
- 不引入版式开关、兼容层或第二套渲染管线。
- 译文 PDF 隐藏 Link 注释的可见边框但保留跳转；解析诊断框只能存在于独立标注产物。
- 当前工作区存在未提交的原文标注 PDF 改动；实施前必须先把这组改动保存到独立提交或分支，再从包含它的提交创建隔离 worktree。不得覆盖当前工作区。

---

## 当前仓库基线

| 模块 | 当前状态 | V2 差距 |
|---|---|---|
| `mineru_layout.py` | IR 包含 Title、Author、Paragraph、ListBlock、DisplayMath、Image、Table；caption 是 Image/Table 字符串 | 缺少语义角色、caption 译文槽位、首页细分和脚注/代码/算法角色 |
| `layout_model.py` | 可从文字层校正段落、恢复遗漏正文；当前未提交改动正在补 caption bbox 和跨栏续段 | 应在这组改动之上增加栏结构、不可移动区域和角色识别 |
| `translate_service.py` | 标题、正文、列表、表格单元格进入翻译；caption/Author/公式不翻译；失败分片会被省略 | caption、单位地址和参考文献标题需翻译；任何分片失败时整个逻辑块应回退原文 |
| `cjk_fonts.py` | 运行时寻找系统字体，只注册一个宋体 regular/bold 家族 | 改为内置固定宋体和黑体，测试不再依赖系统字体 |
| `layout_fit.py` | 正文默认 10.5pt，逐块拟合后做有限全页统一；表格默认正文大小；缺坐标行内公式会被删除 | 增加单双栏模板、全页统一缩放、同栏文字链、caption plan、6pt 下限；公式不得删除 |
| `layout_render.py` | 可验证文字移除并回退块；失败页默认自由重排，可能增加页数 | 删除自由重排回退；失败页只能原页回退；保留现有移除验证 |
| `pdf_ops.py` / PDF 注释 | 克隆源页时保留原始 Link 注释及其红绿边框 | 译文成品统一隐藏 Link 边框，同时保留点击区域和目标 |
| `document_pipeline.py` | 保存 `layout-plan.json`，布局问题主要写入字符串日志 | 增加结构化 layout issues，并在 API/UI 显示 |
| 测试 | 已覆盖续段、表格线条、公式裁图、回退预算和自由重排 | 旧的 caption 不翻译、缺公式即删除、自由重排增加页数测试必须改写 |

---

### Task 1: 固化 IR 语义角色和布局问题接口

**Files:**
- Modify: `backend/app/services/mineru_layout.py`
- Modify: `backend/app/services/layout_model.py`
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/app/api/routes_document.py`
- Test: `backend/tests/test_mineru_layout.py`
- Test: `backend/tests/test_annotated_pdf_api.py`

**Interfaces:**
- Add `TextRole = Literal["body", "abstract", "keywords", "author", "affiliation", "footnote", "code", "algorithm", "acknowledgement", "appendix", "reference_heading", "reference_entry", "running", "unknown"]`.
- Add `role: str = "body"` to `Title`, `Author`, `Paragraph`, and `ListBlock`, preserving existing constructors through defaults.
- Add `translated_caption: str = ""` to `Image` and `Table`; retain `caption` as source text and `caption_bbox` as source geometry.
- Add API model `LayoutIssueItem(kind: str, page: int, block_kind: str, message: str)` and `DocumentStatusResponse.layout_issues`. Data is stored under `record.metadata["layout_issues"]`, so no SQLite schema migration is needed.

- [x] **Step 1: Add failing IR role tests**

Add fixtures proving that Abstract/Keywords headings set following paragraphs to matching roles, a References heading is `reference_heading`, typed reference lists are `reference_entry`, and caption source text plus bbox survive both `content_list_v2` and `middle.json` parsing.

- [x] **Step 2: Run focused tests and confirm the missing fields fail**

Run: `conda run -n pt pytest backend/tests/test_mineru_layout.py -q`

Expected failure: constructors or assertions cannot find `role` / `translated_caption`, and the References heading has no distinct role.

- [x] **Step 3: Add role classification without replacing existing block classes**

Track the current section role while iterating MinerU blocks. Classify only explicit headings and parser-provided block types; leave uncertain content as `body` or `unknown`. Keep repeated running headers excluded from translation.

- [x] **Step 4: Add typed layout issues to the document response**

Read issue dictionaries from `record.metadata.get("layout_issues", [])`, validate into `LayoutIssueItem`, and return an empty list for old documents.

- [x] **Step 5: Extend annotated-PDF categories**

Update the current uncommitted `annotation_render.py` work to color captions, footnotes, references, formulas and unknown blocks distinctly. Do this only after the annotation work is committed and present in the implementation worktree.

- [x] **Step 6: Run focused tests**

Run: `conda run -n pt pytest backend/tests/test_mineru_layout.py backend/tests/test_annotated_pdf_api.py backend/tests/test_annotation_render.py -q`

- [x] **Step 7: Commit**

```bash
git add backend/app/services/mineru_layout.py backend/app/services/layout_model.py backend/app/models/schemas.py backend/app/api/routes_document.py backend/app/services/annotation_render.py backend/tests/test_mineru_layout.py backend/tests/test_annotated_pdf_api.py backend/tests/test_annotation_render.py
git commit -m "refactor: 补充译文版面语义角色"
```

### Task 2: 内置固定宋体和黑体

**Files:**
- Create: `backend/app/assets/fonts/`
- Create: `backend/app/assets/fonts/LICENSES.md`
- Modify: `backend/app/services/cjk_fonts.py`
- Modify: `desktop/PaperReader.spec`
- Modify: `desktop/setup_macos.py`
- Test: `backend/tests/test_layout_fonts.py`

**Interfaces:**
- Replace `CjkFontFamily(regular, bold, path)` with `CjkFontSet(serif_regular, serif_bold, sans_medium, sans_bold, asset_paths)`.
- Add `CjkFontSet.serif(bold: bool = False) -> str` and `CjkFontSet.sans(bold: bool = False) -> str`.
- Keep `require_cjk_font()` as the public loader name, but return `CjkFontSet` to minimize pipeline call-site changes.

- [x] **Step 1: Select and record redistributable static TrueType faces**

Vendor one Simplified Chinese serif regular/bold pair and one sans medium/bold pair. Record upstream name, version, license and source in `LICENSES.md`. Reject variable fonts or CFF-only OpenType files that ReportLab 4.2.5 cannot load.

- [x] **Step 2: Add a failing font test**

The test must clear system-font assumptions, call `require_cjk_font()`, render 宋体正文 and 黑体标题, and assert that four registered font names are available from repository assets.

- [x] **Step 3: Replace system discovery with repository asset loading**

Resolve assets relative to the installed package, register each face once, and probe the existing CJK/Latin/math punctuation character set. Remove directory scans and platform candidate tables.

- [x] **Step 4: Package assets on Windows and macOS**

Add `backend/app/assets/fonts` to PyInstaller `datas` and py2app resources. Ensure development, packaged app and generated PDF use the same paths.

- [x] **Step 5: Run font and package smoke checks**

Run: `conda run -n pt pytest backend/tests/test_layout_fonts.py backend/tests/test_layout_render.py -q`

Run: `python -m compileall backend/app`

- [x] **Step 6: Commit**

```bash
git add backend/app/assets/fonts backend/app/services/cjk_fonts.py desktop/PaperReader.spec desktop/setup_macos.py backend/tests/test_layout_fonts.py
git commit -m "feat: 内置译文排版中文字体"
```

### Task 3: 修正翻译槽位和失败块回退

**Files:**
- Modify: `backend/app/services/mineru_layout.py`
- Modify: `backend/app/services/translate_service.py`
- Modify: `backend/app/services/document_pipeline.py`
- Test: `backend/tests/test_translate_ir.py`
- Test: `backend/tests/test_pipeline_resume.py`

**Interfaces:**
- Extend `_block_slots()` to yield caption targets, translatable affiliation parts and reference headings.
- Keep reference entries masked. Change the mask transition so the heading itself translates and masking begins with the first reference entry.
- Add `TranslationIssue(logical_index: int, source: str, reason: str)` internally; `translate_ir()` still returns display notes but restores the entire source slot when any chunk fails.
- Bump `_TRANSLATION_CONTRACT_VERSION` so old omitted/partial checkpoint values are not reused.

- [x] **Step 1: Rewrite translation-contract tests**

Assert that figure/table captions and affiliation text enter the queue, author names do not, `References` translates, reference entries stay English, and one failed chunk restores the entire original logical segment.

- [x] **Step 2: Verify the current omission behavior fails the new tests**

Run: `conda run -n pt pytest backend/tests/test_translate_ir.py -q`

Expected failure: captions are absent from the queue and failed chunks produce shortened text.

- [x] **Step 3: Add caption and front-matter translation slots**

Write translated captions to `translated_caption`; preserve `caption` for matching and fallback. Split front matter only where parser/text-layer evidence identifies names, affiliations and contacts; uncertain author blocks remain source text.

- [x] **Step 4: Make logical-slot fallback atomic**

If any piece in `segment_groups` is missing after retries, set the slot result to its complete source string and add one structured issue. Never join only successful pieces.

- [x] **Step 5: Preserve checkpoint and alignment behavior**

Cache successful translated slots and explicit source fallbacks separately. Ensure `save_exact_alignment()` receives one source and one final string per logical slot.

- [x] **Step 6: Run translation and resume tests**

Run: `conda run -n pt pytest backend/tests/test_translate_ir.py backend/tests/test_pipeline_resume.py -q`

- [x] **Step 7: Commit**

```bash
git add backend/app/services/mineru_layout.py backend/app/services/translate_service.py backend/app/services/document_pipeline.py backend/tests/test_translate_ir.py backend/tests/test_pipeline_resume.py
git commit -m "fix: 保留翻译失败的完整原文块"
```

### Task 4: 建立固定中文样式和页面栏型

**Files:**
- Modify: `backend/app/services/layout_fit.py`
- Modify: `backend/app/services/layout_model.py`
- Test: `backend/tests/test_layout_typography.py`
- Test: `backend/tests/test_layout_continuation.py`

**Interfaces:**
- Add immutable `TypographyProfile` constants for title levels, single-column body, double-column body, caption, table cell and footnote.
- Add `PageColumns(kind: Literal["single", "double", "mixed"], columns: list[Rect])`.
- Add `detect_page_columns(frame: PageFrame, blocks: list[Block]) -> PageColumns`.
- Change `block_style()` to accept block role and page column profile; alignment and paragraph indentation still come from source geometry.

- [x] **Step 1: Add failing typography tests**

Create synthetic single-column, double-column and mixed pages. Assert body baselines of 10.5pt and 9pt, title levels 16/13/11.5pt, captions and table cells 8pt, footnotes 7.5pt, serif body and sans headings.

- [x] **Step 2: Add deterministic column detection**

Cluster body block horizontal ranges, treating full-width titles/abstracts as spans rather than a third column. A page is double-column only when two stable non-overlapping body bands contain substantial text.

- [x] **Step 3: Replace source-derived leading with fixed template leading**

Keep source alignment and indentation evidence, but use profile line-height ratios. Do not enlarge short translations.

- [x] **Step 4: Keep continuation grouping compatible**

Ensure existing cross-column and cross-page continuation tests still merge semantic content before layout. Column detection must not alter text ownership.

- [x] **Step 5: Run focused tests**

Run: `conda run -n pt pytest backend/tests/test_layout_typography.py backend/tests/test_layout_continuation.py -q`

- [x] **Step 6: Commit**

```bash
git add backend/app/services/layout_fit.py backend/app/services/layout_model.py backend/tests/test_layout_typography.py backend/tests/test_layout_continuation.py
git commit -m "refactor: 应用固定中文排版模板"
```

### Task 5: 保证块内公式和独立公式不丢失

**Files:**
- Modify: `backend/app/services/layout_fit.py`
- Modify: `backend/app/services/layout_render.py`
- Modify: `backend/app/services/layout_model.py`
- Test: `backend/tests/test_layout_render.py`
- Test: `backend/tests/test_layout_formula.py`

**Interfaces:**
- Extend formula fragments with source bbox, source-line bbox, aspect ratio and baseline ratio.
- Add `recover_inline_formula_box(frame, block, run_index) -> Rect | None`.
- Add `FormulaFallback = Literal["exact_crop", "line_crop", "original_block"]` to layout-plan diagnostics.

- [x] **Step 1: Replace the existing missing-formula expectation**

Remove the test that expects a geometry-less formula to be dropped. Add cases for exact crop, recovered bbox, source-line crop and whole-block fallback.

- [x] **Step 2: Recover formula geometry from adjacent text**

Use preceding/following text spans and the source text layer to bound the gap occupied by the formula. Accept a recovered box only when it lies inside the paragraph and does not overlap unrelated text.

- [x] **Step 3: Preserve a source line when exact recovery fails**

Crop the smallest source line containing the formula placeholder and insert it as one atomic inline fragment. If the line cannot be isolated, mark the whole block original.

- [x] **Step 4: Keep independent formulas fixed**

Add DisplayMath boxes to immutable page obstacles. Do not mask, translate, resize or include them in body-size solving. Preserve equation numbers only inside the verified source formula region.

- [x] **Step 5: Verify baseline and completeness**

Render a paragraph containing text–formula–text and assert the formula crop is present, remains atomic during wrapping and no source formula is erased.

- [x] **Step 6: Run formula tests**

Run: `conda run -n pt pytest backend/tests/test_layout_formula.py backend/tests/test_layout_render.py -q`

- [x] **Step 7: Commit**

```bash
git add backend/app/services/layout_fit.py backend/app/services/layout_render.py backend/app/services/layout_model.py backend/tests/test_layout_formula.py backend/tests/test_layout_render.py
git commit -m "fix: 保证译文中的公式完整保留"
```

### Task 6: 实现图表注翻译和表格单元格规则

**Files:**
- Modify: `backend/app/services/layout_fit.py`
- Modify: `backend/app/services/layout_render.py`
- Modify: `backend/app/services/layout_model.py`
- Test: `backend/tests/test_layout_captions.py`
- Test: `backend/tests/test_layout_render.py`

**Interfaces:**
- Add `CaptionPlan(owner_kind, source_rect, target, source_text, translated, size, status, reason)`.
- Add `PagePlan.captions: list[CaptionPlan]`.
- Set table-cell baseline to 8pt and minimum to 6pt; retain per-cell `original` fallback.

- [x] **Step 1: Add failing caption tests**

Cover figure captions, table captions, table notes, wrapped captions, full-width captions attached to the final subfigure, and captions with `(a)/(b)` markers. Assert labels and numbers survive translation.

- [x] **Step 2: Use caption geometry from the current annotation work**

Prefer parser-provided `caption_bbox`; otherwise reuse text-layer caption location logic. Keep the current protection against matching a distant identical正文 line.

- [x] **Step 3: Plan captions as movable text next to an immutable owner**

Start at the source caption box, expand only into adjacent whitespace, then join the following same-column text chain. Never move or cover the owner image/table.

- [x] **Step 4: Apply table-cell typography**

Wrap translated cell text at 8pt, binary-search to 6pt, then mark only that cell original. Preserve rules and source text outside translated cell boxes.

- [x] **Step 5: Render and verify searchable captions/cells**

Extract text from the output PDF and assert translated caption and cell text are searchable while table rules remain visible in a raster check.

- [x] **Step 6: Run caption/table tests**

Run: `conda run -n pt pytest backend/tests/test_layout_captions.py backend/tests/test_layout_render.py backend/tests/test_mineru_layout.py -q`

- [x] **Step 7: Commit**

```bash
git add backend/app/services/layout_fit.py backend/app/services/layout_render.py backend/app/services/layout_model.py backend/tests/test_layout_captions.py backend/tests/test_layout_render.py backend/tests/test_mineru_layout.py
git commit -m "feat: 翻译并定位图表注释"
```

### Task 7: 用同栏文字链替换逐块独立拟合

**Files:**
- Modify: `backend/app/services/layout_fit.py`
- Test: `backend/tests/test_layout_flow.py`
- Test: `backend/tests/test_layout_continuation.py`

**Interfaces:**
- Add `FlowItem(block_plan_index, source_rect, min_gap_before, movable)`.
- Add `FlowChain(page_index, column_rect, items, obstacles)`.
- Add `build_flow_chains(frame, blocks, captions, columns) -> list[FlowChain]`.
- Add `solve_page_layout(page_plan, measurer) -> None`, replacing per-block `expand_target()` as the final authority.

- [x] **Step 1: Add failing flow-chain tests**

Cover two paragraphs borrowing a gap, a caption pushing following正文, an intervening formula stopping movement, independent left/right columns, and a full-width heading feeding two columns.

- [x] **Step 2: Build chains from reading order and horizontal overlap**

Assign movable text blocks to one column. Start a new chain at an immutable obstacle, a full-width structural boundary or a change of column. Keep original top positions as preferred anchors.

- [x] **Step 3: Allocate vertical space at baseline typography**

Measure every item, consume existing gaps, reduce paragraph gaps to their source-safe minimum, and shift later items without crossing the chain boundary.

- [x] **Step 4: Solve one body size for the whole page**

If any chain overflows, binary-search one shared body size from the page baseline down to 6pt and re-solve every chain. Titles retain their profile ratios; captions and cells use their own sizes.

- [x] **Step 5: Mark page failure instead of individual fit fallback**

If any body chain still overflows at 6pt, set `PagePlan.status = "original"` with a precise reason. Do not revert only the dense paragraph and do not invoke free reflow.

- [x] **Step 6: Run flow tests**

Run: `conda run -n pt pytest backend/tests/test_layout_flow.py backend/tests/test_layout_continuation.py -q`

- [x] **Step 7: Commit**

```bash
git add backend/app/services/layout_fit.py backend/tests/test_layout_flow.py backend/tests/test_layout_continuation.py
git commit -m "refactor: 按同栏文字链统一排版"
```

### Task 8: 删除增页重排回退并统一页面预算

**Files:**
- Modify: `backend/app/services/layout_render.py`
- Modify: `backend/app/core/config.py`
- Delete: `backend/app/services/reflow_render.py`
- Modify: `.env.example`
- Test: `backend/tests/test_reflow.py`
- Test: `backend/tests/test_layout_render.py`

**Interfaces:**
- Remove `settings.layout_reflow_fallback` and `_try_reflow()`.
- Keep page statuses `ok`, `masked`, and `original`; `reflow` is no longer valid.
- Keep `_enforce_failure_budget()` thresholds unchanged: all pages, or more than 3 pages and at least 20%.

- [x] **Step 1: Replace reflow tests with one-page-one-page assertions**

Assert that a failed page writes exactly one original page, total output page count equals source page count, and page fallback contributes to the failure budget.

- [x] **Step 2: Remove the reflow branch and configuration**

When `PagePlan.status != "ok"` or rendering cannot safely remove source text, use masked overlay only when the translation plan remains valid; otherwise append the original source page. Never create fresh flow pages.

- [x] **Step 3: Delete the unused renderer**

Remove `reflow_render.py` and all imports/tests that rely on page spill. Preserve formula-crop support in the anchored renderer.

- [x] **Step 4: Verify failure budgets**

Test 3/10 original pages succeeds, 4/20 fails at 20%, 4/21 succeeds below 20%, and all pages original fails regardless of count.

- [x] **Step 5: Run render tests**

Run: `conda run -n pt pytest backend/tests/test_reflow.py backend/tests/test_layout_render.py -q`

- [x] **Step 6: Commit**

```bash
git add backend/app/services/layout_render.py backend/app/core/config.py backend/tests/test_reflow.py backend/tests/test_layout_render.py .env.example
git rm backend/app/services/reflow_render.py
git commit -m "refactor: 严格保持译文页数一致"
```

### Task 9: 隐藏译文链接边框并隔离诊断覆盖层

**Files:**
- Modify: `backend/app/services/pdf_ops.py`
- Modify: `backend/app/services/layout_render.py`
- Modify: `backend/app/services/annotation_render.py`
- Modify: `frontend/src/pages/ReaderPage.tsx`
- Test: `backend/tests/test_pdf_link_annotations.py`
- Test: `backend/tests/test_layout_render.py`
- Test: `backend/tests/test_annotated_pdf_api.py`

**Interfaces:**
- Add `hide_link_borders(page) -> int` in `pdf_ops.py`; return the number of Link annotations normalized.
- The helper preserves `/Rect`, `/A`, `/Dest` and other navigation data. It sets `/Border` to `[0 0 0]`, sets `/BS /W` to `0`, removes `/C`, and removes a Link-only `/AP` appearance when present.
- Call the helper on every final page added to the translated writer, including masked-overlay pages and original fallback pages, immediately before `writer.write()`.
- Do not call it when producing the original source file or the independent annotated-source artifact.

- [x] **Step 1: Add a synthetic colored-link regression PDF**

Generate a page containing a red internal Figure link and a green citation link with visible borders. Record each annotation's rectangle, action or destination, subtype and border color before rendering.

- [x] **Step 2: Write failing border-removal assertions**

Assert that translated output keeps the same number of `/Link` annotations, the same `/Rect`, `/A` or `/Dest`, but has zero border width, no `/C`, and no visible red/green rectangles in a raster render. Assert that non-Link annotations are dictionary-equivalent to their source values.

- [x] **Step 3: Implement Link-only normalization**

Dereference each item in `/Annots`, select only dictionaries whose `/Subtype` is `/Link`, and update the cloned writer-page annotation dictionary. Use pypdf `ArrayObject`, `NumberObject`, `DictionaryObject` and `NameObject`; do not mutate the reader's original page.

- [x] **Step 4: Apply normalization after all page fallbacks**

Run `hide_link_borders()` over every page in the translated `PdfWriter` after overlay/revert decisions are final. This ensures an original fallback page cannot reintroduce colored borders.

- [x] **Step 5: Keep diagnostic overlays in their own artifact**

Add a regression assertion that `render_annotated_pdf()` writes boxes only to the annotated artifact. Verify `ReaderPage` switches `leftPdfUrl` back to `originalPdfUrl` as soon as `show_annotated_pdf` is false and does not reuse an annotated override URL.

- [x] **Step 6: Run focused checks**

Run: `conda run -n pt pytest backend/tests/test_pdf_link_annotations.py backend/tests/test_layout_render.py backend/tests/test_annotated_pdf_api.py -q`

Run: `npm --prefix frontend run build`

- [x] **Step 7: Commit**

```bash
git add backend/app/services/pdf_ops.py backend/app/services/layout_render.py backend/app/services/annotation_render.py frontend/src/pages/ReaderPage.tsx backend/tests/test_pdf_link_annotations.py backend/tests/test_layout_render.py backend/tests/test_annotated_pdf_api.py
git commit -m "fix: 隐藏译文PDF链接边框"
```

### Task 10: 持久化布局计划和界面提示

**Files:**
- Modify: `backend/app/services/document_pipeline.py`
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/app/api/routes_document.py`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/pages/ReaderPage.tsx`
- Modify: `frontend/src/components/Sidebar.tsx`
- Test: `backend/tests/test_pipeline_resume.py`
- Test: `backend/tests/test_reader_features.py`

**Interfaces:**
- `layout-plan.json` version 2 records typography profile, column model, flow chains, formula fallback, caption plans, page status and cell status.
- `DocumentStatus.layout_issues: LayoutIssueItem[]` mirrors `record.metadata["layout_issues"]`.
- UI groups issues by page and kind; it does not render diagnostic warnings into the PDF.

- [x] **Step 1: Add API tests for structured issues**

Create a record with a translation fallback, English table cell and original page, then assert stable JSON fields and one-based page numbers.

- [x] **Step 2: Populate issues from translation, planning and rendering**

Clear old layout issues when reprocessing starts. Append issues through one helper that de-duplicates by kind/page/block. Save before publishing the translated PDF.

- [x] **Step 3: Version the layout-plan artifact**

Add a top-level version and immutable input summary. Old plan files remain downloadable but are not interpreted as V2 diagnostics.

- [x] **Step 4: Add the reader warning panel**

Show counts for untranslated blocks, English cells and original pages. Clicking a page issue navigates both PDF panes to that page. Keep full details in the existing logs/artifact area.

- [x] **Step 5: Run backend and frontend checks**

Run: `conda run -n pt pytest backend/tests/test_pipeline_resume.py backend/tests/test_reader_features.py -q`

Run: `npm --prefix frontend run build`

- [x] **Step 6: Commit**

```bash
git add backend/app/services/document_pipeline.py backend/app/models/schemas.py backend/app/api/routes_document.py frontend/src/lib/api.ts frontend/src/pages/ReaderPage.tsx frontend/src/components/Sidebar.tsx backend/tests/test_pipeline_resume.py backend/tests/test_reader_features.py
git commit -m "feat: 展示译文布局回退信息"
```

### Task 11: 集成回归和真实论文验收

**Files:**
- Modify: `scripts/reflow_real_paper_check.py`
- Create: `scripts/translation_layout_v2_check.py`
- Modify: `docs/translation-layout.md` only if implementation details changed without changing approved behavior
- Test: existing backend suite and frontend build

**Interfaces:**
- The new checker accepts source PDF, translated PDF and `layout-plan.json`; returns non-zero on page-count/size mismatch, body text below 6pt, unreported original page, missing formula fragment, visible Link border, overlap or page overflow.

- [x] **Step 1: Replace the old reflow-oriented real-paper script**

Rename its assertions around one-page-one-page geometry. Do not retain checks that accept extra reflow pages.

- [x] **Step 2: Add deterministic PDF checks**

Compare page count and dimensions, inspect layout-plan statuses, extract translated text, and raster-check that immutable image/table/formula regions match the source within the renderer's mask exclusions.

- [x] **Step 3: Run targeted backend suites**

Run: `conda run -n pt pytest backend/tests/test_mineru_layout.py backend/tests/test_translate_ir.py backend/tests/test_layout_continuation.py backend/tests/test_layout_formula.py backend/tests/test_layout_captions.py backend/tests/test_layout_flow.py backend/tests/test_layout_render.py backend/tests/test_pdf_link_annotations.py backend/tests/test_pipeline_resume.py -q`

- [x] **Step 4: Run complete verification**

Run: `conda run -n pt pytest backend/tests -q`

Run: `python -m compileall backend/app`

Run: `npm --prefix frontend run build`

- [x] **Step 5: Run real-paper acceptance**

Use at least one single-column paper, one dense two-column paper, and one paper containing inline formulas, display formulas, multi-panel figures, wrapped captions, complex tables, footnotes and references. Inspect the translated PDF, annotated source PDF and layout-plan together.

- [x] **Step 6: Confirm acceptance outcomes**

Record page count, fallback pages, untranslated blocks, English table cells, minimum body size, formula fallback modes, Link annotation count/targets and whether every issue is visible in the UI. Do not declare completion while any formula is missing, any page count differs, or an unreported fallback remains.

- [x] **Step 7: Commit**

```bash
git add scripts/reflow_real_paper_check.py scripts/translation_layout_v2_check.py docs/translation-layout.md
git commit -m "test: 完善译文排版验收"
```

---

## 实施顺序和评审门

1. Task 1–3 先固定数据契约和内容完整性；这时渲染外观暂不变化。
2. Task 4–7 完成字体、公式、图表注和页面求解；每个任务必须独立通过对应测试。
3. Task 8 删除与一页对应一页冲突的旧回退路径。
4. Task 9 清除成品中的链接边框并隔离诊断覆盖层。
5. Task 10 让所有回退可见、可定位。
6. Task 11 才进行完整测试和真实论文视觉验收。

执行期间不要同时修改当前主工作区的未提交标注功能。若标注功能尚未形成提交，先暂停代码实施，只保留本设计和路线文档。
