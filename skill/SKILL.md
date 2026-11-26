---
name: soft-exam-cloze
description: 从 Typora Markdown 笔记抽取原文重点并生成多角度挖空四选一卡片，存入本地 SQLite。
---

# 软考 Markdown 知识点记忆 Skill

当用户说“更新软考题库”“用笔记生成挖空卡片”时：
1. 读取 `notes/` 目录 Markdown 文件，保留原始资料，不覆盖源文件。
2. 根据标题、粗体、高亮、代码、普通正文中的关键概念识别知识点。一个重要知识点可以生成 2~3 种不同挖空方式。不要额外编写没有原文依据的知识点。
3. 每张卡片输出：`section`、`original`（原文纯文本）、`answer`（原文连续子串且仅出现一次）、`options`（含正确答案共四个不同字符串）、`line`。
4. 需满足 `original.count(answer) == 1`，干扰项不能与正确答案相同，不可以生成多个正确选项。不要隐藏无意义词语。
5. 将 JSON 数组通过项目 `/api/import` 导入，或建议在“题库管理”直接使用 Ollama。导入前进行人工核对，确认题目没有知识性错误。
6. 保留不变知识点对应的稳定 ID 和历史记录，不能清空 `data/study.sqlite3`。
7. 复习和错题保存在 SQLite。不得宣称本项目已经实现 FSRS（现为简化间隔复习）。

建议本地运行 `python scripts/server.py` 后用浏览器导入 Markdown。Ollama 地址 `http://127.0.0.1:11434`。不允许未经用户同意将笔记上传到云端。
