# 译文排版字体资产

译文 PDF 使用应用内置、随 PDF 嵌入的固定简体中文字体，所有平台使用同一套字形与度量。
字体文件为 ReportLab 4.2.5 可直接加载的静态 TrueType（`glyf`）字体，不含可变字体轴。

| 文件 | 家族 | 字重 | 用途 |
|---|---|---|---|
| `NotoSerifSC-Regular.ttf` | Noto Serif SC | Regular 400 | 宋体正文、标题、图表注、表格单元格、脚注 |
| `NotoSerifSC-Bold.ttf` | Noto Serif SC | Bold 700 | 宋体强调 |
| `NotoSansSC-Medium.ttf` | Noto Sans SC | Medium 500 | 随应用内置的黑体（无衬线）字族 |
| `NotoSansSC-Bold.ttf` | Noto Sans SC | Bold 700 | 随应用内置的黑体粗体 |

## 上游与许可

- 上游项目：Noto CJK（Google / Adobe 合作项目）
- 上游仓库：https://github.com/notofonts/noto-cjk
- 下载来源：Google Fonts CSS API（`fonts.googleapis.com/css2?family=Noto+Serif+SC` 与
  `family=Noto+Sans+SC`）返回的静态 TrueType 地址
- 字体版本：Noto Serif SC v35、Noto Sans SC v40
- 许可：SIL Open Font License 1.1，完整文本见同目录 `OFL.txt`

字体在 SIL Open Font License 1.1 下可自由再分发，包括随应用一起分发。
