# eBay 智能客服 Agent（v0.1 基础框架）

设计与实现说明见 [eBay_24小时智能客服Agent_开发方案_v2.2.md](eBay_24小时智能客服Agent_开发方案_v2.2.md)，其中第 21 章是当前实现。

## 快速启动

1. PyCharm 解释器选项目下 `.venv\Scripts\python.exe`（或任意 Python 3.11+ 虚拟环境）。
2. 右键 `main.py` → Run。首次会生成 `.env`、构建前端，并自动打开浏览器。
3. 在项目根目录的 `.env` 里填写 `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_CHAT_MODEL` / `LLM_API_KEY`，重新运行即可生成 AI 草稿。

所有模型、密钥、连接串只在 `.env` 中配置（模板见 `.env.example`），`.env` 不提交仓库。

## 仓库内容

仓库包含前后端源码、开发方案、测试代码和虚构演示数据。首次克隆后需要安装依赖并填写本地配置。

真实凭证及其备份、运行数据、客户聊天记录、内部知识库文件和生成的发票不上传 GitHub。`知识库/来源/`、`知识库/上传/`、`data/`、`客户测试消息系统/data/` 和 `测试输出/` 由本地独立维护；知识库格式与上传流程见 [知识库格式规范](知识库/知识库格式规范.md)。

## 测试

```bash
.venv\Scripts\python.exe -m pytest tests -q
```
