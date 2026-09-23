# PaperReader v2.2.0 macOS（Apple Silicon）

## 安装与首次启动

1. 下载 `PaperReader-v2.2.0-macOS-arm64.dmg` 及对应 `.sha256`，校验后打开 DMG。
2. 将 PaperReader 拖入“应用程序”。本版本使用临时签名但未经过 Apple 公证；如果首次打开被拦截，请在 Finder 中按住 Control 点击应用并选择“打开”。
3. 首次启动没有注册和登录，直接进入工作台。点击左侧栏工具栏最右侧的齿轮按钮打开「设置」，填写大模型 API Key、Base URL 与模型名称；配置只需填写一次。

应用数据、隐藏配置和日志位于 `~/Library/Application Support/PaperReader`。其中的 `data/settings.json` 保存着你的 API Key，不要分享该目录或包含私人论文的数据。

## 阅读体验

- 上传 PDF 后，由本机的 PDFMathTranslate-next worker 完成页面解析、翻译与版式合成并输出译文 PDF；译文沿用原稿页幅与页数，原文与译文可对照阅读，且逐页对齐。
- Ctrl/Cmd+F 文档内搜索；可持久化的彩色批注与备注，并能导出 Markdown 阅读笔记；阅读位置记忆；双栏联动滚动；侧栏跨文档全文搜索；BibTeX 导出；图表墙。PDF 渲染按需加载，长文档更流畅。
- 翻译或版式合成失败时，可在进度面板点击“从此处重试”，也可在历史记录中点「重新处理」。worker 一次处理整篇论文，重试会从头重跑。上传时提示 worker 不可用，说明还没找到翻译运行时，见「运行要求」。
- PDF 中的 HTTP(S) 外链由系统默认浏览器打开，PaperReader 保持当前论文和阅读位置；PDF 内部章节/页码链接仍在阅读器中跳转。

## 运行要求

- Apple Silicon Mac（arm64），macOS 13 或更高版本。
- APP 已包含 Python 后端、WKWebView 窗口和前端资源，不需要另装 Python 或 Node.js。
- 翻译与译文 PDF 由一个独立的 PDFMathTranslate-next 运行时执行，该运行时单独提供：
  - 打包时准备了 `desktop/worker-runtime` 的话，它会被复制到 `PaperReader.app/Contents/Resources/worker-runtime`，启动器自动使用它；
  - 否则在 `~/Library/Application Support/PaperReader/.config.env` 中把 `PDFMATHTRANSLATE_PYTHON` 指向一个装有 worker 依赖的解释器（依赖见 `desktop/requirements-worker.txt`），或用 `PDFMATHTRANSLATE_WORKER` 指向独立的 worker 可执行文件。
- 译文 PDF 与原文标注 PDF 自带字体，宿主不需要安装中文字体或 MacTeX/TeX Live。
- worker 首次运行时会把版面模型、字体与 tiktoken 词表下载到 `~/.cache/babeldoc`；该目录可写且能访问模型来源，是首次翻译的前置条件。需要离线首跑时，把已下载的 `~/.cache/babeldoc` 一并复制到目标机器即可。
- 翻译需要网络及用户自己的大模型服务密钥。

## 开发者构建

在 Apple Silicon Mac、Python 3.11 与 Node.js 20 环境执行：

```sh
python -m pip install -r desktop/requirements-build.txt
./desktop/build_macos.sh
```

脚本会构建前端、生成 `.app`、执行 ad-hoc 签名并输出 DMG 与 SHA-256 文件。

可移植包必须带独立 Python 运行时：使用 python-build-standalone 等 standalone distribution，安装 `desktop/requirements-worker.txt` 后放入 `desktop/worker-runtime`，并在其中提供 `runtime-manifest.json`（`runtime_type` 为 `python-standalone`）。普通 venv 会被打包脚本拒绝；没有该运行时就不会生成可移植发布包。
