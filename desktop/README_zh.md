PaperReader v2.2.0 Windows 可移植版
============================

使用方法
--------

1. 解压整个 ZIP，不能只把 PaperReader.exe 单独复制出来。
2. 双击 PaperReader.exe。首次启动没有注册和登录，直接进入工作台。
3. 点击左侧栏工具栏最右侧的齿轮按钮打开「设置」，填写大模型 API Key、Base URL 与模型名称。配置保存在本机数据目录，只需填写一次。
4. 上传的论文、翻译结果和批注保存在 %LOCALAPPDATA%\PaperReader\data。
5. 目前只支持上传 PDF 文件（点击「新解析」选择文件）。处理流程为：PDF → PDFMathTranslate-next worker（页面解析 → 翻译 → 版式合成）→ 译文 PDF。译文沿用原稿的页幅与页数，图片、公式与表格线条取自原稿。
6. 翻译或版式合成失败时，可在进度面板点击“从此处重试”，也可在历史记录中点「重新处理」。worker 一次处理整篇论文，重试会从头重跑。如果上传时提示 worker 不可用，请先按「运行要求」装好翻译运行时。

运行要求
--------

- Windows 10/11 64 位，并需要 Microsoft WebView2 Runtime（大多数当前 Windows 安装已包含）。
- 接收者不需要安装 Python 或 Node.js。翻译与译文 PDF 由一个独立的 PDFMathTranslate-next 运行时执行，该运行时单独提供：
  - 打包时准备了 `desktop/worker-runtime` 的话，它会随包发布，启动器自动使用它；
  - 否则在 `%LOCALAPPDATA%\PaperReader\.config.env` 中把 `PDFMATHTRANSLATE_PYTHON` 指向一个装有 worker 依赖的解释器（依赖见 `desktop/requirements-worker.txt`），或用 `PDFMATHTRANSLATE_WORKER` 指向独立的 worker 可执行文件。
- 译文 PDF 与原文标注 PDF 自带字体，宿主不需要安装中文字体或 TeX。
- 打包时若 `release/` 下有 `offline_assets_*.zip`（BabelDOC 离线资产包），它会随包发布，启动时自动还原到 `%USERPROFILE%\.cache\babeldoc`，首次翻译无需联网；没有该包时 worker 首次运行会把版面模型、字体与 tiktoken 词表下载到该目录，此时该目录可写且能访问模型来源是首次翻译的前置条件。
- 不要分享 %LOCALAPPDATA%\PaperReader 下的隐藏配置或 data 用户数据：其中 settings.json 保存着你的 API Key。

分享方法
--------

直接分享 PaperReader-v2.2.0-Windows-x64.zip。每位使用者应在自己机器的「设置」中填写自己的 API 密钥。

开发者重新打包
--------------

在仓库根目录运行 `powershell -ExecutionPolicy Bypass -File .\desktop\build_portable.ps1`。
脚本默认从 PATH 查找 npm 和 Python；也可通过 `-NpmPath`、`-PythonPath` 指定路径。
要把翻译用的离线资产包（字体/模型，免首翻联网）一并打进压缩包，改用 `.\desktop\build_portable_offline.ps1`。

可移植包必须带独立 Python 运行时：使用 python-build-standalone 等 standalone distribution，安装 `desktop/requirements-worker.txt` 后放入 `desktop/worker-runtime`，并在其中提供 `runtime-manifest.json`（`runtime_type` 为 `python-standalone`）。普通 venv 会被打包脚本拒绝；没有该运行时就不会生成可移植发布包。

版本与升级
----------

发布标签为 v2.2.0，程序/前端版本为 2.2.0。下载包同时提供 SHA-256 校验文件。
EXE 未做 Authenticode 签名。本版本去掉了账号体系、TeX 工程上传与 AI 对话：首次启动会清空旧的用户/会话/项目数据，需要在「设置」中重新填写一次密钥，旧论文需要重新上传解析。文件仍保留在 data 目录下。详见 docs/UPGRADING.md。

重新构建前安装 Node.js 20 和 Python 3.11，然后运行：
python -m pip install -r desktop/requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\desktop\build_portable.ps1

解压包中的 create_shortcut.ps1 可用于创建直接指向 PaperReader.exe 的桌面快捷方式。
