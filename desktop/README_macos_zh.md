# PaperReader v2.2.0 macOS（Apple Silicon）

## 安装与首次启动

1. 下载 `PaperReader-v2.2.0-macOS-arm64.dmg` 及对应 `.sha256`，校验后打开 DMG。
2. 将 PaperReader 拖入“应用程序”。本版本使用临时签名但未经过 Apple 公证；如果首次打开被拦截，请在 Finder 中按住 Control 点击应用并选择“打开”。
3. 首次启动没有注册和登录，直接进入工作台。点击左侧栏工具栏最右侧的齿轮按钮打开「设置」，填写大模型 API Key、Base URL、模型，以及可选的 MinerU 参数；配置只需填写一次。

应用数据、隐藏配置和日志位于 `~/Library/Application Support/PaperReader`。其中的 `data/settings.json` 保存着你的 API Key，不要分享该目录或包含私人论文的数据。

## 阅读体验

- 上传 PDF 后，通过 MinerU 云解析（或本地文字层解析）、翻译、版式合成得到译文 PDF，原文与译文可对照阅读，且逐页对齐。
- Ctrl/Cmd+F 文档内搜索；可持久化的彩色批注与备注，并能导出 Markdown 阅读笔记；阅读位置记忆；双栏联动滚动；侧栏跨文档全文搜索；BibTeX 导出；图表墙。PDF 渲染按需加载，长文档更流畅。
- 翻译或版式合成失败时，可在进度面板点击“从此处重试”。已完成的结构化解析与译块由 checkpoint 复用；合成失败的块回退为原稿内容，失败的页整页回退为原稿页。
- PDF 中的 HTTP(S) 外链由系统默认浏览器打开，PaperReader 保持当前论文和阅读位置；PDF 内部章节/页码链接仍在阅读器中跳转。
- 视觉校验默认关闭，可在「设置 → 阅读偏好」中开启自动或人工复核。

## 运行要求

- Apple Silicon Mac（arm64），macOS 13 或更高版本。
- APP 已包含 Python 后端、WKWebView 窗口和前端资源，不需要另装 Python 或 Node.js。
- 生成译文 PDF 不再需要 MacTeX/TeX Live；译文排版使用系统中文字体，macOS 自带的 Songti SC 即可。
- LLM 与 MinerU 解析需要网络及用户自己的服务密钥。

## 开发者构建

在 Apple Silicon Mac、Python 3.11 与 Node.js 20 环境执行：

```sh
python -m pip install -r desktop/requirements-build.txt
./desktop/build_macos.sh
```

脚本会构建前端、生成 `.app`、执行 ad-hoc 签名并输出 DMG 与 SHA-256 文件。
