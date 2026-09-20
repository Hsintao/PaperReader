# PaperReader v2.2.0

English | [简体中文](docs/zh-cn/README.zh-cn.md)

[Download Windows / macOS v2.2.0](https://github.com/Mars-Dingdang/PaperReader/releases/tag/v2.2.0) · [Upgrade guide](docs/UPGRADING.md) · [Release notes](docs/releases/v2.2.0.md)

> 📖 **Please read the [User Guide (中文)](docs/user_instruction.md) before downloading.** It covers installation (including the Windows "unblock ZIP" step that prevents most launch failures), provider setup, paper submission, history, and artifacts.

v2.2.0 replaces the reflowed LaTeX translation with a page-faithful one: every translated block is pinned to the source paragraph's coordinates, figures, formulas, captions and table rules are reused from the original page, and the translated PDF keeps the source page size and page count. TeX Live is no longer required. See the [release notes](docs/releases/v2.2.0.md). Frontend/API package version: `2.2.0`.

![](./images/demo1.png)

PaperReader is a full-stack bilingual paper-reading app. Upload a PDF; PaperReader parses it (MinerU cloud API or the built-in text-layer parser), translates it with an LLM, lays the translation out on the source page — same page size, same page count, figures and formulas untouched — and lets you read both versions side by side.

## Desktop quick start

- **Windows**: download the ZIP from the release page, extract the complete archive, and run `PaperReader.exe`. If it fails to open, unblock the ZIP first — see the [User Guide](docs/user_instruction.md).
- **macOS (Apple Silicon)**: open the DMG and copy PaperReader to Applications.
- Both builds ask for your LLM and [MinerU](https://mineru.net/apiManage/docs) credentials in the in-app Settings dialog; everything else is bundled.
- Translated PDF generation requires a Chinese font on the host (macOS: Songti SC; Windows: SimSun; Linux: fonts-noto-cjk). No TeX installation is needed.
- Platform guides: [Windows](desktop/README_zh.md) · [macOS](desktop/README_macos_zh.md)

## Features

- No accounts and no sign-in: one local operator uses the whole app, with LLM / MinerU / parser / vision settings kept in a `0600` local file; stored keys are never returned to the frontend
- Persistent history in local SQLite; processed files reopen after a restart
- PDF-only upload: `.pdf` files are parsed with the MinerU cloud API or the built-in local text-layer extractor
- PDF parsing via the MinerU cloud API — no local OCR or GPU required
- Concurrent LLM translation with validated per-chunk checkpoints and automatic retry; failed documents resume from the last checkpoint instead of starting over
- Translation domain setting (computer science / medicine / general academic) that selects the translation prompt and its terminology rules; each domain keeps its own glossary, which accumulates the terms learned from translated documents and is consolidated on a fixed interval
- Page-faithful translation layout: every block is pinned to its source coordinates with the source's own font size, weight, alignment and line spacing; when the translation does not fit it first uses the whitespace below, then shrinks that block only, then continues into a region the parser dropped, and finally falls back to the source wording
- Figures, block formulas, captions and running heads are reused verbatim from the source page; tables keep their vector rules and only the translated cell text is replaced
- Blocks are verified after composition: any source text that survived removal reverts that block, and a page whose original text would be lost falls back to the untouched source page
- Optional vision-model adversarial check on each page (auto / manual review modes, off by default)
- Side-by-side original/translated PDF reader with outlines (bookmarks or backend-parsed section structure), selectable text, trackpad zoom, on-demand page rendering, and a progress bar with stage breakdown, ETA, and failure diagnosis
- In-document search (Ctrl/Cmd+F) with match navigation across the whole file
- Persistent colored annotations with optional notes, restored on reopen, exportable as a bilingual Markdown reading-notes file
- Reading-position memory: reopen a document where you left off
- Optional synced dual-pane scrolling driven by the bilingual alignment index; counterpart highlighting lands near the passage you selected instead of always at the start of the block
- Figure gallery: every parsed figure and table as a thumbnail strip that jumps to its page
- Library-wide full-text search across all parsed documents, opening the match at its location
- Paper metadata (title/authors/year/venue) via Semantic Scholar with one-click BibTeX export
- Artifact panel with reference preview and drag-into-PDF-pane
- Light / dark theme persisted locally
- Native desktop apps (WebView2 on Windows, WKWebView on macOS arm64)

## Documentation

| Document | Content |
| --- | --- |
| [User Guide (中文)](docs/user_instruction.md) | Installation, setup, and usage — read before downloading |
| [Developer Docs](docs/DEVELOPMENT.md) | Building from source, packaging and release process, project structure, environment variables, API reference |
| [Upgrade Guide](docs/UPGRADING.md) | Migrating between versions |
| [Release notes](docs/releases/) ([中文](docs/zh-cn/releases/)) | Per-version changes |
