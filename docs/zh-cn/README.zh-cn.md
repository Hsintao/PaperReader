# PaperReader v2.2.0

[English](../../README.md) | 简体中文

[下载 Windows / macOS v2.2.0](https://github.com/Mars-Dingdang/PaperReader/releases/tag/v2.2.0) · [升级指南](../UPGRADING.md) · [发布说明](releases/v2.2.0.md)

> 📖 **下载前请先阅读[用户说明书](../user_instruction.md)**，其中包含安装（含 Windows 安装包"解除锁定"步骤，可避免绝大多数启动失败）、首次运行的服务商配置、论文提交、历史记录与产物文件等说明。

v2.2.0 用 PDFMathTranslate-next 完成解析、翻译与译文 PDF 生成：一次 worker 处理保留原稿页幅与页数，图片、公式与表格线条沿用原稿。详见[发布说明](releases/v2.2.0.md)。前端/API 版本号：`2.2.0`。

![](../../images/demo1.png)

PaperReader 是一款全栈双语论文阅读应用。上传 PDF 后，PaperReader 把它交给本机的 PDFMathTranslate-next worker：worker 解析页面、通过你配置的大模型端点翻译，并输出译文 PDF 与一份结构化清单（manifest），记录每页的区块、图、表、图注与参考文献。PaperReader 依据这份 manifest 生成目录、参考文献列表、双语对齐与原文标注 PDF，阅读器提供原文/译文对照。

## 桌面版快速开始

- **Windows**：从 Release 页下载 ZIP，完整解压后运行 `PaperReader.exe`。若无法打开，请先对 ZIP "解除锁定"，见[用户说明书](../user_instruction.md)。
- **macOS（Apple Silicon）**：打开 DMG，将 PaperReader 拷贝到"应用程序"。
- 两个平台的安装包都在应用内的「设置」中填写你自己的大模型 API Key。翻译本身由一个独立的 PDFMathTranslate-next 运行时执行，该运行时需单独安装，见 `desktop/requirements-worker.txt`。
- 译文 PDF 自带中文字体，原文标注 PDF 使用应用内置字体绘制，宿主无需安装中文字体或 TeX。
- 平台说明：[Windows](../../desktop/README_zh.md) · [macOS](../../desktop/README_macos_zh.md)

## 功能特性

- 无账号、无登录：单个本地操作者直接使用全部功能，大模型、主题与阅读偏好保存在本机 `0600` 权限的配置文件中；密钥不回传前端
- 本地 SQLite 持久化历史记录；重启后可重新打开已处理的文件
- 仅支持上传 `.pdf`，文件交给 PDFMathTranslate-next worker，由它一次完成页面解析、翻译与译文 PDF 生成
- worker 是独立进程，使用单独安装的运行时（`desktop/requirements-worker.txt`）：后端不导入翻译器，每次任务由一个 JSON 作业文件描述
- 翻译领域设置（计算机科学 / 医学 / 通用学术）决定翻译提示词与术语规范；每个领域维护独立术语库，自动积累翻译器抽取的术语并按固定间隔合并，作业时作为术语表传给 worker
- 译文沿用原稿版式：译文排回原稿页面，图片、块级公式与表格线条沿用原稿，页幅与页数不变
- worker 输出结构化清单（manifest，`paperreader-manifest-v1`）：页面几何、带类型的区块（标题/段落/列表/公式/图/表）及其原文与译文、保护片段（URL、引用、数字、行内公式）、图表图注与单元格、参考文献，以及逻辑对象到片段的映射
- 目录、图表墙、参考文献列表、双语对齐索引与原文标注 PDF 全部由该 manifest 生成；标注 PDF 框出每个解析区域，包括每个图注与表注
- 原文/译文 PDF 对照阅读：书签或后端解析的章节大纲、文本选择复制、触控板缩放、按需页面渲染，以及带阶段分解、预计耗时与失败诊断的进度条
- Ctrl/Cmd+F 文档内全文搜索，支持逐个命中跳转
- 持久化彩色批注与备注，重新打开自动恢复，可导出双语 Markdown 阅读笔记
- 阅读位置记忆：重开文档回到上次读到的位置
- 可选的双栏联动滚动（基于双语对齐索引）；对照高亮落在所选语句对应的片段，而不是总在段首
- 图表墙：解析出的全部图表以缩略图条展示，点击跳转到所在页
- 侧栏跨文档全文搜索，点击命中直达文档对应位置
- 基于 Semantic Scholar 的论文元数据（标题/作者/年份/期刊）与一键 BibTeX 导出
- 产物面板：参考文献预览与拖入 PDF 窗格
- 明/暗主题本地持久化
- 原生桌面应用（Windows 使用 WebView2，macOS 使用 WKWebView）

## 文档

| 文档 | 内容 |
| --- | --- |
| [用户说明书](../user_instruction.md) | 安装、配置与使用，下载前必读 |
| [开发者文档](../DEVELOPMENT.md) | 源码构建、打包与发布流程、项目结构、环境变量、API 参考 |
| [升级指南](../UPGRADING.md) | 版本间数据迁移 |
| [发布说明](releases/)（[English](../releases/)） | 各版本变更 |
