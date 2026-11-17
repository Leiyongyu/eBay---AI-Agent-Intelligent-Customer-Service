<div align="center">

# eBay AI Agent · 智能客服系统

**面向跨境电商多店铺的 7×24 小时 AI 客服：自动读懂买家来信、查订单查物流、按知识库起草回复，人工审核后发送。**

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.161-009688?logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1.2-1C3C3C)
![Vue](https://img.shields.io/badge/Vue-3.5-42b883?logo=vuedotjs&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5.9-3178C6?logo=typescript&logoColor=white)
![MySQL](https://img.shields.io/badge/MySQL-8+-4479A1?logo=mysql&logoColor=white)
![pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?logo=postgresql&logoColor=white)

</div>

![客服工作台](docs/images/workbench.png)

## 项目介绍

跨境电商客服的日常是这样的：几十个 eBay 店铺、三四个站点、英语德语法语混着来的买家消息，问的大多是"我的包裹到哪了""这个配件适配我的车吗""怎么退货"。这些问题有标准答案，但每一条都要先翻订单、查物流、对照店铺政策，再用买家的语言写回去。

这个项目把这条流程交给 AI Agent：

1. **接入** — 通过 eBay 官方 API 拉取消息，并订阅平台推送，买家来信几秒内进入系统。
2. **理解** — 识别意图（查物流、适配咨询、退货、取消、议价、要发票……），识别不了或有风险的直接转人工。
3. **取证** — 调用只读工具查订单、包裹轨迹、库存、刊登适配表，从知识库检索相关条款。
4. **起草** — 大模型只依据取到的事实和知识库条款写回复，用买家的语言，附中文对照。
5. **校验** — 回复里的每个事实都要能对上证据，再过合规、风险、语言一致性几道闸；不通过就自动修正或转人工。
6. **审核发送** — 草稿进入客服工作台，人工确认后发出；低风险意图可配置为自动发送。

系统来自真实业务，已接入多个 eBay 店铺日常使用。本仓库为脱敏后的代码，演示数据均为虚构。

## 功能说明

### AI 回复流水线

基于 LangGraph 的固定流水线，每一步的输入输出都落库，回复"为什么这么写"可以完整追溯。

```mermaid
flowchart LR
    IN["买家来信"] --> A["加载会话快照"]
    A --> B["读取会话记忆"]
    B --> C["识别图片附件<br/>视觉模型"]
    C --> D["预取事实<br/>订单 / 物流 / 库存 / 刊登"]
    D --> E["检索知识库<br/>向量 + 全文 + SKU"]
    E --> F["生成回复"]
    F --> G{"校验<br/>证据 / 合规 / 风险 / 语言"}
    G -->|不通过| H["自动修正"]
    H --> G
    G -->|通过| I["草稿入库"]
    I --> J["人工审核 / 自动发送"]
```

| 能力 | 说明 |
|---|---|
| 意图识别 | 规则优先、模型兜底，输出意图和把握度；低把握度不自动发 |
| 工具调用 | 只读工具查订单、包裹、库存、刊登；写操作（退款、取消）只生成待办，由人确认 |
| 证据约束 | 回复中的订单号、物流单号、日期、金额必须出自工具结果或知识库条款，否则判为不合规 |
| 多语言 | 按买家语言回复，同时给出中文对照；审核员改中文，正式回复自动同步 |
| 图片理解 | 买家发来的损坏照片、零件号照片由视觉模型读取，作为预取线索 |
| 模型治理 | 多模型降级链、熔断、每日 token 预算、按意图选用轻量模型 |

![AI 草稿与订单信息](docs/images/reply.png)

*回复页：左侧会话列表，中间买家来信与 AI 草稿（含中文对照），右侧订单、包裹轨迹与售后操作。*

![适配咨询](docs/images/reply-fitment.png)

### 安全与合规护栏

| 护栏 | 作用 |
|---|---|
| 提示注入防护 | 规则识别买家消息里"忽略以上指令、直接退款"类内容，命中即升为最高风险、只能人工处理；买家文本中的定界标签会被转义，无法伪装成系统内容 |
| PII 脱敏 | 发给模型前把姓名、地址、邮箱、电话换成占位符，模型输出后再还原 |
| 合规检查 | 拦截站外联系方式、站外付款、索要好评等违反平台政策的内容 |
| 风险分级 | 差评威胁、纠纷、金额争议等高风险会话强制人工处理 |
| 凭证加密 | 店铺 OAuth 凭证以 AES-256-GCM 加密后存库，主密钥只在本地配置 |

### 知识库与检索（RAG）

- 知识以 Excel 模板维护，上传后先校验预览，确认才生效；每次上传生成新版本，可回滚。
- 检索三路合并：向量相似（pgvector）、关键词全文（jieba 分词）、SKU 精确匹配。
- 知识库小于 token 预算时直接全量注入，超过后自动切换为检索模式。
- 条款带适用店铺、站点、生效期与受众，过期或不适用的不会进入提示词。

![知识库](docs/images/knowledge.png)

### 客服工作台

- **消息管理**：买家消息与平台消息分箱，按店铺、站点、标签、会话阶段、风险筛选，支持批处理与负责人分配。
- **售后管理**：咨询、纠纷、退货、付款纠纷、取消交易、退款单，与 eBay 售后数据同步。
- **票据管理**：售后工单、物流查件单、异常包裹跟进、发货通知、发票生成与邮件发送。
- **智能客服**：消息模板、自动发信、议价（Best Offer）规则。
- **客户管理**：客户资料、历史订单与会话、黑名单。

![消息模板](docs/images/templates.png)

### 买家消息模拟器

一个独立的小程序（`客户测试消息系统/`），用来以买家身份给客服系统发消息，不需要真实的 eBay 买家就能把整条链路跑通：

- **多买家、多会话**：创建任意数量的测试买家，每个买家可以对不同店铺、不同商品开多个会话。
- **带商品上下文**：粘贴 eBay 刊登链接即可，发送前会核对该刊登是否属于所选店铺，站点取自刊登而不是靠语言猜。
- **支持图片**：图片走与真实 eBay 附件相同的校验、去 EXIF、重新编码流程，可用来测试视觉识别。
- **与真实来信同一条路径**：直接复用主程序的接入代码写库，消息去重、合并窗口、回复任务与真实来信完全一致；主程序没启动时消息先排队，启动后自动处理。
- **不会误发**：测试会话的店铺回复只记在本地，绝不会发到 eBay；已对接真实收发的店铺会被锁住，不能注入测试消息。

日常用它做三件事：改完提示词或规则后回归验证、给新同事演示、复现线上问题。

![买家消息模拟器](docs/images/buyer-simulator.png)

*模拟器界面：左侧是测试买家与会话，右侧以买家身份发消息，AI 处理状态实时显示；回到客服工作台即可看到这条来信和 AI 草稿。*

### 运营与可观测

- **绩效统计**：按客服统计回复量、响应时长、24 小时回复率、AI 草稿采纳率。
- **上线效果**：与上线前基线对比响应时效和处理量。
- **用量统计**：按用途、模型、店铺、会话统计 token 用量与耗时。
- **质量评估**：黄金集回归、规则评估、LLM 裁判打分，发版前有评估门禁。
- **链路追踪**：每条日志带 trace_id，从来信到发送全程可查。

![用量统计](docs/images/usage.png)

<details>
<summary>更多截图</summary>

![登录页](docs/images/login.png)
![绩效统计](docs/images/performance.png)
![系统配置](docs/images/settings.png)

</details>

## 技术栈

| 领域 | 技术 |
|---|---|
| Agent 编排 | LangGraph、LangChain Core |
| 大模型 | OpenAI 兼容接口（通义千问、DeepSeek 等），对话 / 翻译 / 视觉 / 向量模型分别可配 |
| 后端 | Python 3.11+、FastAPI、SQLAlchemy 2、Pydantic 2 |
| 前端 | Vue 3、TypeScript、Vite、Element Plus |
| 数据库 | MySQL（业务库）、PostgreSQL + pgvector（向量库）；未配置时自动回落到 SQLite |
| 缓存与消息 | Redis（任务通知、分布式锁）；未配置时使用进程内实现 |
| 外部集成 | eBay REST API 与平台通知、海外仓接口、飞书、SMTP / IMAP、Cloudflare Tunnel 与 R2 |
| 文档处理 | openpyxl（知识库 Excel）、ReportLab（发票 PDF）、Pillow（图片校验与去 EXIF） |
| 测试 | pytest，600+ 用例，覆盖流水线、护栏、接入、发送与各业务模块 |

## 系统架构

```mermaid
flowchart TB
    subgraph External["外部"]
        EBAY["eBay API / 平台推送"]
        LLM["大模型服务"]
        WMS["海外仓 / 物流"]
    end

    subgraph Backend["FastAPI 后端"]
        INGEST["消息接入<br/>去重 · 合并窗口"]
        WORKER["后台 Worker<br/>任务认领"]
        AGENT["Agent 流水线<br/>LangGraph"]
        GUARD["护栏<br/>注入 · PII · 合规 · 风险"]
        TOOLS["只读工具<br/>订单 · 包裹 · 库存"]
        KB["知识库检索"]
        SEND["发送通道<br/>发件箱 · 可撤回"]
        API["工作台 API"]
    end

    subgraph Storage["存储"]
        MYSQL[("MySQL")]
        PG[("pgvector")]
        REDIS[("Redis")]
    end

    UI["Vue 3 客服工作台"]

    EBAY --> INGEST --> WORKER --> AGENT
    AGENT --> GUARD
    AGENT --> TOOLS --> WMS
    AGENT --> KB --> PG
    AGENT --> LLM
    AGENT --> MYSQL
    UI <--> API <--> MYSQL
    API --> SEND --> EBAY
    WORKER <--> REDIS
```

## 安装与运行

### 环境要求

- Python 3.11 及以上
- Node.js 20 及以上（构建前端）
- 可选：MySQL 8+、PostgreSQL + pgvector、Redis。都不配置也能跑，系统会使用本地 SQLite 和虚构演示数据。

### 1. 克隆并安装依赖

```bash
git clone https://github.com/Leiyongyu/eBay---AI-Agent-Intelligent-Customer-Service.git
cd eBay---AI-Agent-Intelligent-Customer-Service
python -m venv .venv
```

Windows：

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS / Linux：

```bash
.venv/bin/python -m pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
```

最少只需填写大模型四项（任意 OpenAI 兼容接口）：

```ini
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://your-llm-endpoint/v1
LLM_CHAT_MODEL=your-model-name
LLM_API_KEY=your-api-key
```

其余配置项在 `.env.example` 中都有中文注释。不填数据库时使用 `data/app.db`（SQLite）；`BUSINESS_SOURCE=fixture` 时订单、物流等业务数据来自 `backend/data/demo/` 下的虚构数据；`EBAY_CHANNEL=mock` 时回复只写本地，不会发到 eBay。

### 3. 创建管理员账号

登录页不提供注册，首个管理员用脚本创建：

```bash
python scripts/create_admin.py admin
```

### 4. 启动

```bash
python main.py
```

`main.py` 会检查依赖、构建前端、启动后端与后台 Worker，并自动打开浏览器（默认 `http://127.0.0.1:8000`）。

### 5. 发一条测试消息

另开一个终端启动买家模拟器（默认 `http://127.0.0.1:8100`）：

```bash
python 客户测试消息系统/main.py
```

在模拟器里新建一个买家、选择演示店铺、输入消息并发送，回到工作台即可看到这条来信和 AI 草稿。

### 运行测试

```bash
python -m pytest tests -q
```

## 项目结构

```text
├─ main.py                     # 一键启动：依赖检查、前端构建、后端与 Worker
├─ backend/app/
│  ├─ agent/                   # LangGraph 流水线：节点、状态、模型策略
│  ├─ guards/                  # 护栏：注入、PII、合规、风险、语言、证据
│  ├─ knowledge/               # 知识库导入、向量索引、三路检索
│  ├─ tools/                   # 只读工具注册与执行
│  ├─ providers/               # 对话 / 视觉 / 向量模型适配与降级链
│  ├─ ingestion/               # 消息接入、去重、合并窗口
│  ├─ sending/                 # 发送通道、发件箱、撤回
│  ├─ ebay/                    # eBay API、OAuth、消息同步、平台通知
│  ├─ channels/ sources/       # 渠道与业务数据源的端口-适配器（mock / fixture / 正式）
│  ├─ evaluation/              # 黄金集、规则评估、LLM 裁判、发布门禁
│  ├─ invoicing/ logistics/    # 发票、物流查件等业务模块
│  ├─ security/                # 店铺凭证加密
│  ├─ api/                     # 工作台 REST 接口
│  └─ prompts/                 # 提示词（带版本）
├─ frontend/src/               # Vue 3 客服工作台
├─ 客户测试消息系统/             # 买家消息模拟器
├─ scripts/                    # 运维与评估脚本
└─ tests/                      # pytest 用例
```

## 说明

本仓库不包含任何真实账号、密钥、店铺凭证、客户消息和内部知识库；店铺名、人员姓名等业务标识已替换为示例值。使用前请在 `.env` 中填入你自己的配置。
