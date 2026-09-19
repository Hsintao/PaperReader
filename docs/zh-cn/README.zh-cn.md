# PaperReader v2.1.12

[English](../../README.md) | 简体中文

[下载 Windows / macOS v2.1.12](https://github.com/Mars-Dingdang/PaperReader/releases/tag/v2.1.12) · [升级指南](../UPGRADING.md) · [发布说明](releases/v2.1.12.md)

> 📖 **下载前请先阅读[用户说明书](../user_instruction.md)**，其中包含安装（含 Windows 安装包"解除锁定"步骤，可避免绝大多数启动失败）、首次运行的服务商配置、论文提交、历史记录与产物文件等说明。

v2.1.12 将 LaTeX 图表缩略图裁剪为图表本身，不再显示整个页面，原文与译文 PDF 均独立生效。标题定位优先识别真正的标题行，而不是正文中的引用。详见[发布说明](releases/v2.1.12.md)。前端/API 版本号：`2.1.12`。

![](../../images/demo1.png)

PaperReader 是一款全栈双语论文阅读应用。上传 PDF 后，PaperReader 通过 MinerU 云端 API 解析，使用大模型翻译并保留公式、图片与表格结构，重新编译生成译文 PDF，并支持原文/译文对照阅读。

## 桌面版快速开始

- **Windows**：从 Release 页下载 ZIP，完整解压后运行 `PaperReader.exe`。若无法打开，请先对 ZIP "解除锁定"，见[用户说明书](../user_instruction.md)。
- **macOS（Apple Silicon）**：打开 DMG，将 PaperReader 拷贝到"应用程序"。
- 两个平台的安装包都在应用内的「设置」中填写你自己的大模型与 [MinerU](https://mineru.net/apiManage/docs) 凭据，其余依赖已全部内置。
- 生成译文 PDF 需要宿主机额外安装 [TeX Live](https://www.tug.org/texlive/) 与 `latexmk`。
- 平台说明：[Windows](../../desktop/README_zh.md) · [macOS](../../desktop/README_macos_zh.md)

## 功能特性

- 无账号、无登录：单个本地操作者直接使用全部功能，LLM / MinerU / 解析器 / 视觉模型设置保存在本机 `0600` 权限的配置文件中；密钥不回传前端
- 本地 SQLite 持久化历史记录；重启后可重新打开已处理的文件
- 仅支持上传 `.pdf`，解析走 MinerU 云端 API 或内置的本地文本层提取，无需本地 OCR 或 GPU
- LLM 并发翻译，带逐 chunk 校验检查点与自动重试；失败文档从最近检查点续跑，无需从头再来
- LaTeX 自动恢复：散文本清洗 → strict/降级两级编译 → 最多五轮编译反馈驱动的模型修复，支持扩大上下文、修复导言区与宏包、自动备份 → 浏览器内手动 TeX 编辑器
- 可选的视觉模型逐页对抗校验（自动/手动复核，默认关闭）
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
