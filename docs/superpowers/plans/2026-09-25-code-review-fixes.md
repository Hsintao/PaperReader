# 代码审查问题修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复当前工作区代码审查确认的后端文档生命周期竞态、产物冲突、上传清理问题和前端跨文档异步竞态，并为每项行为补充可执行的回归验证。

**Architecture:** 后端以文档运行代次（revision/run generation）和受控状态转换为协调边界；删除、取消、重试及后台写回都必须检查同一代次，产物发布使用互不冲突的文件名并在失败/取消时清理旧引用。前端为所有跨文档异步操作捕获 `documentId`，并为同一文档内的搜索请求使用递增请求序号；返回结果只允许写回仍然匹配的文档和请求。修复按后端生命周期、worker 资源、前端异步和低风险清理四组交付，每组均有独立测试。

**Tech Stack:** Python、FastAPI、SQLite、pytest、React、TypeScript、Vite、PDF.js。

**Spec:** 本计划依据当前代码审查结果及 `AGENTS.md`，不引入新的产品需求或依赖；审查记录中的 9 个主要问题和 4 个低优先级设计问题均以当前代码为准。

## Global Constraints

- 后端测试放在 `backend/tests/test_*.py`，使用现有 fixture、SQLite 隔离目录和 worker stub。
- 前端当前没有 first-party React 测试 runner；不得为单个修复擅自引入测试框架，至少运行 `npm --prefix frontend run build`，并扩展现有浏览器测试或用可测试的纯函数/模块边界验证。
- worker 专属依赖只能位于 `desktop/requirements-worker.txt` 及 `workers/pdfmathtranslate` 运行时，不得加入根目录 `requirements.txt`。
- 不改变已有 API 返回字段语义，除非该字段当前错误地暴露了失效产物；错误使用现有 HTTP 状态码和错误格式。
- 不删除用户数据以外的运行时文件；测试必须使用临时目录，不能污染 `data/uploads`、`data/outputs`、`data/worker`。
- 不添加哈希、兼容层、feature flag 或迁移框架；只实现当前项目实际路径需要的协调和清理。
- 每项实现先写一个能复现问题的失败测试，再写最小修复；测试失败原因必须与目标行为直接相关。
- 每个任务完成后运行该任务的局部测试；全部任务完成后运行 `pytest -q backend/tests`、`python -m compileall -q backend/app workers` 和 `npm --prefix frontend run build`。

## 文件变更总览

### 后端核心

- Modify: `backend/app/api/routes_document.py`：删除、取消、重试相关路由在文档运行代次和 worker 生命周期上协调；失败/取消时不暴露旧译文 URL。
- Modify: `backend/app/models/store.py`：增加原子状态/运行代次更新、按代次清理和不冲突的 artifact 文件名；保持现有持久化接口兼容。
- Modify: `backend/app/services/document_pipeline.py`：为后台 pipeline 绑定运行代次，所有状态和产物写回验证代次；区分 dual/merged 文件名并在新轮次开始时清空旧译文引用。
- Modify: `backend/app/services/pdf_translation_worker.py`：使取消、完成、清理生命周期可被 pipeline 安全判断，并避免已完成 run 被取消。
- Modify: `backend/app/api/routes_upload.py`：上传写入异常时删除 partial file。
- Modify: `backend/app/core/database.py`、`backend/app/main.py`：取消模块导入时的副作用初始化，保留应用启动时初始化和恢复行为。
- Modify: `workers/pdfmathtranslate/runner.py`：为终止信号执行可控 scratch 清理；正常完成、失败、取消都收敛到同一清理路径。

### 后端测试

- Modify: `backend/tests/test_retry_api.py`：增加取消/完成竞态、重试旧 URL 清理、删除处理中任务的回归测试。
- Modify: `backend/tests/test_document_delete.py`：增加删除时取消 worker、外部 source 不删除和取消后孤儿产物检查。
- Modify: `backend/tests/test_document_pipeline.py`：增加运行代次、dual/merged 不覆盖、失败/取消不暴露旧产物的测试。
- Modify: `backend/tests/test_pdfmathtranslate_worker.py`：增加完成后取消、worker 清理边界的 wrapper 测试。
- Modify: `backend/tests/test_routes_upload.py`：增加 partial upload 清理测试。
- Modify: `backend/tests/test_legacy_database_migration.py`：增加新增列补齐测试；若初始化逻辑移动，增加启动初始化测试。
- Create: `backend/tests/test_document_rename.py`：覆盖重命名对 artifact 引用的更新。
- Create: `backend/tests/test_counterpart_api.py`：覆盖 locate counterpart 路由分支。
- Create: `backend/tests/test_pdfmathtranslate_runner.py`：覆盖 runner 的产物发布和 scratch 清理。
- Create: `backend/tests/test_store.py`：覆盖完整记录 round-trip 及文件名函数。

### 前端

- Modify: `frontend/src/pages/ReaderPage.tsx`：所有 annotations、locate counterpart、pending locate 和删除/重命名后的异步写回绑定 `documentId`。
- Modify: `frontend/src/hooks/usePdfSearch.ts`：增加搜索请求序号/取消标志，旧请求不能覆盖新查询。
- Modify: `frontend/src/components/PdfPane.tsx`：单页翻页也上报阅读进度；跨 PDF 的异步定位和进度写回保留文档身份校验。
- Modify: `frontend/tests/readerSwitch.browser.mjs`：增加文档切换期间异步 locate/annotation/search 的浏览器回归场景（若现有脚本能力不足，先抽取无需新依赖的纯函数测试边界）。

---

### Task 1: 建立文档运行代次和原子状态转换

**Files:**
- Modify: `backend/app/models/store.py:199-553`
- Modify: `backend/app/services/document_pipeline.py:411-567`
- Modify: `backend/app/api/routes_document.py:45-228,387-392`
- Test: `backend/tests/test_retry_api.py`
- Test: `backend/tests/test_document_delete.py`
- Test: `backend/tests/test_document_pipeline.py`

**Interfaces:**
- Produces a per-run token or monotonically increasing `revision` stored with the document record and captured by each queued pipeline. Use the existing record/artifact persistence style; do not expose a new public API field unless required by existing artifact metadata.
- Produces atomic store helpers with explicit expected state/run arguments, for example `mark_document_failed(..., expected_revision=...)`, `mark_document_cancelled(..., expected_revision=...)`, and `clear_translated_artifact(..., expected_revision=...)`. Exact names may follow local conventions, but every caller must pass the captured run identity.
- `process_document()` must accept or obtain the run identity before starting work and must ignore stale writes from an older run.

- [ ] **Step 1: Add a failing test for deleting a queued/processing document.**

  Arrange a document with an active fake worker and assert that the delete path calls the worker cancellation hook before removing source/output files. Complete the fake worker after deletion and assert that no new artifacts are registered and no deleted document row is written back to an active state.

  Run: `pytest -q backend/tests/test_document_delete.py -k "processing or queued"`

  Expected: FAIL because deletion currently removes files without calling `cancel_worker()` and the worker can still publish.

- [ ] **Step 2: Add a failing test for a completed worker being cancelled.**

  Keep the worker handle in `_ACTIVE_RUNS` after it has emitted its finish event, call the cancel endpoint, and assert that the endpoint returns the existing completed-state conflict and leaves `status == "done"`.

  Run: `pytest -q backend/tests/test_retry_api.py -k "completed.*cancel or cancel.*completed"`

  Expected: FAIL because `cancel_worker()` currently treats the still-registered handle as cancellable without checking completion/state.

- [ ] **Step 3: Add a failing test for stale pipeline writes after retry.**

  Queue run A, advance the document to run B through retry, then let run A emit failure/cancel/progress. Assert that run A cannot change run B's status, stages, failure, or artifact list.

  Run: `pytest -q backend/tests/test_document_pipeline.py -k "stale or revision or generation"`

  Expected: FAIL because pipeline writes currently identify a document but not the run that owns the write.

- [ ] **Step 4: Implement the smallest store-level compare-and-set boundary.**

  Add the run identity to the document persistence model/schema following the repository's existing SQLite column-addition pattern. Add one transactionally guarded transition helper that updates only when `document_id`, expected status (where needed), and expected run identity match. Return a boolean/updated record so callers can stop stale work without raising a new user-facing error.

- [ ] **Step 5: Capture the run identity when queueing retry/reprocess and pass it through pipeline callbacks.**

  Change `retry_document`, `reprocess_document`, and `_run_retry_pipeline` so the background task receives the exact run identity returned by the atomic queue operation. Update `_StageSwitcher`, `_event_reporter`, `_fail`, `_cancel`, and artifact registration to use guarded writes.

- [ ] **Step 6: Coordinate cancellation with worker completion.**

  Make `WorkerRun` expose an unambiguous finished state set before removal from `_ACTIVE_RUNS`. `cancel_worker()` must return false for a finished run and the route must re-read the document status before changing it. On successful cancellation, the pipeline may clean up only the matching run's temporary artifacts and must not overwrite a completed result.

- [ ] **Step 7: Cancel before deleting and make deletion stale-write safe.**

  In `delete_document`, inspect the document state, call `cancel_worker()` for queued/processing documents using the captured run identity, then perform application-level artifact/source cleanup. A worker completion racing after the delete must fail the guarded write and must not recreate files or artifacts.

- [ ] **Step 8: Run the focused regression suite.**

  Run: `pytest -q backend/tests/test_document_delete.py backend/tests/test_retry_api.py backend/tests/test_document_pipeline.py`

  Expected: PASS, with all existing retry, cancellation, deletion, and pipeline tests preserved.

---

### Task 2: 修复 dual PDF、merged PDF 和重试旧译文引用

**Files:**
- Modify: `backend/app/models/store.py:115-158`
- Modify: `backend/app/services/document_pipeline.py:97-159,439-543`
- Modify: `backend/app/api/routes_document.py:57-75,310-320`
- Test: `backend/tests/test_document_pipeline.py`
- Test: `backend/tests/test_side_by_side_pdf.py`
- Test: `backend/tests/test_retry_api.py`
- Create: `backend/tests/test_store.py`

**Interfaces:**
- `dual_pdf_filename()` and `merged_pdf_filename()` must return distinct deterministic names for the same source document.
- A new run must remove or invalidate the previous translated PDF artifact and `translated_pdf_url` before worker execution; a failed/cancelled run must not expose the previous URL.
- `_publish_dual_pdf()` and `_publish_merged_pdf()` must register different paths and preserve both files when both products exist.

- [ ] **Step 1: Add a failing filename test.**

  Call the two filename helpers with the same source name and assert their results differ, both retain the intended PDF extension, and both remain stable for the same input.

  Run: `pytest -q backend/tests/test_store.py -k "dual or merged or filename"`

  Expected: FAIL because both helpers currently return `<source>_双语对照.pdf`.

- [ ] **Step 2: Add a failing pipeline test for simultaneous dual and merged products.**

  Use a worker stub that publishes a dual PDF, then run merged PDF creation. Assert that both artifact records point to existing distinct files and that reading either file returns its own marker bytes.

  Run: `pytest -q backend/tests/test_document_pipeline.py -k "dual.*merged or merged.*dual"`

  Expected: FAIL because merged output overwrites the dual path.

- [ ] **Step 3: Add a failing retry test for stale translated URL exposure.**

  Complete a document once, record its translated URL, queue a retry, force the retry worker to fail or cancel, then fetch the document. Assert that status is failed/cancelled and `translated_pdf_url` is absent/null; old output files and artifact records must not be returned as current products.

  Run: `pytest -q backend/tests/test_retry_api.py -k "translated.*url or old.*artifact"`

  Expected: FAIL because retry cleanup does not clear `record.translated_pdf_url`.

- [ ] **Step 4: Implement distinct filenames and update every producer/consumer.**

  Change only the filename helpers and call sites that construct dual/merged paths. Preserve existing URL construction and artifact metadata shape. Add a migration-free cleanup of the old colliding path when starting a new run only if it is an artifact owned by the current document.

- [ ] **Step 5: Clear translated output state at retry/reprocess start.**

  Extend the existing artifact purge/reset path to clear `translated_pdf_url` atomically with the new run state. Ensure `_publish_translated_pdf()` is the only path that restores it, and ensure cancellation/failure does not restore an older URL.

- [ ] **Step 6: Verify output and artifact invariants.**

  Run: `pytest -q backend/tests/test_store.py backend/tests/test_document_pipeline.py backend/tests/test_side_by_side_pdf.py backend/tests/test_retry_api.py`

  Expected: PASS; both products remain readable, and a failed second run exposes no successful first-run translated URL.

---

### Task 3: 修复 worker scratch 目录清理并补 runner 直接测试

**Files:**
- Modify: `workers/pdfmathtranslate/runner.py:338-489`
- Modify: `backend/app/services/pdf_translation_worker.py:173-222,420-519`
- Test: `backend/tests/test_pdfmathtranslate_worker.py`
- Create: `backend/tests/test_pdfmathtranslate_runner.py`

**Interfaces:**
- `runner.run()` and `_run_job()` must clean per-job scratch state on success, worker error, cancellation, and timeout-triggered termination.
- `keep_debug=True` preserves only the documented debug outputs; it must not preserve the entire scratch directory accidentally.
- Backend cancellation/timeout may terminate the process, but the runner must also handle termination cleanup where the process receives a signal before normal Python `finally` execution.

- [ ] **Step 1: Add direct runner tests for normal/error cleanup.**

  Import `workers.pdfmathtranslate.runner` with fake job/translator/build-manifest dependencies. Assert scratch files are absent after successful `run()` and after a translator exception; assert the expected error stage is emitted.

  Run: `pytest -q backend/tests/test_pdfmathtranslate_runner.py -k "cleanup or error"`

  Expected: FAIL because runner tests do not exist and current cleanup relies only on normal `finally` execution.

- [ ] **Step 2: Add a cancellation/termination cleanup test.**

  Create a subprocess using a minimal fake runner entrypoint, send the supported termination signal during a job, wait for exit, and assert the job scratch directory contains no intermediate JSON/PDF files except explicitly retained debug files.

  Run: `pytest -q backend/tests/test_pdfmathtranslate_runner.py -k "signal or cancel or terminate"`

  Expected: FAIL because no signal handler currently maps termination to scratch cleanup.

- [ ] **Step 3: Add backend wrapper tests for completed-run cancellation and timeout cleanup.**

  Use the existing fake worker fixtures in `test_pdfmathtranslate_worker.py`; after finish, call cancellation and assert no terminate/kill is issued. For a timeout, assert the process is stopped and the wrapper removes/invalidates its temporary job input according to the existing ownership boundary.

- [ ] **Step 4: Implement one cleanup function and signal-safe invocation.**

  Keep `_clean_work_dir()` as the single cleanup implementation. Register minimal `SIGTERM`/`SIGINT` handlers inside the worker process that mark cancellation and invoke cleanup before exiting; do not perform backend-only filesystem cleanup against paths the backend does not own. Preserve `keep_debug` behavior by copying the approved debug files before removing the scratch directory.

- [ ] **Step 5: Make worker state transitions idempotent.**

  Set finished/cancelled state before process removal, make repeated `cancel()`/`kill()` harmless, and ensure event reader shutdown cannot publish a second finish event. Keep stdout event format unchanged and send diagnostic logs to stderr.

- [ ] **Step 6: Run worker tests and compile check.**

  Run: `pytest -q backend/tests/test_pdfmathtranslate_worker.py backend/tests/test_pdfmathtranslate_runner.py && python -m compileall -q backend/app workers`

  Expected: PASS; no worker scratch artifacts remain after the tested terminal paths.

---

### Task 4: 修复上传 partial file 与数据库初始化副作用

**Files:**
- Modify: `backend/app/api/routes_upload.py:29-55`
- Modify: `backend/app/core/database.py:187-231`
- Modify: `backend/app/main.py:29-46`
- Test: `backend/tests/test_routes_upload.py`
- Test: `backend/tests/test_legacy_database_migration.py`

**Interfaces:**
- Failed upload writes must remove the partial target path and must not enqueue a document.
- Database schema initialization and queued/processing recovery must occur once during application startup, not as an import-time side effect.
- Direct database utility imports used by tests and scripts must still be able to initialize the schema explicitly through `init_database()`.

- [ ] **Step 1: Add a failing partial-upload test.**

  Monkeypatch the target file's write operation to raise `OSError` after writing one chunk. Assert the request returns the existing server error, the partial target does not exist, and the pipeline enqueue hook was not called.

  Run: `pytest -q backend/tests/test_routes_upload.py -k "partial or write"`

  Expected: FAIL because the current `upload()` path has no cleanup around the write loop.

- [ ] **Step 2: Add a failing initialization test.**

  Import the database module with a temporary database path and assert importing it does not mutate queued/processing rows. Call `init_database()` explicitly and assert schema creation plus recovery occurs once.

  Run: `pytest -q backend/tests/test_legacy_database_migration.py -k "import or initialize or recovery"`

  Expected: FAIL because `database.py` currently calls `init_database()` at import time.

- [ ] **Step 3: Implement upload cleanup with a narrow exception scope.**

  Track whether the target was created by this request. On `OSError` during chunk writes, close the file, unlink only that target if it still exists, and re-raise through the existing API error path. Do not catch unrelated pipeline exceptions as file-write failures.

- [ ] **Step 4: Move initialization to application startup.**

  Remove the module-level call from `database.py`. Call `init_database()` once from the existing `main.py` startup construction path before recovery-dependent services start. Keep direct test fixtures calling `init_database()` explicit where needed.

- [ ] **Step 5: Verify upload and migration behavior.**

  Run: `pytest -q backend/tests/test_routes_upload.py backend/tests/test_legacy_database_migration.py backend/tests/test_retry_api.py`

  Expected: PASS, including existing migration/recovery tests and the new cleanup/import tests.

---

### Task 5: 补齐后端路由、store 和 pipeline 的回归覆盖

**Files:**
- Modify: `backend/app/api/routes_document.py` only if tests expose a contract defect
- Modify: `backend/app/models/store.py` only if tests expose persistence defects
- Modify: `backend/app/services/document_pipeline.py` only if tests expose unguarded existing branches
- Create: `backend/tests/test_document_rename.py`
- Create: `backend/tests/test_counterpart_api.py`
- Create: `backend/tests/test_store.py`
- Modify: `backend/tests/test_document_pipeline.py`

**Interfaces:**
- Rename tests must assert database artifact `name/path/url` and existing files stay synchronized.
- Counterpart tests must cover invalid side, empty bilingual text, exact alignment, low confidence, legacy ratio fallback, and page ratio calculation.
- Store tests must cover complete record round-trip, normalized filenames, and source cleanup outside `upload_dir`.
- Pipeline tests must cover merge/annotation failure as best-effort, empty manifest failure, marker cleanup, title fallback, and metadata enrichment failure as non-fatal.

- [ ] **Step 1: Write route-level rename tests.**

  Add tests for a successful rename with translated/annotated/merged artifacts, missing derived files, invalid path-like names, and a source filename containing underscores. Assert only the document's own artifacts are renamed.

  Run: `pytest -q backend/tests/test_document_rename.py`

- [ ] **Step 2: Write route-level counterpart tests.**

  Use small manifest fixtures and the existing client fixture. Assert the exact HTTP status and response fields for valid alignment, low confidence, legacy fallback, invalid `source_side`, empty bilingual text, and page-count ratio handling.

  Run: `pytest -q backend/tests/test_counterpart_api.py`

- [ ] **Step 3: Write store round-trip and filename tests.**

  Save a `DocumentRecord` containing artifacts, references, stages, failure/chunks, retry count, metadata, reading progress, timestamps, and deletion state; clear any in-memory references and read it back from SQLite. Separately test normalization of names with underscores, path separators, empty names, long names, and extensions.

  Run: `pytest -q backend/tests/test_store.py`

- [ ] **Step 4: Add pipeline branch tests.**

  Force merged/annotated generation to raise and assert the document remains done without that artifact; exercise empty/unreadable manifests, missing-page marker removal, title fallback, successful metadata enrichment, existing metadata skip, and metadata lookup failure.

  Run: `pytest -q backend/tests/test_document_pipeline.py -k "merge or annotat or manifest or metadata or title or missing"`

- [ ] **Step 5: Run the complete backend suite.**

  Run: `pytest -q backend/tests`

  Expected: PASS with no new collection errors. Do not use repository-root `pytest -q` as the project verification command because packaged `desktop/worker-runtime` dependencies contain unrelated test files.

---

### Task 6: 绑定 ReaderPage 的跨文档异步操作

**Files:**
- Modify: `frontend/src/pages/ReaderPage.tsx:259-365,458-493`
- Modify: `frontend/src/components/PdfPane.tsx:531-570,647-729`
- Modify: `frontend/tests/readerSwitch.browser.mjs`

**Interfaces:**
- Every asynchronous callback that writes `annotations`, locate results, current document state, or pane highlights must capture the initiating `documentId` and verify it still matches before writing.
- `pendingLocate` must include `{ documentId, text, sourceSide }` rather than only text/direction.
- `PdfPane` imperative locate must reject stale PDF/document identity before applying highlights; progress writes must identify the current document.

- [ ] **Step 1: Add a browser regression for annotation isolation.**

  Start an annotation list/create/delete request for document A, switch to document B before the request resolves, and assert B's annotation list and visible state remain unchanged. Use the existing browser test harness and controllable network delay if available.

  Run: `npm --prefix frontend run build` followed by the existing browser test command documented in `frontend/tests/readerSwitch.browser.mjs`.

  Expected: FAIL before the guard because A's response calls `setAnnotations()` against B.

- [ ] **Step 2: Add a browser regression for counterpart isolation.**

  Start locate for A, switch to B before the locate response returns, and assert no navigation/highlight is applied to B. Also start a sidebar pending locate for A, switch to B while B loads, and assert the pending effect does not run on B.

- [ ] **Step 3: Implement document identity guards.**

  Capture `const requestDocumentId = activeId` before each asynchronous request. After every awaited call, compare it with the current active document and return without state/pane mutation if it differs. Store `documentId` in pending locate state and include it in the effect dependency/guard. Pass the expected document identity into pane locate calls where the imperative API permits it.

- [ ] **Step 4: Ensure deletion and rename callbacks do not resurrect stale state.**

  After delete/rename awaits, update summaries/cache/active selection only if the request still refers to the same document; if the user has switched documents, refresh summaries without replacing the current pane state.

- [ ] **Step 5: Run frontend verification.**

  Run: `npm --prefix frontend run build` and the existing `frontend/tests/readerSwitch.browser.mjs` browser test.

  Expected: PASS; switching documents during delayed requests leaves the active document's annotations, page, highlights, and active selection correct.

---

### Task 7: 修复 PDF 搜索竞态并补充阅读进度行为

**Files:**
- Modify: `frontend/src/hooks/usePdfSearch.ts:17-130`
- Modify: `frontend/src/components/PdfPane.tsx:647-729`
- Modify: `frontend/tests/readerSwitch.browser.mjs`
- Modify: `frontend/tests/pdfDarkMode.test.mjs` only if the existing test harness is appropriate for progress/async helper coverage

**Interfaces:**
- `usePdfSearch` must associate each `run(query)` with a monotonically increasing request id or abort signal. Only the newest request may update `matches`, `current`, and `searching`; empty query must invalidate prior requests and clear results.
- Single-page navigation must call the same debounced reading-progress persistence used by scroll mode, while programmatic restore/search/locate must not be mistaken for user reading progress.

- [ ] **Step 1: Add a failing stale-search test.**

  Control page-text promises so query A resolves after query B even though A started first. Assert the final matches and search state correspond to B, and that navigation never jumps to A's page.

  Run: `npm --prefix frontend run build` and the existing frontend browser test command.

  Expected: FAIL because `run()` unconditionally calls `setMatches(found)` and `setCurrent(0)` after completion.

- [ ] **Step 2: Add a failing single-page progress test.**

  In the browser reader flow, navigate pages in single-page mode without scrolling and assert the reading-progress request contains the new page/ratio. Then perform a programmatic restore and assert it does not create a user-progress update.

- [ ] **Step 3: Implement request invalidation in `usePdfSearch`.**

  Increment a ref counter for every query update. Capture the counter in `run()`, check it after every awaited `getPageText()` call and before each state update, and invalidate it on empty query, close, unmount, and PDF/document change. Preserve the existing maximum match count and keyboard navigation behavior.

- [ ] **Step 4: Unify progress persistence for scroll and single-page mode.**

  Keep one debounced persistence function keyed by the current document. Call it from user-driven page changes as well as scroll changes; keep the existing programmatic-scroll suppression and unmount flush. Do not send progress for a stale document after a PDF switch.

- [ ] **Step 5: Run frontend verification.**

  Run: `npm --prefix frontend run build` and the existing reader browser tests, including the new stale-search and single-page progress scenarios.

  Expected: PASS; older searches cannot overwrite newer results and both reader modes persist location consistently.

---

### Task 8: 端到端验收与审查结论复核

**Files:**
- No source files unless a preceding test reveals an unresolved defect.
- Review: all files listed in this plan and `AGENTS.md`.

**Interfaces:**
- The deliverable must pass the project's standard backend, syntax, and frontend build checks on the modified workspace.
- The original review scenarios must be exercised through real API/pipeline/browser calls, not only imports or static checks.

- [ ] **Step 1: Run focused backend suites again after all merges.**

  Run: `pytest -q backend/tests/test_document_delete.py backend/tests/test_retry_api.py backend/tests/test_document_pipeline.py backend/tests/test_pdfmathtranslate_worker.py backend/tests/test_pdfmathtranslate_runner.py backend/tests/test_routes_upload.py backend/tests/test_document_rename.py backend/tests/test_counterpart_api.py backend/tests/test_store.py`

  Expected: PASS.

- [ ] **Step 2: Run all standard backend checks.**

  Run: `pytest -q backend/tests` and `python -m compileall -q backend/app workers`.

  Expected: PASS. If root `pytest -q` still collects packaged runtime tests, report that as test-discovery scope rather than treating it as a backend regression.

- [ ] **Step 3: Run frontend build and browser checks.**

  Run: `npm --prefix frontend run build` and all existing browser scripts under `frontend/tests` using their documented commands.

  Expected: PASS with no TypeScript or Vite errors.

- [ ] **Step 4: Manually exercise the original scenarios.**

  Verify through the running app/API: delete a queued document, cancel a near-complete document, retry a completed document and force failure, generate both dual and merged PDFs, switch ReaderPage documents during delayed locate/annotation calls, enter searches rapidly, and navigate in single-page mode.

- [ ] **Step 5: Sweep stale comments and contracts.**

  Search for comments/docstrings describing old filename collisions, unconditional cancellation, import-time initialization, or scroll-only progress. Update or remove them so documentation matches the final behavior.

- [ ] **Step 6: Record final result.**

  Report modified files, test commands and results, any environment-dependent browser verification limitation, and any remaining issue in one `Known Issues` section. Do not claim completion while any standard check or original scenario remains unverified.

## Parallel Dispatch Guidance

可并行分派但必须先完成 Task 1 的运行代次接口设计：

- Task 2 可在 Task 1 的 artifact/状态接口确定后独立实现。
- Task 3 可独立实现 runner 清理，但 backend wrapper 的完成状态字段需与 Task 1 对齐。
- Task 4 可独立实现，除 `main.py` 启动初始化调用外不依赖文档运行代次。
- Task 5 应在 Task 2/Task 4 的接口稳定后运行，避免测试锁定旧文件名或旧初始化行为。
- Task 6 和 Task 7 可并行；两者只共享 `PdfPane.tsx` 时应由同一智能体串行合并，或先分别提交再人工解决冲突。
- Task 8 必须最后执行，不能用局部测试替代全套验收。

## Self-review

- 已覆盖审查中的高优先级问题：删除不取消、完成后仍可取消、dual/merged 覆盖、重试暴露旧译文、ReaderPage locate 跨文档污染。
- 已覆盖中优先级问题：批注跨文档污染、pending locate 缺 documentId、PDF 搜索旧查询覆盖新查询、worker scratch 遗留。
- 已覆盖低优先级问题：上传 partial file、数据库导入初始化副作用、单页模式阅读进度不更新。
- 未把“根目录 `pytest` 收集 packaged runtime 测试”作为产品代码修复；计划只要求使用项目规定的 `backend/tests` 范围验证，并在最终报告中明确该测试发现范围问题。
- 未将未证实的数据库外键行为、静态资源 MIME 或一般性防御性检查纳入修复范围；它们不属于本次审查已确认的用户可见缺陷。
- 计划中所有实现步骤均指定了文件、行为、失败测试和验证命令；没有引入新的依赖或未定义的公共接口。
