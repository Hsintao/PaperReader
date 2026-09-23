# PaperReader 开发者文档：Windows / macOS 本地构建与 Release 发布

## v2.2.0 阅读器回归

```powershell
conda activate pt
$env:PYTHONPATH = "backend"
python -m pytest backend/tests -q
npm.cmd --prefix frontend run build
```

`backend/tests/test_document_structure.py` 覆盖 Figure/Table 顺序、引用文件、双 PDF 页码、标题行优先于正文引用的定位，以及插图/表格线的紧裁剪缩略图（原文与译文双侧）。将 `Denoise.pdf` 与 `Denoise.tar.gz` 放到本地 `testexamples/` 后，同一测试命令还会检查该论文的 11 个 Figure、13 个 Table 及对应页码；CI 无样例时跳过此项。`testexamples/` 由 Git 忽略。

选区回归路径：原文与译文分别在连续滚动、单页模式下拖选文字，普通右键或 macOS Control-click 打开菜单后选区应保持可见；检查“问 AI”、对照定位与批注保存使用原选中文本，备注输入框可正常输入；关闭菜单后可重新拖选。`Page` 的 `onRenderTextLayerSuccess` 回调应保持稳定，菜单状态或备注变化不能重建文字层。

浏览器回归路径：在已显示文字的页首次执行对照高亮；跳转到未渲染的远端页；点击批注查看备注；打开图表浮层，分别点击 Figure、Table 及同页多个 Table；切换论文并确认列表更新。文字高亮在 `onRenderTextLayerSuccess` 后绘制。图表结构接口返回原文 `figures` 与译文 `translated_figures`，预览位于 `outputs/<document_id>/figure-previews/`，译文 PDF 更新后刷新。缩略图按标题旁的矢量插图、嵌入图片与表格横线裁剪。

设置密钥回归需验证大模型更新、留空保持和明确删除三项。未配置大模型 Key 时上传返回 `config_required`，找不到 worker 解释器时上传返回 `worker_unavailable`；两种情况下前端都会直接打开「设置」。

本文档面向需要在 **Windows 或 macOS 本地从源代码运行、构建 PaperReader 原生应用**，以及维护 GitHub Release 的开发者。Windows 部分的命令行均以 **Git Bash**（Git for Windows 自带）为准。

PaperReader 当前桌面端不是 Electron/Tauri，而是以下组合：

- 前端：React + TypeScript + Vite
- 后端：FastAPI + Uvicorn
- 原生窗口：pywebview（macOS 使用 WKWebView，Windows 使用 WebView2）
- macOS 打包：py2app
- Windows 打包：PyInstaller
- 分发格式：macOS 为 `.app` + `.dmg`；Windows 为可移植版 ZIP
- CI / Release：GitHub Actions

当前 macOS 包只面向 **Apple Silicon (`arm64`)**，最低系统版本为 **macOS 13**。当前 Windows 包只面向 **x64**，最低系统为 **Windows 10 64 位**（需 WebView2 Runtime）。

---

## 1. 环境要求

建议使用：

- Apple Silicon Mac（`arm64`）
- macOS 13+
- Git
- Python **3.11**
- Node.js **20**
- npm
- Xcode Command Line Tools
- 翻译需要一份 PDFMathTranslate-next 运行时（PyPI 包 `pdf2zh-next` + `babeldoc`）：见 `desktop/requirements-worker.txt`，Windows 构建环境一节同样适用

> `desktop/build_macos.sh` 与 `desktop/setup_macos.py` 共用同一个 `python`：脚本从该解释器推导 bundle 内的版本目录（`Resources/lib/python<major>.<minor>`），并把 conda 的运行时库补拷进 `Contents/Frameworks`。因此构建前务必确认 `python -V` 就是你要用来打包的那个环境，并且必须是 arm64；x86_64 Python 仍然不要用于正式包。
>
> 补拷的运行时库名字要匹配 `lib-dynload/*.so` 里的 `@rpath` 引用（例如 `_sqlite3.so` 要的是 `libsqlite3.0.dylib`，而不是 `libsqlite3.dylib`）。漏拷或漏配名字时 app 只会在启动时弹一个 `Launch error`，所以脚本在拷贝后会校验这些库是否都已落到 `Contents/Frameworks`，缺失就当场失败并报出库名。

检查环境：

```bash
uname -m
python3.11 --version
node --version
npm --version
git --version
```

预期至少满足：

```text
arm64
Python 3.11.x
v20.x.x
```

如果尚未安装 Xcode Command Line Tools：

```bash
xcode-select --install
```

译文 PDF 由 worker 生成并自带嵌入字体；原文标注 PDF 由后端绘制，使用内置的 Noto Serif/Sans SC（`backend/app/assets/fonts`）。两者都不需要 TeX，也不需要宿主机安装中文字体。翻译在独立的 worker 进程里进行，它的依赖装在单独的运行时中：

```bash
python3.13 -m venv worker-runtime
worker-runtime/bin/pip install -r desktop/requirements-worker.txt
```

`desktop/launcher.py` 按 `PDFMATHTRANSLATE_WORKER` → `PDFMATHTRANSLATE_PYTHON` → bundle 内的 `worker-runtime` → `python3` 依次解析运行 worker 的解释器；开发时把 `.env` 里的 `PDFMATHTRANSLATE_PYTHON` 指向上面这个环境即可。写成 `worker-runtime/bin/python3` 这样的相对路径时按仓库根目录解析，只有裸名字（如 `python3`）才按 `PATH` 查找。

### Windows（Git Bash）构建环境

- Windows 10/11 64 位（x64）
- Git for Windows（提供 Git Bash）
- Python **3.11**（x64 版本，安装时勾选 "Add python.exe to PATH"）
- Node.js **20**
- npm
- Microsoft WebView2 Runtime（大多数 Windows 10/11 已内置）
- 翻译需要一份 PDFMathTranslate-next 运行时（PyPI 包 `pdf2zh-next` + `babeldoc`），见 `desktop/requirements-worker.txt`

> `desktop/build_portable.ps1` 使用 PyInstaller 按 Python 3.11 / x64 构建，与 CI 的 Windows job 一致。Windows 本地构建的完整步骤见下文「在 Windows 上本地构建可移植版（Git Bash）」一节。

---

## 2. 获取源代码

```bash
git clone https://github.com/Mars-Dingdang/PaperReader.git
cd PaperReader
```

如果仓库已经存在：

```bash
git checkout main
git pull --ff-only origin main
```

---

## 3. 创建 Python 环境

### 方案 A：使用独立 conda 环境

```bash
conda create -n paperreader-dev python=3.11 -y
conda activate paperreader-dev
```

如果你已经按照仓库开发约定使用 `pt` 环境，也可以：

```bash
conda activate pt
python --version
```

但必须确认输出为 Python 3.11.x。

### 方案 B：使用 venv

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

Windows Git Bash 下没有 `python3.11` 命令，且 venv 的激活脚本位于 `Scripts` 目录（不是 macOS/Linux 的 `bin`）：

```bash
python -m venv .venv
source .venv/Scripts/activate
```

然后安装 Python 构建依赖：

```bash
python -m pip install --upgrade pip
python -m pip install -r desktop/requirements-build.txt
```

`desktop/requirements-build.txt` 会同时安装：

- 后端运行依赖
- `pywebview`
- macOS 下的 `py2app`
- Windows 下的 `pyinstaller`，以及固定的 `pythonnet 3.0.5` / `clr-loader 0.2.7.post0`（pythonnet 3.1.0 的 `Python.Runtime.dll` 无法在 PyInstaller 冻结环境中由 .NET Framework 宿主初始化，窗口打不开，`PaperReader-error.log` 中会出现 `Failed to resolve Python.Runtime.Loader.Initialize`）
- Windows 下的 `setuptools` 固定 `65.5.0`（80.x 的 vendored `jaraco.context` 在 Python 3.11 冻结打包时缺少 `backports.tarfile`，EXE 一启动就崩溃）

---

## 4. 安装前端依赖

仓库包含 `frontend/package-lock.json`，正常开发和 CI 构建优先使用：

```bash
npm --prefix frontend ci
```

如果你修改了 `frontend/package.json` 并需要重新生成 lock file，则使用：

```bash
npm --prefix frontend install
```

然后把 `frontend/package-lock.json` 一并提交。

---

# 5. 从源代码直接打开原生 macOS 应用

这是调试 **完整桌面形态** 最直接的方法。

首先构建前端：

```bash
npm --prefix frontend run build
```

该命令会生成：

```text
frontend/dist/
```

然后直接运行桌面启动器：

```bash
python desktop/launcher.py
```

启动器会：

1. 准备 PaperReader 的本地配置和数据目录；
2. 解析翻译 worker 的解释器（`PDFMATHTRANSLATE_WORKER` → `PDFMATHTRANSLATE_PYTHON` → 仓库根目录或 bundle 内的 `worker-runtime` → `python3`）；
3. 在 `127.0.0.1:8000` 启动内嵌 FastAPI/Uvicorn 服务；
4. 使用 pywebview 创建原生 macOS 窗口；
5. 通过 WKWebView 显示 `frontend/dist` 中的前端；
6. 关闭窗口后停止内嵌后端。

翻译要能跑通，仓库根目录需要有一个 `worker-runtime/`（或在其 `.config.env` 中指定 `PDFMATHTRANSLATE_PYTHON`）：

```bash
python3.13 -m venv worker-runtime
worker-runtime/bin/pip install -r desktop/requirements-worker.txt
```

应用数据默认位于：

```text
~/Library/Application Support/PaperReader/
```

其中包括配置、数据库、WebView 状态以及运行时数据。

如果启动异常，检查：

```text
~/Library/Application Support/PaperReader/PaperReader-error.log
```

### 端口 8000 被占用

检查：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

确认是旧 PaperReader/uvicorn 进程后，再结束对应 PID：

```bash
kill <PID>
```

然后重新运行：

```bash
python desktop/launcher.py
```

> 原生 source-run 模式读取的是已经构建好的 `frontend/dist`。修改前端后，需要重新执行 `npm --prefix frontend run build` 才会进入原生窗口。

---

# 6. Web 开发模式（前端热更新）

如果主要修改 React UI，推荐使用 Vite 开发服务器，而不是每次重新打包原生窗口。

首次准备：

```bash
cp .env.example .env
npm --prefix frontend ci
python -m pip install -r requirements.txt
```

根据需要填写 `.env` 中的开发配置，其中 `PDFMATHTRANSLATE_PYTHON` 要指向装有 worker 依赖的解释器（第 1 节 / 19.3 节），否则上传会返回 `worker_unavailable`。

> Windows Git Bash 通常没有 `make`，直接使用下方各自的等价命令即可；`cp .env.example .env` 在 Git Bash 中同样可用。

终端 1：启动 FastAPI：

```bash
make backend
```

等价命令：

```bash
cd backend
uvicorn app.main:app --reload
```

终端 2：启动 Vite：

```bash
make frontend
```

等价命令：

```bash
cd frontend
npm run dev
```

浏览器打开：

```text
http://localhost:5173
```

这种模式适合快速修改 UI；准备正式 `.app` 前仍应做一次原生桌面运行或打包测试。

## 6.1 一键启动（单进程，FastAPI 托管前端）

不改前端、只想在浏览器里用完整功能时，用单进程模式：FastAPI 直接托管 `frontend/dist`，只开一个端口，不需要 Node 常驻。

```bash
make web
```

等价命令：

```bash
bash scripts/start_web.sh
```

脚本会依次处理：

- 缺少 `.env` 时从 `.env.example` 复制一份，并提示填写 `OPENAI_API_KEY` 以及把 `PDFMATHTRANSLATE_PYTHON` 指向装有 worker 依赖的解释器；
- 没有配置 `PDFMATHTRANSLATE_WORKER` 时，用 `PDFMATHTRANSLATE_PYTHON` 指向的解释器试导入 `pdf2zh_next`/`babeldoc`，失败就打印警告（否则只会在上传后表现为文档失败）；
- 选择解释器：当前环境能 `import uvicorn, fastapi` 就直接用，否则依次尝试 `conda activate pt`；
- 端口按 `8000` → `8004` 取第一个空闲端口；若该端口上已有 PaperReader 在跑，直接打开它而不重复启动；也可用 `PAPERREADER_PORT=8010 bash scripts/start_web.sh` 指定端口；
- `frontend/dist` 缺失或比 `frontend/src` 旧时，自动执行 `npm --prefix frontend run build`；
- 启动服务并等 `/health` 返回 PaperReader 后打开浏览器，`Ctrl+C` 停止。

终端里打印的 `http://127.0.0.1:<port>` 就是访问地址。

---

# 7. 在 macOS 本地构建 `.app` 和 `.dmg`

先确认当前环境：

```bash
uname -m
python --version
node --version
```

然后安装构建依赖：

```bash
python -m pip install -r desktop/requirements-build.txt
```

执行完整构建：

```bash
chmod +x desktop/build_macos.sh
./desktop/build_macos.sh
```

脚本会依次执行：

1. `npm ci`
2. `npm run build`
3. 生成 `.icns`
4. 使用 `py2app` 构建原生 `.app`
5. 补入运行时 Python 包/动态库
6. 若存在 `desktop/worker-runtime`，把它复制到 `Contents/Resources/worker-runtime`（在签名之前，否则签名失效）
7. 对 `.app` 执行 ad-hoc `codesign`
8. 使用 `hdiutil` 制作压缩 DMG
9. 生成 SHA-256 文件

应用版本来自：

```text
frontend/package.json -> version
```

例如版本为 `2.1.2` 时，主要输出为：

```text
dist/PaperReader.app
release/PaperReader-v2.1.2-macOS-arm64.dmg
release/PaperReader-v2.1.2-macOS-arm64.dmg.sha256
```

直接打开构建出的 APP：

```bash
open dist/PaperReader.app
```

打开 DMG：

```bash
open release/PaperReader-v2.1.2-macOS-arm64.dmg
```

---

# 8. 在 Windows 上本地构建可移植版（Git Bash）

Windows 版 PaperReader 不是安装器，而是 PyInstaller 打包的 **可移植版**：解压 ZIP 后直接运行 `PaperReader.exe`，最终用户不需要安装 Python 或 Node.js。

以下命令全部在 **Git Bash**（Git for Windows 自带）中执行。

## 8.1 从源码直接打开原生 Windows 应用

调试完整桌面形态时，先构建前端，再运行与 macOS 相同的桌面启动器：

```bash
npm --prefix frontend run build
python desktop/launcher.py
```

启动器会：

1. 准备 PaperReader 的本地配置和数据目录；
2. 解析翻译 worker 的解释器（`PDFMATHTRANSLATE_WORKER` → `PDFMATHTRANSLATE_PYTHON` → `worker-runtime` → `python3`）；
3. 在 `127.0.0.1:8000` 启动内嵌 FastAPI/Uvicorn 服务；
4. 使用 pywebview 创建原生 Windows 窗口（WebView2 / EdgeChromium）；
5. 显示 `frontend/dist` 中的前端；
6. 关闭窗口后停止内嵌后端。

翻译要能跑通，仓库根目录需要有一个 `worker-runtime/`（或在其 `.config.env` 中指定 `PDFMATHTRANSLATE_PYTHON`）：

```bash
python -m venv worker-runtime
worker-runtime/Scripts/pip install -r desktop/requirements-worker.txt
```

应用数据默认位于：

```text
%LOCALAPPDATA%\PaperReader\
```

Git Bash 中即 `~/AppData/Local/PaperReader/`，其中包括配置、数据库、WebView 状态以及运行时数据。启动异常时检查：

```text
%LOCALAPPDATA%\PaperReader\PaperReader-error.log
```

### 端口 8000 被占用

```bash
netstat -ano | grep :8000 | grep LISTENING
```

输出最后一列是 PID。确认是旧 PaperReader/uvicorn 进程后结束它（Git Bash 中 `taskkill` 的参数斜杠要写成 `//`，避免被 MSYS 转换成路径）：

```bash
taskkill //PID <PID> //F
```

然后重新运行 `python desktop/launcher.py`。

> 原生 source-run 模式读取的是已经构建好的 `frontend/dist`。修改前端后，需要重新执行 `npm --prefix frontend run build` 才会进入原生窗口。

## 8.2 构建可移植版 ZIP

安装构建依赖（`desktop/requirements-build.txt` 通过 `sys_platform` 在 Windows 下安装 PyInstaller）：

```bash
python -m pip install -r desktop/requirements-build.txt
```

构建脚本是 PowerShell 脚本，从 Git Bash 直接调用 `powershell.exe` 执行：

```bash
powershell -ExecutionPolicy Bypass -File ./desktop/build_portable.ps1
```

如果 `python` 或 `npm` 不在 PATH，可显式指定路径：

```bash
powershell -ExecutionPolicy Bypass -File ./desktop/build_portable.ps1 \
  -PythonPath "C:/Python311/python.exe" \
  -NpmPath "C:/Program Files/nodejs/npm.cmd"
```

脚本会依次执行：

1. `npm ci`
2. `npm run build`
3. 使用 PyInstaller（`desktop/PaperReader.spec`）打包 `PaperReader.exe`，并打入 `workers/` 包
4. 若存在 `desktop\worker-runtime`，把它复制到可移植目录下的 `worker-runtime`
5. 把 `desktop/README_zh.md` 复制为包内 `使用说明.txt`，并放入 `create_shortcut.ps1`
6. 压缩为 ZIP 并生成 SHA-256 文件

应用版本同样来自 `frontend/package.json -> version`。例如版本为 `2.1.2` 时，主要输出为：

```text
dist/PaperReader/PaperReader.exe
release/PaperReader-v2.1.2-Windows-x64.zip
release/PaperReader-v2.1.2-Windows-x64.zip.sha256
```

直接运行打包出的 EXE：

```bash
./dist/PaperReader/PaperReader.exe
```

> 脚本内部会自行执行 `npm ci` 和 `npm run build`，因此打包前不需要单独构建前端。

## 8.3 构建后校验

与 CI 的 Windows job 一致的 smoke test：

```bash
python scripts/smoke_release.py --web
python scripts/smoke_release.py --archive release/PaperReader-v2.1.2-Windows-x64.zip
```

校验 ZIP 的 SHA-256（Git Bash 自带 `sha256sum`）：

```bash
sha256sum release/PaperReader-v*-Windows-x64.zip
cat release/PaperReader-v*-Windows-x64.zip.sha256
```

比较两处输出的 SHA-256 digest 是否一致。

后端测试与 Python 语法检查与 macOS 相同：

```bash
python -m pytest backend/tests -q
python -m compileall -q backend/app desktop/launcher.py
```

---

## 9. macOS 构建后校验

### 9.1 检查签名

```bash
codesign --verify --deep --strict --verbose=2 dist/PaperReader.app
```

当前脚本使用的是 **ad-hoc signing**：

```bash
codesign --force --deep --sign - dist/PaperReader.app
```

这不是 Developer ID 签名，也没有经过 Apple Notarization。

因此通过 GitHub Release 分发后，用户第一次启动时可能需要：

1. Finder 中找到 PaperReader；
2. Control-click / 右键；
3. 选择“打开”。

### 9.2 运行仓库自带 smoke test

GitHub Actions 对正式 macOS 构建执行的是：

```bash
python scripts/smoke_release.py --app dist/PaperReader.app
```

本地准备 Release 前也推荐执行相同检查：

```bash
python scripts/smoke_release.py --app dist/PaperReader.app
```

### 9.3 后端测试

```bash
python -m pytest backend/tests -q
```

### 9.4 Python 语法检查

```bash
python -m compileall -q backend/app workers desktop/launcher.py
```

### 9.5 前端生产构建

```bash
npm --prefix frontend run build
```

---

## 10. 校验 DMG SHA-256

构建后可执行：

```bash
DMG=$(ls release/PaperReader-v*-macOS-arm64.dmg | head -n 1)
shasum -a 256 "$DMG"
cat "$DMG.sha256"
```

比较两处输出的 SHA-256 digest 是否一致。

> 当前 `build_macos.sh` 在 checksum 文件中写入的是构建机上的 DMG 路径，因此把 `.dmg` 与 `.sha256` 下载到另一台机器后，直接运行 `shasum -c` 可能因为路径不同而失败。此时比较 digest 本身即可。

---

# 11. GitHub Actions 当前 Release 流程

仓库已经配置：

```text
.github/workflows/release.yml
```

普通 `main` push / PR 会执行测试和打包检查；当 push 的 tag 匹配：

```text
v2.*
```

时，会执行完整 Release 流程。

主要阶段：

1. Linux / Windows / macOS 后端兼容性测试；
2. Windows x64 portable 构建 + smoke test；
3. macOS arm64 APP/DMG 构建 + smoke test；
4. 下载两个平台的 workflow artifacts；
5. 自动创建新的 GitHub Release，或在同名 Release 已存在时更新说明并替换旧资产；
6. 将 Windows ZIP、macOS DMG 和 checksum 文件作为 Release assets 上传。

正式 Release 由该 GitHub Actions 流程自动发布。

---

# 12. 发布一个新的 Release

## 12.1 版本号 / Tag 约定

workflow 要求 Git tag 与 `frontend/package.json` 的完整 SemVer 严格一致：

```bash
VERSION=$(python -c "import json; print(json.load(open('frontend/package.json'))['version'])")
test "$RELEASE_TAG" = "v$VERSION"
```

也就是说：

```text
frontend/package.json: 2.1.2
Git tag:               v2.1.2
Release notes:         docs/releases/v2.1.2.md
```

旧的 `v2.1` 短标签保持不动；所有新版本（包括 patch release）都使用完整三段版本号。

---

## 12.2 示例：发布 v2.1.2

假设准备发布：

```text
应用版本：2.1.2
Git tag：v2.1.2
```

### Step 1：更新前端版本号

推荐让 npm 同时维护 `package.json` 和 `package-lock.json`：

```bash
cd frontend
npm version 2.1.2 --no-git-tag-version
cd ..
```

确认：

```bash
node -p "require('./frontend/package.json').version"
```

应该输出：

```text
2.1.2
```

### Step 2：创建 Release Notes

创建：

```text
docs/releases/v2.1.2.md
```

文件名必须与 Git tag 一致，因为 workflow 会直接读取：

```text
docs/releases/$RELEASE_TAG.md
```

可以参考已有：

```text
docs/releases/v2.1.2.md
```

### Step 3：本地验证

至少执行：

```bash
python -m pytest backend/tests -q
python -m compileall -q backend/app desktop/launcher.py
npm --prefix frontend run build
```

如果本机是 Apple Silicon Mac，建议额外执行：

```bash
./desktop/build_macos.sh
python scripts/smoke_release.py --app dist/PaperReader.app
```

如果本机是 Windows，建议额外执行（Git Bash）：

```bash
powershell -ExecutionPolicy Bypass -File ./desktop/build_portable.ps1
python scripts/smoke_release.py --web
python scripts/smoke_release.py --archive release/PaperReader-v*-Windows-x64.zip
```

### Step 4：提交 Release 准备变更

```bash
git status
git add frontend/package.json frontend/package-lock.json docs/releases/v2.1.2.md
git commit -m "prepare v2.1.2 release"
git push origin main
```

推荐先等待 `main` 对应的 GitHub Actions 全部通过。

### Step 5：创建并 push tag

```bash
git checkout main
git pull --ff-only origin main
git tag -a v2.1.2 -m "PaperReader v2.1.2"
git push origin v2.1.2
```

**push tag 是正式 Release 的触发动作。**

GitHub Actions 随后会自动构建并发布 Release。

---

# 13. 使用 GitHub CLI 查看发布状态

如果安装了 `gh`：

```bash
gh auth status
```

查看 workflow：

```bash
gh run list --workflow release.yml --limit 10
```

查看某次运行：

```bash
gh run view <RUN_ID>
```

实时查看：

```bash
gh run watch <RUN_ID>
```

发布成功后：

```bash
gh release view v2.1.2
```

查看 Release assets：

```bash
gh release view v2.1.2 --json assets
```

---

# 14. 手动发布 Release（仅故障恢复时使用）

正常情况应让 `.github/workflows/release.yml` 自动发布，因为它会保证 Windows/macOS 都经过对应 smoke test。

如果 CI 已经成功构建了全部 artifacts，但最后的 `publish` job 单独失败，可以下载/收集完整 Release 文件后手动执行类似：

```bash
gh release create v2.1.2 \
  release/* \
  --verify-tag \
  --title "PaperReader v2.1.2" \
  --notes-file docs/releases/v2.1.2.md \
  --latest
```

不要在只有 macOS DMG、没有 Windows ZIP 的情况下随意执行这条命令，否则会产生不完整的正式 Release。

---

# 15. Release 失败时如何处理

## 测试或打包 job 失败

优先修复代码/构建问题并重新 push commit。

如果只是偶发 CI 问题，也可以在 GitHub Actions 页面重新运行失败 jobs。

不要反复删除并移动已经公开发布的 tag，除非明确知道这样做对已有用户和下载链接的影响。

## tag 已 push，但 Release 尚未创建

修复 workflow 或代码后，可以重新运行该 tag 对应 workflow run。

## Release 已创建

正常情况下不要复用同一个版本号做不同内容的正式发布。应创建新版本。

如果维护者明确决定修复并重新发布同一个版本，先通过 PR 将修复合并到 `main`，确认 `main` CI 通过，再将现有标签重指向新的合并提交并强制推送该标签。标签 workflow 会重新构建 Windows 与 macOS 包；发布步骤检测到同名 Release 后，会用 `gh release upload --clobber` 替换全部资产，并从 `docs/releases/<tag>.md` 更新 Release 标题和说明。不要手工混用新旧构建资产。

---

# 16. 常见问题

## `npm ci` 失败

如果出现 lock file 与 `package.json` 不一致：

```bash
npm --prefix frontend install
```

确认变更后提交：

```bash
git add frontend/package.json frontend/package-lock.json
```

## py2app 构建失败

先确认：

```bash
python --version
python -c "import platform; print(platform.machine())"
python -c "import py2app; print(py2app.__version__)"
```

应重点确认 Python 3.11 和 arm64。

构建日志位于：

```text
build/macos-py2app.log
```

查看尾部：

```bash
tail -120 build/macos-py2app.log
```

## APP 打开后立刻退出

检查：

```text
~/Library/Application Support/PaperReader/PaperReader-error.log
```

以及端口：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

## Windows 打包版无法启动或窗口空白

确认已安装 Microsoft WebView2 Runtime（大多数 Windows 10/11 已内置）。启动失败详情见：

```text
%LOCALAPPDATA%\PaperReader\PaperReader-error.log
```

以及端口占用（Git Bash）：

```bash
netstat -ano | grep :8000 | grep LISTENING
taskkill //PID <PID> //F
```

## 可以打开 APP，但不能生成译文 PDF

译文由 worker 生成，先确认它的解释器能导入翻译器（`workers` 包本身不导入 `pdf2zh_next`，依赖是惰性加载的）：

```bash
python -c "import pdf2zh_next, babeldoc"
```

把上面的 `python` 换成 `PDFMATHTRANSLATE_PYTHON` 指向的解释器，或 bundle 内的 `worker-runtime/bin/python3`（Windows 为 `worker-runtime\Scripts\python.exe`）。报 `ModuleNotFoundError` 就说明该运行时没装 worker 依赖：按 `desktop/requirements-worker.txt` 重建，并让 `PDFMATHTRANSLATE_PYTHON` 指向它；打包版本再确认 `worker-runtime` 目录在 bundle 内（启动日志会打印 `worker interpreter not found`）。上传时返回 `worker_unavailable` 说明解释器本身就找不到——`require_worker_ready()` 只校验可执行文件，不校验依赖。worker 失败时的 stderr 尾部会写进文档日志，进度面板的失败信息里能看到原文。字体已随应用内置，不需要在宿主机安装中文字体。

macOS 上若导入时报 `scipy.sparse.linalg._propack._spropack` 的 `dlopen` 失败（`zero-fill section type`），是该运行时里的 scipy 太旧：换成 Python 3.11+ 的环境并按 `desktop/requirements-worker.txt` 重装（其中已固定 `scipy>=1.16`）。`bash scripts/start_web.sh` 启动时会用 `PDFMATHTRANSLATE_PYTHON` 指向的解释器试导入 `pdf2zh_next`/`babeldoc`，不通过会直接给出警告。

## 用户下载 DMG 后提示无法验证开发者

当前 macOS Release 是 ad-hoc signed、未 notarize。这是现有发布方式的已知限制。

首次启动通常可以：Finder → Control-click PaperReader → Open。

若未来希望正常双击、减少 Gatekeeper 警告，需要增加：

- Apple Developer ID Application 签名
- Hardened Runtime
- Apple notarization (`notarytool`)
- stapling

这些步骤目前没有集成进仓库 Release workflow。

---

# 17. 清理本地构建产物

如果需要重新做一次干净构建：

```bash
rm -rf build dist frontend/dist
```

`desktop/worker-runtime` 是自备的翻译运行时（体积数百 MB），既不是构建产物也不随 `rm -rf dist` 清掉；需要重装时删掉它再按第 1 节重建。

如果 `release/` 中没有需要保留的本地包，也可以额外删除：

```bash
rm -rf release
```

重新构建：

```bash
./desktop/build_macos.sh
```

Windows Git Bash 中同样使用上述 `rm -rf` 命令清理，然后重新执行：

```bash
powershell -ExecutionPolicy Bypass -File ./desktop/build_portable.ps1
```

---

# 18. 最短命令速查

## 本地直接从源码打开原生 APP（macOS）

```bash
git clone https://github.com/Mars-Dingdang/PaperReader.git
cd PaperReader

conda create -n paperreader-dev python=3.11 -y
conda activate paperreader-dev

python -m pip install -r desktop/requirements-build.txt
npm --prefix frontend ci
npm --prefix frontend run build

python3.13 -m venv worker-runtime
worker-runtime/bin/pip install -r desktop/requirements-worker.txt

python desktop/launcher.py
```

## 构建 macOS APP + DMG

```bash
conda activate paperreader-dev
./desktop/build_macos.sh
open dist/PaperReader.app
```

## 本地直接从源码打开原生 APP（Windows Git Bash）

```bash
git clone https://github.com/Mars-Dingdang/PaperReader.git
cd PaperReader

python -m venv .venv
source .venv/Scripts/activate

python -m pip install -r desktop/requirements-build.txt
npm --prefix frontend ci
npm --prefix frontend run build

python -m venv worker-runtime
worker-runtime/Scripts/pip install -r desktop/requirements-worker.txt

python desktop/launcher.py
```

## 构建 Windows 可移植版 ZIP（Git Bash）

```bash
source .venv/Scripts/activate
python -m pip install -r desktop/requirements-build.txt

python -m venv desktop/worker-runtime
desktop/worker-runtime/Scripts/pip install -r desktop/requirements-worker.txt

powershell -ExecutionPolicy Bypass -File ./desktop/build_portable.ps1
./dist/PaperReader/PaperReader.exe
```

## 发布下一个 SemVer Release（以 v2.1.2 为例）

```bash
cd frontend
npm version 2.1.2 --no-git-tag-version
cd ..

# 编写 docs/releases/v2.1.2.md

python -m pytest backend/tests -q
npm --prefix frontend run build
./desktop/build_macos.sh
python scripts/smoke_release.py --app dist/PaperReader.app

git add frontend/package.json frontend/package-lock.json docs/releases/v2.1.2.md
git commit -m "prepare v2.1.2 release"
git push origin main

git tag -a v2.1.2 -m "PaperReader v2.1.2"
git push origin v2.1.2
```

之后由 GitHub Actions 自动生成 Windows x64 和 macOS arm64 构建并发布 GitHub Release。

---

# 19. 全栈 Web 开发参考

以下内容自 README 移入，面向直接运行后端 + 前端（或 Docker 部署）的开发与联调场景；桌面端构建与发布见上文第 1–18 节。

## 19.1 项目结构

```text
PaperReader/
├── backend/
│   ├── app/
│   │   ├── api/            # routes_*.py：settings / upload / document / annotations / discovery / review / data
│   │   ├── assets/fonts/   # 标注 PDF 使用的内置中文字体
│   │   ├── core/           # config.py、database.py、local_config.py
│   │   ├── models/         # schemas.py、store.py
│   │   ├── services/       # document_pipeline、pdf_translation_worker（worker 进程边界）、document_manifest（manifest → IR）、
│   │   │                   # document_ir、document_structure、alignment_service、annotation_render、cjk_fonts、
│   │   │                   # pdf_ops、pdf_extraction、app_settings、glossary_service、paper_metadata、
│   │   │                   # translation_prompts、stage_tracker
│   │   ├── workers/        # tasks.py（Celery）
│   │   └── main.py
│   └── tests/              # pytest
├── desktop/                # 桌面端启动器、打包脚本与 worker 运行时依赖（见第 1–18 节）
├── frontend/
│   ├── src/
│   │   ├── components/     # ReaderPage 使用的 UI 组件
│   │   ├── lib/            # api.ts、pdfDocumentOptions.ts
│   │   ├── pages/          # ReaderPage.tsx
│   │   ├── App.tsx
│   │   ├── main.tsx
│   │   └── styles.css
│   ├── index.html
│   ├── package.json
│   └── vite.config.ts
├── workers/
│   └── pdfmathtranslate/   # 独立进程：__main__.py、runner、job、events、manifest
├── data/                   # 运行时数据（uploads / outputs / paperreader.db），不要提交
├── docs/                   # 用户说明书、开发者文档、release notes
├── infra/                  # Dockerfile.backend
├── scripts/                # setup_*.sh / .ps1、smoke_release.py、cleanup_legacy_artifacts.py
├── .env.example
├── docker-compose.yml
├── Makefile
└── requirements.txt
```

## 19.2 Python 依赖

来自 `requirements.txt`：

- fastapi==0.115.0
- uvicorn[standard]==0.30.6
- python-multipart==0.0.9
- pydantic==2.9.2
- pydantic-settings==2.5.2
- openai==1.51.2
- requests==2.32.3
- celery==5.4.0
- redis==5.0.8
- httpx==0.27.2
- pytest==8.3.3
- pypdf==4.3.1
- pypdfium2==4.30.0
- Pillow==10.4.0
- reportlab==4.2.5
- matplotlib==3.10.5

## 19.3 环境变量

复制环境文件并填写 OpenAI 兼容端点与密钥：

```bash
cp .env.example .env
```

### 必需变量

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`
- `PDFMATHTRANSLATE_PYTHON` — 运行 worker 的解释器；它自己的环境里必须装有 `desktop/requirements-worker.txt` 的依赖（`pdf2zh-next`、`babeldoc`）。也可以改用 `PDFMATHTRANSLATE_WORKER` 指定独立的 worker 可执行文件，该命令按原样执行，作业文件路径追加在末尾。

### 可选 / 调优变量

- `SQLITE_DB_NAME`（默认 `paperreader.db`）— `DATA_DIR` 下的本地持久化数据库文件。
- `GLOSSARY_REFRESH_INTERVAL_MINUTES`（默认 `30`）— 分领域术语库把候选术语合并进正式术语库的间隔（分钟）。由后台线程执行，设置接口读取术语库时也会补齐一次错过的间隔。翻译领域本身是本地设置（`settings.json` 的 `translation_domain`），不是环境变量。

### PDFMathTranslate-next worker

翻译、页面解析与译文排版都由 `workers/pdfmathtranslate` 这个独立进程完成。它固定使用 PDFMathTranslate-next `2.9.0` 与 BabelDOC `0.6.2`；ONNX、OpenCV、scikit-image、HuggingFace Hub 以及模型与字体缓存只装在 worker 自己的运行时里，不写进 `requirements.txt`，后端也不导入任何翻译器模块。

后端把作业写成 JSON 文件，再以 `PDFMATHTRANSLATE_PYTHON -m workers.pdfmathtranslate <job.json>` 启动，工作目录和 `PYTHONPATH` 都指向仓库（或桌面 bundle）根目录，这样 `workers` 包可直接导入。桌面版由 `desktop/launcher.py` 按 `PDFMATHTRANSLATE_WORKER` → `PDFMATHTRANSLATE_PYTHON` → bundle 内 `worker-runtime` → `python3` 解析解释器。

| 变量 | 含义 |
| --- | --- |
| `PDFMATHTRANSLATE_PYTHON` | 运行 worker 的解释器（默认 `python3`） |
| `PDFMATHTRANSLATE_WORKER` | 独立的 worker 可执行文件；设置后按原样执行，作业路径追加在末尾 |
| `PDFMATHTRANSLATE_VERSION` | 仅作记录：manifest 中登记的 PDFMathTranslate-next 版本（默认 `2.9.0`） |
| `PDFMATHTRANSLATE_TIMEOUT` | 单篇文档的处理预算，默认 `3600` 秒；超时后进程被强杀，文档按 `translate` 阶段失败 |
| `PDFMATHTRANSLATE_WORKING_DIR` | 临时目录根，默认 `<DATA_DIR>/worker`；每篇文档一个子目录，作业结束时清空 |
| `PDFMATHTRANSLATE_DEBUG`（默认 `false`） | 把翻译器自己的布局输出保留到 `outputs/<id>/extraction/debug`，单篇可达数百 MB；manifest 无论开关都会产出 |
| `PDFMATHTRANSLATE_OUTPUT_MODE` | `mono`（译文单语，默认）或 `dual`（双语）；非法值回落 `mono` |
| `PDFMATHTRANSLATE_QPS` | 同时翻译的段落数（默认 `4`，即库的默认值）。一篇论文的耗时几乎全在 `Translate Paragraphs` 阶段，与并发数成反比；提高前先确认供应商的并发/速率限制 |

作业文件由 `app/services/pdf_translation_worker.py` 生成：`job_id`、`input_pdf`、`output_dir`、`work_dir`、`translation`（`api_key` / `base_url` / `model`）、`options`（`output` / `no_watermark` / `debug`）、`qps`，以及可选 `glossary`（领域术语表的 CSV 路径）。作业文件写在临时工作目录中，API key 只出现在这里，不写日志。

事件协议：worker 在 stdout 上逐行写 JSON，每行一个带 `type` 的事件；日志走 stderr，保证 stdout 可解析。

- `stage_summary` — 即将执行的阶段清单（名称、归并后的 PaperReader 阶段、权重）；
- `progress_start` / `progress_update` / `progress_end` — 单阶段进度（`stage`、`group`、`progress`、`overall`、`current`、`total`）；
- `finish` — 成功，携带 `translated_pdf`、`dual_pdf`（dual 模式下的双语 PDF，否则为空）、`manifest_path`、`extraction_dir`、`debug_dir`、`glossary_path`、`mode_label`、`page_count`；
- `error` — 失败，携带 `message` 与 `stage`。

`group` 字段把 BabelDOC 的阶段名归并成 `parse` / `translate` / `render` 三档，`document_pipeline._event_reporter` 据此切换进度面板的当前阶段并写入进度。非零退出、超时、`error` 事件、缺少译文 PDF 或缺少 manifest 都会转成 `WorkerError`，其 `stage` 决定文档记录的失败阶段。上传接口在启动流水线前用 `require_worker_ready()` 校验解释器是否可执行，不通过则返回 409 `worker_unavailable`。

manifest 契约：worker 把 BabelDOC 的调试布局（`paragraph_finder.json`、`add_debug_information.json`、`il_translated.json`）转换成 `extraction/manifest.json`，schema 为 `paperreader-manifest-v1`，并用 `boxes_normalized: false` 声明坐标是 PDF 用户空间的点（原点在页面左下角）。内容包括：每页的 `width` / `height` 与按阅读顺序排列的区块；区块的 `kind`（`title` / `paragraph` / `list` / `formula` / `figure` / `table`）、`bbox`、`layout_label`、`source_text`、`translated_text`、`protected_spans`（URL、引用、行内公式、数字）、`fragment_id` / `logical_id` / `fragments`，图/表的 `captions` 与 `caption_bbox`，表格的 `table_html` 与单元格；顶层的 `figures`、`tables`、`references`、`logical_objects`（逻辑对象 id → 片段 id 列表，按阅读顺序）与 `glossary`（翻译器抽取的术语）。每个区块是一个物理片段，`logical_id` 指向它所属的上游布局区域；BabelDOC 的布局区域编号逐页从 1 开始，因此逻辑对象 id 带页码前缀（如 `p0-l7`），跨页重号不会被合并。`app/services/document_manifest.py` 是唯一读取方，由它生成 IR、页面几何、目录、图表、参考文献与对齐对；下游不读 BabelDOC 的内部 JSON。

产物布局：`outputs/<document_id>/` 下是译文 PDF `<源文件名>_Chinese_ver.pdf`、dual 模式下的 `<源文件名>_双语对照.pdf`、`original.pdf`、`<源文件名>_原文标注.pdf`、`alignment.json`，以及 `extraction/manifest.json`、`extraction/glossary.csv`（术语表非空时才有）和可选的 `extraction/debug/`。

## 19.4 本地运行

Web 开发模式（前端热更新，`make backend` / `make frontend`）已在上文第 6 节说明，此处只补充其余命令。

启动可选的 Celery worker（用于后续异步任务扩展，请在独立终端运行）：

```bash
make worker
# 等价：cd backend && celery -A app.workers.tasks worker -l info
```

前端构建与预览：

```bash
npm --prefix frontend run build      # 产出 frontend/dist
npm --prefix frontend run preview    # 本地预览生产构建
```

常用校验：

```bash
pytest                               # 运行后端测试
python -m compileall backend/app     # 快速语法检查
```

> 上传接口立即返回，流水线在 `BackgroundTasks` 里执行；前端持续轮询 `GET /api/document/{id}` 直至 `status` 变为 `done` 或 `failed`。

## 19.5 本机设置

应用没有账号体系：单个本地操作者直接使用全部功能，没有登录、向导或个人中心。

- 设置（LLM `API Key` / `Base URL` / `Model`、主题、翻译领域、收藏）
  统一存放在 `DATA_DIR/settings.json`，文件权限为 `0600`，写入采用临时文件 + `os.replace` 的原子替换。
- 读取接口只返回 `api_key_configured` 布尔值加上 `base_url` / `model` / `theme` / `show_annotated_pdf` / `translation_domain` / `favorites`，不会回显密钥明文。
- 启动时 `app_settings.purge_removed_keys()` 会从 `settings.json` 中删掉早期版本遗留的解析器配置键（常量 `REMOVED_KEYS`）。
- `translation_domain`（`cs` | `medical` | `general`）决定翻译提示词中的领域参数，并对应一套术语库；写入非法值时回落 `general`。
- 术语库存放在 `DATA_DIR/glossary/<domain>.json`，候选池为同目录的 `<domain>.pending.json`：作业开始时把术语库写成 CSV 交给 worker，翻译器抽取到的术语随 manifest 回来并进入候选池，后台线程按 `GLOSSARY_REFRESH_INTERVAL_MINUTES` 合并进术语库（冲突按票数取多数；术语库按文件大小封顶 50MB，超限时按票数从高到低保留，候选池不设上限），也可在「设置 → 翻译设置」中立即更新。该目录不在 `/data/` 的对外暴露范围内。
- 文档与批注持久化在本地 SQLite：`documents`、`annotations`。没有 `owner_user_id`，也没有用户/会话表。
- 前端首次进入时若未配置 API Key，会在工作台空白页给出「开始前需要配置 AI 服务」的入口，点击打开「设置」弹窗。

## 19.6 Docker 部署

```bash
docker compose up --build
```

服务地址：

- Frontend: `http://localhost:5173`
- Backend: `http://localhost:8000`
- Redis: `localhost:6379`

## 19.7 API 端点

- `GET /health`
- **设置（routes_settings.py）**
  - `GET /api/settings/me`
  - `PUT /api/settings/me`
  - `PUT /api/settings/me/providers`
- **术语库（routes_glossary.py）**
  - `GET /api/glossary/{domain}` — 术语库快照（条数、候选数、文件大小、最近更新时间），读取时会补齐错过的合并间隔
  - `POST /api/glossary/{domain}/refresh` — 立即合并候选池
  - `DELETE /api/glossary/{domain}/terms` — 删除一条术语（请求体 `{"en": "..."}`）
- **上传**
  - `POST /api/upload`（multipart，仅接受 `.pdf`）— 缺少大模型 Key 返回 409 `config_required`，找不到可用的 worker 解释器返回 409 `worker_unavailable`
- **文档**
  - `GET /api/documents` — 列出本机文档摘要
  - `GET /api/document/{document_id}`
  - `PATCH /api/document/{document_id}`
  - `DELETE /api/document/{document_id}` — 软删除一条历史记录
  - `POST /api/document/{document_id}/retry` — 重新排队失败的文档
  - `POST /api/document/{document_id}/reprocess` — 从头重跑一篇已完成或失败的文档
  - `POST /api/document/{document_id}/locate-counterpart` — 双语对应定位，返回 `highlight_text` 用于片段级高亮
  - `GET|POST /api/document/{document_id}/annotations`、`DELETE /api/document/{document_id}/annotations/{id}` — 持久化批注
  - `GET /api/document/{document_id}/notes.md` — 导出双语 Markdown 阅读笔记
  - `PATCH /api/document/{document_id}/progress` — 保存阅读位置（`last_read_page` / `last_read_ratio`）
  - `GET /api/document/{document_id}/structure` — 后端解析的章节目录与图表列表
  - `GET /api/document/{document_id}/bibtex` — 依据 Semantic Scholar 元数据导出 BibTeX
  - `GET /api/search?q=` — 跨文档全文搜索
- **译文 PDF** — 由 worker 产出：解析、翻译与排版在同一个进程里完成，译文排回原稿页面并沿用原稿的图片、公式与表格线条；`translated_pdf` artifact 指向 `outputs/<id>/<源文件名>_Chinese_ver.pdf`
- **产物访问**
  - `GET|HEAD /data/{file_path}` — 产物与上传源文件下载；仅 `uploads/` 与 `outputs/` 下的文件可访问，
    数据库与 `settings.json` 不对外暴露

### `GET /api/document/{document_id}` 响应要点

- `source_filename`
- `updated_at`、`last_opened_at`
- `artifacts`（上传与生成的文件：`source_pdf` / `original_pdf` / `translated_pdf` / `annotated_pdf` / `alignment_index` / `manifest` / `glossary`）
- `references`（提取的参考文献条目，用于预览）
- `progress`、`current_stage`、`current_stage_label`、`eta_seconds`、`stages`（Phase A）
- `failure.stage` — 失败阶段，取值为 `upload` / `parse` / `clean` / `translate` / `render`；重试与重新处理都从 `parse` 重跑（worker 一次处理整篇文档，没有可复用的半成品）
- 以及既有的 `status`、`original_pdf_url`、`translated_pdf_url`、`logs`

## 19.8 平台说明

### macOS (Apple Silicon)

- 翻译与页面解析在本机 worker 进程内完成，不需要 GPU、Torch 或 MPS，也不需要云解析服务。
- PDF 内的 HTTP(S) 链接在系统浏览器打开；PaperReader 窗口保留当前论文与阅读位置。
- WKWebView 阅读器支持 PDF 文本选择/复制、生成的大纲与更快的触控板捏合缩放。
- worker 启动失败（例如 `worker interpreter not found`）时，文档会直接进入 `failed`，不会停在处理中。

### Linux

- worker 只用 CPU；GPU 不是必需条件。
- LLM 翻译仍使用 `.env` 中配置的 OpenAI 兼容端点。

### Windows

- 使用 `scripts/setup_windows.ps1`。
- worker 运行时（`desktop/requirements-worker.txt`）需要单独准备；字体已随应用内置。

## 19.9 当前实现边界

- 上传接口立即返回，流水线在 `BackgroundTasks` 里执行；翻译与排版的并发由 worker 内部管理，后端只转述它的进度事件。
- 文档与批注持久化在 SQLite，设置存放在 `settings.json`；应用面向单个本地操作者，不是加固的互联网级多租户服务。
- 参考文献提取是启发式的（基于章节/行模式），不是完整的引文解析器。
- 前端支持设置弹窗与面板开关。
