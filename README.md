# 软考知识记忆

基于 Markdown 笔记的本地知识点复习工具。导入笔记后，系统按章节和二级标题整理成卡片，支持随机抽查、自评、不理解与收藏复习、搜索、卡片编辑和数据库备份。

## 需要安装什么

| 用途 | 软件 | 是否必需 |
| --- | --- | --- |
| 运行本地网页服务 | [Python 3.10 或更新版本](https://www.python.org/downloads/) | 必需 |
| 打开网页 | 常见现代浏览器 | 必需 |
| 辅助切分较长知识点 | [Ollama](https://ollama.com/download) 和本地模型 | 可选 |
| 从手机通过公网访问电脑上的服务 | [Cpolar](https://www.cpolar.com/download) | 可选 |

程序只使用 Python 标准库，网页所需的 JavaScript、样式和字体已放在 `web/vendor/`，无需运行 `pip install` 或 `npm install`。在 Windows 上安装 Python 后，确认终端能运行 `python --version`。Linux 上若要使用“选择文件”弹窗，还需要系统提供 Tkinter。

## 启动

1. 下载仓库源码并解压，或使用 Git 克隆仓库。
2. Windows 上双击 `启动网页.bat`；macOS/Linux 在项目目录运行 `python3 scripts/server.py`。Windows 也可以运行 `python scripts/server.py`。
3. 在浏览器打开 `http://127.0.0.1:8765`。使用期间保持服务运行。

首次启动会自动创建空的 `data/study.sqlite3`。仓库中的 `notes/软考笔记/` 是笔记素材；要生成学习卡片，请按 [使用指南](使用指南.md) 导入 Markdown。学习记录只保存在本机数据库中。

## 可选功能

- **Ollama**：安装并启动 Ollama，再下载 `config/model.json` 中配置的模型，例如 `ollama pull qwen3:8b`。在导入页面勾选“用本地 Ollama 优化切分”并刷新模型列表。没有 Ollama 时取消勾选，仍可使用规则切分。
- **Cpolar**：仅在需要从手机等外部设备访问时安装。将隧道指向本地服务端口 8765。公网地址可能让其他人访问学习数据和管理接口，请自行控制隧道访问权限。Cpolar 本地接口地址、隧道名等默认值在 `config/cpolar.json`。

需要修改本机连接配置时，在 `config/` 新建对应的 `*.local.json`，例如 `config/model.local.json` 或 `config/cpolar.local.json`，只填写要覆盖的字段。账号密码只写在本机的 `config/cpolar.local.json`；这些本机文件已被 Git 忽略。服务地址和端口由 `config/server.json` 管理，修改后重启服务。

安装包请从上面的官方页面选择适合自己系统的版本。若维护者要提供离线安装包，建议放在 GitHub Release，标注软件版本、适用系统和官方下载来源；普通用户不需要下载全部可选软件。

## 数据与测试

- `notes/软考笔记/`：Markdown 原稿及相邻的 `img/` 图片。
- `data/study.sqlite3`：本机生成的卡片、图片与学习记录，不随源码分发。
- `data/import-queue.json`：导入过程产生的临时状态，不随源码分发。
- “记录备份”页面可保存和恢复数据库快照；备份文件保存在 `data/backups/`。
- 开发者可运行 `python -m unittest discover -s tests -p "test_*.py"`；`tests/test_ui.cjs` 需要 Node.js 才能运行，普通用户无需安装 Node.js。

本项目目前未提供开源许可证。公开仓库不等于授予他人复制、修改和再分发代码或笔记的许可；如需开放这些权利，请先确认笔记与图片的来源及授权，再添加合适的许可证。
