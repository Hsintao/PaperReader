# PaperReader v2.2.0

English | [简体中文](docs/zh-cn/README.zh-cn.md)

[Download Windows / macOS v2.2.0](https://github.com/Mars-Dingdang/PaperReader/releases/tag/v2.2.0) · [Upgrade guide](docs/UPGRADING.md) · [Release notes](docs/releases/v2.2.0.md)

> 📖 **Please read the [User Guide (中文)](docs/user_instruction.md) before downloading.** It covers installation (including the Windows "unblock ZIP" step that prevents most launch failures), provider setup, paper submission, history, and artifacts.

v2.2.0 translates PDFs with PDFMathTranslate-next: parsing, translation and the translated PDF all come from one worker pass that keeps the source page size and page count and reuses the original page's figures, formulas and table rules. See the [release notes](docs/releases/v2.2.0.md). Frontend/API package version: `2.2.0`.

![](./images/demo1.png)

PaperReader is a full-stack bilingual paper-reading app. Upload a PDF; PaperReader runs it through a local PDFMathTranslate-next worker, which parses the pages, translates the text through your LLM endpoint and writes a translated PDF plus a structured manifest of every block, figure, table, caption and reference. PaperReader builds the outline, the reference list, the bilingual alignment and the annotated source PDF from that manifest, and the reader shows the original and the translation side by side.

## Desktop quick start

- **Windows**: download the ZIP from the release page, extract the complete archive, and run `PaperReader.exe`. If it fails to open, unblock the ZIP first — see the [User Guide](docs/user_instruction.md).
- **macOS (Apple Silicon)**: open the DMG and copy PaperReader to Applications.
- Both builds ask for your LLM API key in the in-app Settings dialog. The translator itself runs from a PDFMathTranslate-next runtime that is installed separately — see `desktop/requirements-worker.txt`.
- Translated PDFs carry their own Chinese fonts and the annotated source PDF is drawn with the fonts bundled with the app, so no host font and no TeX installation are needed.
- Platform guides: [Windows](desktop/README_zh.md) · [macOS](desktop/README_macos_zh.md)

## Features

- No accounts and no sign-in: one local operator uses the whole app, with the LLM, theme and reading settings kept in a `0600` local file; the stored key is never returned to the frontend
- Persistent history in local SQLite; processed files reopen after a restart
- PDF-only upload: `.pdf` files go to the PDFMathTranslate-next worker, which parses the pages, translates them through your LLM endpoint and writes the translated PDF in one pass
- The worker is a separate process with its own runtime (`desktop/requirements-worker.txt`): the backend never imports the translator, and one JSON job file describes each run
- Translation domain setting (computer science / medicine / general academic) that selects the translation prompt and its terminology rules; each domain keeps its own glossary, which accumulates the terms the translator extracts, is consolidated on a fixed interval, and is passed to the worker as job input
- The output keeps the source layout: the translation is typeset onto the source page, reusing the original figures, block formulas and table rules, with the source page size and page count
- The worker publishes a manifest (`paperreader-manifest-v1`) holding page geometry, typed blocks with source and translated text, protected spans (URLs, citations, numbers, inline formulas), figure and table captions and cells, references, and the logical-object to fragment mapping
- The outline, figure gallery, reference list, bilingual alignment index and annotated source PDF are all built from that manifest; the annotated PDF boxes every parsed region, including each figure and table caption
- Side-by-side original/translated PDF reader with outlines (bookmarks or backend-parsed section structure), selectable text, trackpad zoom, on-demand page rendering, and a progress bar with stage breakdown, ETA, and failure diagnosis
- In-document search (Ctrl/Cmd+F) with match navigation across the whole file
- Persistent colored annotations with optional notes, restored on reopen, exportable as a bilingual Markdown reading-notes file
- Reading-position memory: reopen a document where you left off
- Optional synced dual-pane scrolling driven by the bilingual alignment index; counterpart highlighting lands near the passage you selected instead of always at the start of the block
- Figure gallery: every parsed figure and table as a thumbnail strip that jumps to its page
- Library-wide full-text search across all parsed documents, opening the match at its location
- Paper metadata (title/authors/year/venue) via Semantic Scholar with one-click BibTeX export
- Light / dark theme persisted locally
- Native desktop apps (WebView2 on Windows, WKWebView on macOS arm64)

## Documentation

| Document | Content |
| --- | --- |
| [User Guide (中文)](docs/user_instruction.md) | Installation, setup, and usage — read before downloading |
| [Developer Docs](docs/DEVELOPMENT.md) | Building from source, packaging and release process, project structure, environment variables, API reference |
| [Upgrade Guide](docs/UPGRADING.md) | Migrating between versions |
| [Release notes](docs/releases/) ([中文](docs/zh-cn/releases/)) | Per-version changes |
