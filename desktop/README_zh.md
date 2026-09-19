PaperReader v2.1.12 Windows 可移植版
============================

使用方法
--------

1. 解压整个 ZIP，不能只把 PaperReader.exe 单独复制出来。
2. 双击 PaperReader.exe。首次启动没有注册和登录，直接进入工作台。
3. 点击左侧栏工具栏最右侧的齿轮按钮打开「设置」，填写大模型 API Key、Base URL、模型，以及可选的 MinerU 设置。配置保存在本机数据目录，只需填写一次。
4. 上传的论文、翻译结果和批注保存在 %LOCALAPPDATA%\PaperReader\data。
5. 目前只支持上传 PDF 文件（点击「新解析」选择文件）。处理流程为：PDF → MinerU 云解析 → 翻译 → LaTeX 构建 → 译文 PDF；译文仍由 LaTeX 编译生成。
6. 翻译或 LaTeX 编译失败时，可在进度面板点击“从此处重试”；已完成的 MinerU 解析和译块不会重复调用。

运行要求
--------

- Windows 10/11 64 位，并需要 Microsoft WebView2 Runtime（大多数当前 Windows 安装已包含）。
- 接收者不需要安装 Python 或 Node.js。
- 要生成保持 LaTeX 排版的中文 PDF，电脑仍需安装 TeX Live，并确保 `latexmk` 与 XeLaTeX 可从 PATH 访问。
- 不要分享 %LOCALAPPDATA%\PaperReader 下的隐藏配置或 data 用户数据：其中 settings.json 保存着你的 API Key。

分享方法
--------

直接分享 PaperReader-v2.1.12-Windows-x64.zip。每位使用者应在自己机器的「设置」中填写自己的 API 密钥。

开发者重新打包
--------------

在仓库根目录运行 `powershell -ExecutionPolicy Bypass -File .\desktop\build_portable.ps1`。
脚本默认从 PATH 查找 npm 和 Python；也可通过 `-NpmPath`、`-PythonPath` 指定路径。

版本与升级
----------

发布标签为 v2.1.12，程序/前端版本为 2.1.12。下载包同时提供 SHA-256 校验文件。
EXE 未做 Authenticode 签名。本版本去掉了账号体系、TeX 工程上传与 AI 对话：首次启动会清空旧的用户/会话/项目数据，需要在「设置」中重新填写一次密钥，旧论文需要重新上传解析。文件仍保留在 data 目录下。详见 docs/UPGRADING.md。

重新构建前安装 Node.js 20 和 Python 3.11，然后运行：
python -m pip install -r desktop/requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\desktop\build_portable.ps1

解压包中的 create_shortcut.ps1 可用于创建直接指向 PaperReader.exe 的桌面快捷方式。
