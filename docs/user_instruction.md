# PaperReader 使用说明

## 安装

### 直接使用软件
PaperReader 支持 Windows 和 MacOS 平台，可以直接下载安装包，见相应的 Release 页面（请下载最新版本的安装包）。安装包包含了主程序依赖，无需额外安装 Python 或 Node.js；翻译另需一个 PDFMathTranslate-next 运行时，见「配置 AI 服务」一节。

需要注意的是，**Windows 版本的安装包如果直接解压后 PaperReader.exe 打开失败**，请重新解压前右键 .zip 文件、选择“属性”、**点击“解除锁定”**，然后再解压。**请在原始目录下打开 PaperReader.exe**，因为其运行依赖同一目录下的 _internal 文件夹。

### 从源代码构建
要从源代码构建 PaperReader，需要先克隆仓库，然后按照 docs/DEVELOPMENT.md 中的说明进行构建。

首先：安装必要的环境依赖
```bash
pip install -r desktop/requirements-build.txt
```

翻译所需的 worker 运行时单独安装（见「配置 AI 服务」一节）：
```bash
python3 -m venv worker-runtime
worker-runtime/bin/pip install -r desktop/requirements-worker.txt
```

对于 Windows 用户，使用脚本
```bash
powershell -ExecutionPolicy Bypass -File ./desktop/build_portable.ps1
./dist/PaperReader/PaperReader.exe
```

对于 MacOS (Mac Silicon Only) 用户，使用脚本
```bash
chmod +x desktop/build_macos.sh
./desktop/build_macos.sh
open ./dist/PaperReader.app
```

## 使用说明
### 配置 AI 服务
PaperReader 没有账号，也不需要注册或登录：打开即可使用，配置一次后长期生效。

点击左侧栏工具栏最右侧的齿轮按钮打开「设置」，在「AI 服务」页填入 API Key 和 Base URL、模型名称，即可用自己的大模型密钥进行翻译。推荐使用价格相对较低的模型，如 DeepSeek-V4-Flash-0731。

PDF 的解析、翻译与译文排版全部由本机的 **PDFMathTranslate-next worker** 完成，不需要再配置任何解析服务。worker 是一个独立进程，用单独的 Python 运行时执行；该运行时不在主程序依赖中，需要按 `desktop/requirements-worker.txt` 单独安装，或由打包版本随包提供。找不到可用运行时时，上传会直接提示 worker 不可用，不会开始处理。

只需要填写大模型密钥，留空即保持原值，勾选删除才会清除。缺 Key 时上传会提示先完成配置。

配置保存在本机数据目录的 `settings.json`（仅当前用户可读），密钥不会回传给前端界面。

### 设置翻译领域与术语库
「设置 → 翻译设置」页可选择**计算机科学**、**医学**或**通用学术**。领域决定翻译提示词里的术语规范（例如医学领域要求疾病、药物与检验指标使用规范译名，基因与量表缩写保留英文），保存后对新的翻译任务生效；切换领域后重新翻译同一篇文档时会重新翻译，不会沿用旧译文。

每个领域有独立术语库：术语库中的条目会作为**强制术语表**随作业交给 worker，同一个英文词在全文按同一译法输出；翻译完成后会在后台从本篇论文中抽取新术语，回到该领域的待合并队列，之后按设定间隔（默认 30 分钟）自动合并进术语库，也可以点「立即更新」手动合并。列表里每条术语右侧的删除按钮可以移除译错的条目；术语库文件保存在本机数据目录的 `glossary/` 下。

### 提交论文
![](../images/0910-1.png)
PaperReader 是一款保留公式、图片等结构的论文翻译器，目前仅支持上传 **PDF 文件**：点击“新解析”直接上传即可。

PaperReader 的工作流为：PDF 文件 → PDFMathTranslate-next worker（页面解析 → 翻译 → 版式合成）→ 译文 PDF 与结构化清单。译文排回原稿页面：页幅与页数不变，图片、块级公式与表格线条直接沿用原稿，表格只替换已翻译的单元格文字。译文 PDF 自带字体，电脑上不需要安装中文字体或 TeX。

### 排版与进度

版式由 worker 负责：它先解析页面结构（分栏、段落、图、表、公式），逐段翻译，再把译文排回原稿页面对应的位置，图片、公式与表格线条保持原位。译文 PDF 默认是单语版本，可在配置文件（源码运行的 `.env`、桌面版的 `.config.env`）中设置 `PDFMATHTRANSLATE_OUTPUT_MODE=dual` 改为双语输出；两种输出都不带水印。

worker 通过标准输出逐行上报阶段事件，PaperReader 把它们映射为进度面板上的「解析 / 翻译 / 版式合成」三个阶段，并按阶段显示进度与预计耗时。

进度面板会显示失败阶段与原因，可在该阶段点击“从此处重试”。worker 一次处理整篇论文，重试与「重新处理」都会从头重跑，不会复用半成品。

### 历史记录
左侧栏的“历史记录”中会显示用户提交的论文列表，点击即可查看解析结果和翻译结果。

列表顶部有“全文搜索文献库”搜索框：输入关键词会在所有已解析论文的原文与译文中查找，点击命中结果会直接打开对应论文并高亮所在位置。

可以点击☆收藏论文，收藏的论文会在“我的收藏”中显示。右键论文可“更改文档名”、“导出 BibTeX”（依据 Semantic Scholar 返回的论文元数据自动生成）或删除；历史记录里的「重新处理」按钮会对该论文重新跑一遍 worker。

### 产物文件
每次解析会在输出目录中生成：上传的源 PDF、`original.pdf`（原始文件）、译文 PDF、原文标注 PDF（在原页上框出每个解析区域，包括图注与表注）、`extraction/manifest.json`（解析清单：页面几何、区块原文与译文、图表与参考文献）以及 `alignment.json`（双语对齐索引）。

文件储存的位置为：
- Windows：
```text
 C:\Users\<name>\AppData\Local\PaperReader\data\outputs\<c312aa80-ff07-4122-ab89-17fbc22c0e5c>
```
- MacOS:
```text
/Users/<name>/Library/Application Support/PaperReader/data/outputs/<c312aa80-ff07-4122-ab89-17fbc22c0e5c>
```

### 阅读器功能
- **文档内搜索**：在 PDF 区域按 Ctrl（macOS 为 Cmd）+F 打开搜索栏，Enter / Shift+Enter 在命中之间跳转，Esc 关闭。
- **批注与笔记**：选中一段文字后右键（macOS 也可按住 Control 点击），打开菜单后文字保持选中，可选择四种颜色添加高亮，并可在输入框中附一句备注（按 Enter 保存）。批注保存在本机，重新打开论文自动恢复；点击工具栏的“批注笔记”按钮可直接查看引用文字和备注，点击已有高亮或右键“查看批注”也可打开对应笔记。面板提供“定位原句”“删除批注”和“导出阅读笔记”，导出的 Markdown 包含原文、译文与备注。
- **阅读位置记忆**：关闭论文后重新打开，会自动回到上次读到的位置。
- **双栏联动滚动**：工具栏的“链接”图标默认开启，滚动一侧时另一侧同步滚动；再次点击可关闭。
- **图表导航**：工具栏的“图表”按钮打开当前 PDF 左侧的竖向半透明缩略图浮层，列表可独立滚动，点击卡片定位图注或表注。预览直接从原文与译文 PDF 中裁切，原文、译文分别定位各自的页码。
- **对照高亮**：选中一段文字右键“跳转到对应内容并高亮”，对面译文（或原文）中与所选语句对应的部分会在文字层加载后高亮并居中显示。跨页段落优先匹配所选片段；没有可匹配文字时显示提示。
