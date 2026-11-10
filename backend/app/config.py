"""服务端配置（开发方案第 15.2 节）。

所有模型、密钥、连接串都从项目根目录的 .env 读取，代码里不内置任何模型默认值。
空字符串视为未配置（env_ignore_empty），这样 .env.example 里留空的字段不会触发类型校验错误。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"

# 回复模型只走 OpenAI 兼容的 API Key 接口（百炼千问）。原 ChatGPT 账号（Codex 命令行）通道已于 2026-10-30 移除；
# 标签保留，只为用量统计里显示历史记录
SUPPORTED_LLM_PROVIDERS = {"openai_compatible"}
LLM_PROVIDER_LABELS = {"openai_compatible": "API Key 接口（百炼千问）", "codex_cli": "ChatGPT 账号（Codex，已停用）"}
SUPPORTED_AGENT_MODES = {"fixed_pipeline"}
SUPPORTED_KNOWLEDGE_MODES = {"full_context", "retrieval"}
STRUCTURED_OUTPUT_METHODS = {"prompt_json", "json_mode", "json_schema", "function_calling"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        # 本项目约定“配置全在 .env”：.env 里填了值就以它为准；系统环境变量只补 .env 留空的项。
        # （本机 Windows 用户环境变量里有别的项目留下的 EBAY_CLIENT_ID 等，不能让它们悄悄覆盖 .env）
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    # LLM
    llm_enabled: bool = True  # 大模型总开关：false 时回复草稿、翻译、识图、向量检索全部停用（数据同步、物流、查件单照常）
    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_chat_model: str | None = None
    llm_api_key: str | None = None
    llm_context_window: int | None = None
    llm_max_output_tokens: int | None = None
    llm_temperature: float | None = None
    llm_timeout_seconds: int = 60
    llm_structured_output: str = "prompt_json"
    # 翻译（买家来信译中文、草稿回译、改写译成买家语言）单独用的模型，同一个服务商和秘钥；留空 = 用 LLM_CHAT_MODEL
    llm_translate_model: str | None = None
    llm_translate_thinking: bool = False  # 翻译要不要开思考模式（开了每句都多出思考 token，翻译一般不需要）
    llm_extra_body: str | None = None  # JSON，原样透传给服务商（如百炼的 enable_thinking）

    # Embedding / Vision（当前未使用，仅做就绪检查）
    embedding_provider: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    embedding_dimension: int | None = None
    embedding_distance: str = "cosine"
    embedding_normalize: bool = False
    vision_provider: str | None = None
    vision_base_url: str | None = None
    vision_model: str | None = None
    vision_api_key: str | None = None
    vision_enabled: bool = False  # 是否用视觉模型读图（按图片计费，默认关闭；关闭时附件只给审核员看）
    vision_timeout_seconds: int = 60
    vision_max_images_per_job: int = 3  # 每个回复任务最多识别几张图

    # 附件（第 20 章）
    media_dir: str = "data/media"  # 相对项目根目录
    media_max_bytes: int = 10_000_000
    media_max_per_message: int = 5
    media_max_dimension: int = 2048  # 保存副本的最长边，超出等比缩小（也减少识图 token）
    media_allowed_hosts: str = "ebayimg.com,ebay.com,ebaystatic.com"  # 只从这些域名（含子域名）下载
    media_retention_days: int = 90  # 与聊天记录保留期一致；消息被清理时附件文件一并删除

    # 聊天记录保留期（按消息在 eBay 的发送时间；店铺可在 stores.retention_days 单独设置）
    data_retention_days: int = 90  # 每个会话的消息保留 3 个月（按消息时间）；0 = 不清理
    baseline_retention_days: int = 90  # 上线前基线消息（baseline_messages，含买家正文）保留天数，按消息时间；0 = 不清理
    retention_cleanup_time: str = "03:30"  # 每天几点清理（本机时间 HH:MM）；错过时下次启动补跑
    retention_batch_size: int = 500  # 每批删除的消息数，每批单独提交
    retention_dry_run: bool = True  # 试运行：只统计会删多少，不真删
    audit_retention_days: int = 180  # 审计日志（不含聊天正文）保留天数；0 = 不清理

    # 基础设施
    database_url: str | None = None
    database_password: str | None = None  # 单独填写，避免密码里的 @ : / 等字符破坏连接串
    business_mysql_url: str | None = None
    redis_url: str | None = None
    redis_key_prefix: str = "ebaycs:"
    redis_autostart_path: str | None = None  # 本机 redis-server.exe；REDIS_URL 指向本机且连不上时由启动类拉起
    vector_store_backend: str = "pgvector"
    business_source: str = "fixture"
    ebay_channel: str = "mock"  # ebay = 对接 EBAY_LIVE_STORES 里的店铺真实发到 eBay；mock = 只记在本地
    # 对接 eBay 真实收发的店铺（店铺名或 ID，逗号分隔）：只拉取这些店铺的消息、只有这些店铺真实发送；留空 = 拉取全部店铺、都不真实发送
    ebay_live_stores: str = ""
    # 议价：额外允许真实发报价的店铺（店铺名或 ID，逗号分隔）。“对接 eBay 的店铺”本来就能发；列在这里的只开议价，不会因此真实收发消息
    offer_live_stores: str = ""
    orders_sync_days: int = 180  # 客户列表：第一次拉近多少天的订单（之后按修改时间增量）
    best_offer_sync_minutes: int = 15  # Best offer（买家报价，48 小时过期）每隔多少分钟拉一次；0 = 暂停
    ebay_cases_sync_minutes: int = 60  # 售后管理（咨询、纠纷、退货、付款纠纷、评价、取消、退款单）每隔多少分钟从 eBay 拉一次；0 = 不自动拉
    send_delay_seconds: int = 300  # 发给买家的消息延迟多少秒发出（期间可撤回）；0 = 立即发
    # 启动时导入的知识 JSONL（可选）。正式知识库通过工作台“知识库”页上传 Excel 维护，一般留空
    knowledge_seed_file: str | None = None
    demo_seed: bool = True
    ebay_verification_token: str | None = None
    # 店铺凭证加密主密钥（Base64 的 32 字节）；首次运行 scripts/import_store_credentials.py 时自动生成。只在 .env，不入库
    credentials_master_key: str | None = None
    store_credentials_file: str | None = None  # 紫鸟店铺全量 Excel 的路径，导入脚本读取
    ebay_runame: str | None = None  # OAuth 授权跳转名（RuName），重新授权店铺换秘钥时用
    ebay_deletion_endpoint: str | None = None
    # eBay 实时推送（Notification API）接收地址：公网 HTTPS，经 Cloudflare 隧道转到本机；挑战校验同样用 EBAY_VERIFICATION_TOKEN
    ebay_notify_endpoint: str | None = None
    ebay_push_enabled: bool = True  # 实时推送开关；false = 收到推送只记录（仍回 204，免得 eBay 停用推送地址），不触发拉取
    auth_session_hours: int = 12  # 登录闲置多少小时后需要重新登录（有访问就顺延）
    ebay_notify_verify_signature: bool = True  # 校验 X-EBAY-SIGNATURE；只有本地调试才关
    # Cloudflare 隧道：两项都填了，main.py 启动时带起 cloudflared 子进程、停止时一起关（令牌只在 .env，经环境变量传给子进程）
    cloudflared_path: str | None = None
    cloudflare_tunnel_token: str | None = None

    # 发票：按订单生成 PDF（每份都留底在 INVOICE_DIR，只有管理员能看），人工审核后通过企业邮箱（SMTP）发给买家
    invoice_dir: str = "data/invoices"  # 相对项目根目录；不随聊天记录保留期清理
    invoice_auto_generate: bool = True  # 买家给了邮箱且能确定是哪笔订单时，自动先生成发票等人工审核
    invoice_pdf_font: str | None = None  # 发票用的 TTF 字体；留空用 Windows 的 Arial，找不到再用内置 Helvetica
    invoice_mail_enabled: bool = False  # false = 只记录“模拟发送”，不真正发邮件；SMTP 配好、测试邮件通过后再改 true
    invoice_smtp_host: str = "smtp.exmail.qq.com"  # 企业微信邮箱（腾讯企业邮）
    invoice_smtp_port: int = 465  # SSL
    invoice_smtp_user: str | None = None  # 发件邮箱地址
    invoice_smtp_password: str | None = None  # 企业邮箱的“客户端专用密码”，只放 .env
    invoice_mail_from_name: str | None = None  # 发件人显示名，如店铺名
    invoice_mail_bcc: str | None = None  # 每封发票邮件密送一份到这个邮箱（可空）
    invoice_auto_send: bool = False  # 普通发票不审核：买家给了邮箱、订单能确定时自动生成并直接发到邮箱；增值税发票一律转人工
    invoice_test_recipient: str | None = None  # 测试期：所有发票邮件改发到这个邮箱（不发给买家），测试完清空

    # 售后邮箱：定时用 IMAP 收买家发来的照片/视频（邮件附件或 WeTransfer 链接），按订单号存到对应店铺文件夹
    aftersales_mail_enabled: bool = False  # 账号密码填好后改 true
    aftersales_imap_host: str = "imap.exmail.qq.com"  # 企业微信邮箱（腾讯企业邮）
    aftersales_imap_port: int = 993  # SSL
    aftersales_mail_user: str | None = None  # 售后邮箱地址
    aftersales_mail_password: str | None = None  # 客户端专用密码，只放 .env
    aftersales_mail_folder: str = "INBOX"  # 收件文件夹
    aftersales_mail_done_folder: str = "已处理"  # 处理完移到这里（没有就自动建）
    aftersales_mail_poll_seconds: int = 300
    aftersales_mail_share_with_buyers: bool = True  # AI 能否把售后邮箱地址告诉买家（请买家发视频/大文件）
    # 对买家说的售后邮箱（买家发视频、在 WeTransfer 里填的收件邮箱）；空 = 用 AFTERSALES_MAIL_USER。
    # 填别的地址时，发到这个地址的邮件要能进系统读的邮箱（同一个邮箱，或设了自动转发）
    aftersales_public_address: str | None = None
    aftersales_files_dir: str = "data/after_sales_files"  # 相对项目根目录
    kb_docs_dir: str = "data/kb_documents"  # 参考文档库的文件存放目录（相对项目根目录，在 D 盘项目下）
    kb_docs_max_mb: int = 30  # 参考文档单个文件大小上限（MB）
    aftersales_file_max_mb: int = 2048  # 单个文件上限，超过就丢弃并提醒人工
    aftersales_retention_days: int = 30  # 附件文件和“已处理”里的邮件保留天数
    aftersales_cleanup_time: str = "04:00"  # 每天清理时间（本机时间）
    aftersales_virus_scan: bool = True  # 用 Windows Defender 扫描保存的文件；找不到扫描程序记为 SKIPPED

    # 买家上传页（upload.diwhx.xyz）：买家校验订单后填问题、传视频；文件分块直传 Cloudflare R2，再由后台拉回售后附件目录
    upload_page_enabled: bool = False  # R2、Turnstile 配好后改 true
    upload_public_url: str = "https://upload.diwhx.xyz"  # 上传页的公网地址（生成给买家的链接用）
    r2_account_id: str | None = None  # Cloudflare 账号 ID（R2 地址 https://<账号ID>.r2.cloudflarestorage.com）
    r2_access_key_id: str | None = None  # 只能读写这个存储桶的 R2 API 令牌
    r2_secret_access_key: str | None = None
    r2_bucket: str | None = None  # 私有存储桶名
    r2_endpoint: str | None = None  # 留空按账号 ID 拼；测试或自定义时才填
    turnstile_site_key: str | None = None  # Cloudflare Turnstile 人机验证（绑定上传页域名）
    turnstile_secret_key: str | None = None
    upload_order_max_gb: float = 5  # 每个订单最多上传总量
    upload_max_files: int = 20  # 每次上传最多文件数
    upload_link_days: int = 7  # 客服生成的上传链接有效天数
    upload_session_hours: int = 12  # 买家在上传页校验通过后，会话令牌有效小时数
    upload_part_mb: int = 16  # 分块大小（MB）
    upload_link_secret: str | None = None  # 上传链接签名密钥；留空用 CREDENTIALS_MASTER_KEY 派生

    # 物流查询：谷仓 OMS 按 eBay 订单号查出库单与物流号（只读），17TRACK 查轨迹
    goodcang_endpoint: str = "https://oms.goodcang.net/public_open"
    goodcang_app_token: str | None = None  # 谷仓 OMS 开放接口 app-token，只放 .env
    goodcang_app_key: str | None = None
    goodcang_min_request_interval_ms: int = 1200  # 谷仓按密钥限流：相邻请求最小间隔
    # 回复页右侧栏（相关信息、客户资料、补充信息）里 eBay 订单 / 刊登 / 包裹、谷仓出库单可以用多久以内的缓存；
    # 右上角“刷新”强制重查。AI 起草回复不受这个影响（仍按各自较短的缓存）
    panel_cache_seconds: int = 3600
    goodcang_cache_seconds: int = 600  # 同一 eBay 订单的谷仓出库单多少秒内直接用上次查到的（物流、收件信息共用）；0 = 不缓存
    track17_enabled: bool = False  # 填好 TRACK17_API_KEY 后改 true
    track17_api_key: str | None = None  # 17TRACK 密钥（请求头 17token），只放 .env
    track17_base_url: str = "https://api.17track.net/track/v2.4"
    track17_cache_minutes: int = 120  # 同一物流号多久内直接用缓存（17TRACK 本身 6–12 小时更新一次）
    track17_register_daily_limit: int = 50  # 每天最多新注册多少个物流号（每注册一个扣 1 单额度）
    track17_wait_seconds: int = 12  # 新注册后等轨迹返回的最长秒数，超时先返回基本信息
    logistics_stall_days: int = 4  # 轨迹多少天没更新算“断更”：转人工跟进（可建断更查件单）
    logistics_reship_days: int = 7  # 轨迹多少天没更新要人工判断是否补发
    logistics_skip_sundays: bool = True  # 算“几天没更新”时不算周日（周日不派送、轨迹通常不动）
    logistics_pickup_days: int = 7  # 包裹在取件点放了几天还没取：转人工（取件点一般只保留 7 天左右，过期退回）
    track17_min_quota: int = 10  # 17TRACK 剩余额度低于这个数就不再注册新物流号（已注册的照常免费查）
    logistics_ticket_sync_time: str = "10:00"  # 每天同步谷仓查件单的时间（本机时间）；没开机的启动后补跑
    logistics_ticket_sync_days: int = 30  # 每次同步最近多少天创建的查件单（谷仓上限 186 天）
    logistics_ticket_sync_interval_minutes: int = 60  # 每隔多少分钟同步一次查件单；0 = 只按 LOGISTICS_TICKET_SYNC_TIME 每天一次
    logistics_ticket_auto_send: bool = False  # 谷仓回复后自动把查件结果（AI 起草的消息 + 附件）发给买家（目前为模拟发送）；需人工的不自动发
    # 飞书售后群工单：需技术/售后判断的问题（质量问题、破损错发缺件、安装技术问题、买家发 VIN 核对适配）整理成工单发群，
    # 群里在工单话题回复或 @ 机器人后，AI 起草买家回复，人工审核后才发
    feishu_app_id: str | None = None  # 飞书企业自建应用（机器人），只放 .env
    feishu_app_secret: str | None = None
    feishu_domain: str = "https://open.feishu.cn"
    feishu_case_enabled: bool = False  # 开启后工作台可把工单发到售后群，并定时读取群里的回复
    feishu_case_chat_id: str | None = None  # 工单发到哪个群（chat_id，如 oc_xxx）；测试先用售后测试群
    # 买家在 eBay 消息里发来的 WeTransfer 视频链接自动下载进售后附件（其他网盘链接登记提醒人工下载），飞书工单再发到话题
    message_links_enabled: bool = True
    feishu_case_auto_send: bool = False  # 资料齐全的工单自动发到售后群（不用在工作台点“发到售后群”）；缺资料的先请买家补，补齐后自动发
    feishu_case_poll_seconds: int = 120  # 每隔多少秒读一次群里的回复（没开飞书事件订阅时用轮询）
    ship_notice_enabled: bool = False  # 汇总当天已发货的订单，生成发货通知（订单号、承运商、跟踪号、查件链接）待人工审核发送
    ship_notice_live_only: bool = True  # 只给对接 eBay 的店铺（EBAY_LIVE_STORES）生成发货通知；其余店铺发不出去，生成了也没用
    ship_notice_interval_minutes: int = 60  # 每隔多少分钟汇总一次当天已发货的订单
    ship_notice_wait_for_warehouse: bool = True  # 等谷仓出库单真正“已发货”（或有揽收扫描）再生成通知，买家拿到单号就查得到
    ship_notice_lookback_days: int = 3  # 等仓库发出时，往回看几天内 eBay 标记发货、还没生成通知的订单
    auto_translate_buyer_messages: bool = False  # 买家来信自动译成中文（新拉到的和打开会话时还没译的），工作台显示原文 + 译文
    logistics_ticket_auto_draft: bool = False  # 物流需人工（断更、异常、妥投未收到）时自动起草查件单（只存本系统），客服在工作台核对后提交到谷仓
    logistics_pod_retention_days: int = 180  # 谷仓查件单回复附件（POD 签收证明等）保留天数（纠纷证据，比售后附件长）
    # 发给买家的附件（妥投照片等）公网地址前缀，必须 HTTPS、经 Cloudflare 隧道到本机；留空用 EBAY_NOTIFY_ENDPOINT 的域名
    public_media_base_url: str | None = None
    logistics_ticket_reply_days: int = 7  # 谷仓回复后几天内还没处理的照样处理（main.py 停机几天也不漏）；更早的不再通知买家
    logistics_ticket_followup_days: int = 3  # 查件单谷仓回复（或提交后没回复）超过几天、包裹问题还在：不再算“在跟”，转人工判断补发
    logistics_ticket_auto_submit: bool = False  # 起草后直接提交到谷仓（不用人工核对）；这类回复（告诉买家已发起查件）也不再因物流原因转人工
    logistics_ticket_send_needs_human: bool = False  # 谷仓回复后需要人工的结果（丢失、退回、妥投未收到等）也直接回复买家，同时标待售后
    logistics_ticket_submit_notice: bool = True  # 查件单提交到谷仓后马上给买家发一条安抚消息（已转物流同事、已向承运商查询，有结果马上通知）；谷仓回复后再发查件结果
    logistics_ticket_notify_emails: str = ""  # 查件单通知邮箱（谷仓必填，最多 10 个，逗号分隔）

    # eBay 消息拉取（只读，Commerce Message API）
    ebay_env: str = "production"  # production / sandbox
    ebay_client_id: str | None = None  # App ID（Client ID）
    ebay_client_secret: str | None = None  # Cert ID（Client Secret）
    ebay_dev_id: str | None = None  # Dev ID：REST 接口不需要，留作 Trading API 备用
    ebay_refresh_token: str | None = None  # 店铺授权给本应用的 OAuth refresh token（须含 commerce.message 权限）
    ebay_user_access_token: str | None = None  # 可选：临时测试用的 OAuth 用户访问令牌（约 2 小时过期）
    ebay_store_id: str = "EBAY_STORE_1"  # 本系统内部店铺 ID
    ebay_store_name: str | None = None
    ebay_store_marketplace: str = "EBAY_US"
    ebay_store_marketplaces: str = "EBAY_US,EBAY_DE,EBAY_GB"  # 店铺开通的站点；刊登所在站点不在其中时会话站点记为 UNKNOWN
    ebay_store_timezone: str = "America/Los_Angeles"
    ebay_seller_username: str | None = None  # 店铺自己的 eBay 用户名；留空时从会话参与方自动推断
    ebay_sync_enabled: bool = True  # 凭证齐全时后台定时拉取
    ebay_sync_interval_seconds: int = 300
    ebay_sync_lookback_days: int = 7  # 首次同步回溯天数
    ebay_sync_stores: str | None = None  # 只拉这些店铺（店铺名、ID 或 eBay 账号，逗号分隔；* = 全部店铺）；留空 = 拉“对接 eBay 的店铺”。发送范围另看 EBAY_LIVE_STORES
    ebay_sync_create_reply_jobs: bool = False  # 拉到的买家消息是否进入 AI 处理；默认否（合规确认前不把真实买家消息发给模型）

    # 运行边界
    agent_mode: str = "fixed_pipeline"
    knowledge_mode: str = "full_context"
    knowledge_full_context_max_tokens: int = 12000
    knowledge_retrieval_top_k: int = 6
    knowledge_retrieval_candidates: int = 20
    knowledge_retrieval_max_tokens: int = 6000
    agent_input_budget_tokens: int = 20000
    knowledge_date_override: date | None = None
    message_merge_window_seconds: int = 180
    agent_max_tool_requests: int = 5
    agent_prefetch_max_tool_calls: int = 8
    agent_max_replans: int = 2
    agent_max_output_repairs: int = 1
    # 原生工具调用：模型自己决定查商品、订单、物流、售后、SKU；关掉则退回单次生成（只有程序预取的订单与售后）
    agent_tool_calling: bool = True
    agent_max_tool_rounds: int = 4  # 最多几轮工具调用，用完后让模型直接作答
    agent_deadline_seconds: int = 45
    auto_send_enabled: bool = False
    auto_send_intents: str | None = None
    # 只向买家索要信息（action=clarify）时也允许自动发的 L1 意图，逗号分隔，如 fitment（先要 VIN）
    auto_send_clarify_intents: str | None = None
    # 按商品信息（get_item_details）作答时也允许自动发的意图，逗号分隔，如 item_spec,shipping_cost,handling_time
    auto_send_listing_intents: str | None = None
    auto_send_max_risk: str = "L0"  # 自动发送允许的最高风险：L0 / L1；L2、L3 永远不自动发
    auto_send_exclude_intents: str | None = None  # 白名单为 * 时仍要人工的意图，如 fitment,price_offer
    auto_send_evidence: str = "kb"  # kb：必须引用知识库；any：任一依据（知识库/商品/查询结果）或回复无事实陈述
    # 转人工的来信 N 分钟内没人回复，自动给买家发“已收到，同事正在处理，会尽快回复”（见 review_ack.py）。
    # 2026-11-09 按用户要求默认关闭：没人回复的来信一律保持待审核，系统不替人发（含买家致谢时的自动简短致谢）
    review_ack_enabled: bool = False
    review_ack_delay_minutes: int = 30  # 来信后多少分钟还没人回复就发回执
    review_ack_max_age_hours: int = 24  # 超过多少小时的旧来信不再补发（刚开启时不给积压消息补发）
    review_ack_cooldown_hours: int = 12  # 发过回执还没正式回复时，买家追问多少小时内不重复发
    auto_reply_max_streak: int = 1  # 同一会话 AI 最多连续自动回复几次，达到后买家再来信交人工
    # 同事人工跟进中：上一条店铺回复是人工发的（工作台人工发送或直接在 eBay 后台回复）且在这么多小时内，
    # AI 判断要转人工时不再起草“已转交同事”的回复，直接留给跟进的同事；0 = 关闭
    human_followup_hours: int = 72
    auto_tag_enabled: bool = True  # 按 AI 识别的问题类型自动给会话打已有的标记（A待确认适配、查物流、退货等）

    compliance_extra_url_whitelist: str | None = None

    # 本地服务
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_open_browser: bool = True
    frontend_auto_build: bool = True
    worker_poll_seconds: float = 2
    claim_ttl_seconds: int = 600
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    @property
    def effective_database_url(self) -> str:
        if self.database_url:
            if not self.database_password:
                return self.database_url
            from sqlalchemy.engine import make_url

            # set(password=) 会自动做 URL 编码，密码里有特殊字符也没问题
            return make_url(self.database_url).set(password=self.database_password).render_as_string(hide_password=False)
        DATA_DIR.mkdir(exist_ok=True)
        return f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}"

    @property
    def database_display(self) -> str:
        """日志与状态页用：不含密码。"""
        if self.using_sqlite_fallback:
            return f"SQLite（本地开发回退）{self.effective_database_url}"
        from sqlalchemy.engine import make_url

        return make_url(self.database_url).render_as_string(hide_password=True)

    @property
    def translate_model_name(self) -> str | None:
        return (self.llm_translate_model or "").strip() or self.llm_chat_model

    def translate_extra_body(self) -> dict | None:
        """翻译请求带的额外参数：沿用 LLM_EXTRA_BODY，但思考模式按 LLM_TRANSLATE_THINKING（默认关）。"""
        body = dict(self.extra_body() or {})
        if "enable_thinking" in body or (self.translate_model_name or "").startswith("qwen3"):
            body["enable_thinking"] = bool(self.llm_translate_thinking)
        return body or None

    def extra_body(self) -> dict | None:
        if not self.llm_extra_body:
            return None
        value = json.loads(self.llm_extra_body)
        if not isinstance(value, dict):
            raise ValueError("LLM_EXTRA_BODY 必须是 JSON 对象")
        return value

    @property
    def using_sqlite_fallback(self) -> bool:
        return not self.database_url

    @property
    def output_token_reserve(self) -> int:
        return self.llm_max_output_tokens or 2000

    @property
    def input_token_budget(self) -> int:
        budget = self.agent_input_budget_tokens
        if self.llm_context_window:
            budget = min(budget, self.llm_context_window - self.output_token_reserve)
        return budget

    @property
    def auto_send_intent_set(self) -> set[str]:
        return {s.strip() for s in (self.auto_send_intents or "").split(",") if s.strip()}

    @property
    def auto_send_exclude_intent_set(self) -> set[str]:
        return {s.strip() for s in (self.auto_send_exclude_intents or "").split(",") if s.strip()}

    @property
    def auto_send_listing_intent_set(self) -> set[str]:
        return {s.strip() for s in (self.auto_send_listing_intents or "").split(",") if s.strip()}

    @property
    def ebay_live_store_keys(self) -> set[str]:
        return {s.strip().lower() for s in (self.ebay_live_stores or "").replace("，", ",").split(",") if s.strip()}

    @property
    def offer_live_store_keys(self) -> set[str]:
        return {s.strip().lower() for s in (self.offer_live_stores or "").replace("，", ",").split(",") if s.strip()}

    @property
    def auto_send_clarify_intent_set(self) -> set[str]:
        return {s.strip() for s in (self.auto_send_clarify_intents or "").split(",") if s.strip()}

    @property
    def ebay_marketplace_set(self) -> list[str]:
        return [m.strip().upper() for m in (self.ebay_store_marketplaces or "").split(",") if m.strip()]

    @property
    def ebay_api_base(self) -> str:
        return "https://api.sandbox.ebay.com" if self.ebay_env == "sandbox" else "https://api.ebay.com"

    def ebay_not_ready_reasons(self) -> list[str]:
        reasons = [f"{name} 未配置" for name, v in (
            ("EBAY_CLIENT_ID", self.ebay_client_id),
            ("EBAY_CLIENT_SECRET", self.ebay_client_secret),
        ) if not v]
        if not (self.ebay_refresh_token or self.ebay_user_access_token):
            reasons.append("EBAY_REFRESH_TOKEN 未配置（或临时填 EBAY_USER_ACCESS_TOKEN）")
        if self.ebay_env not in ("production", "sandbox"):
            reasons.append(f"EBAY_ENV={self.ebay_env} 无效（production / sandbox）")
        return reasons

    def knowledge_date(self) -> date:
        return self.knowledge_date_override or date.today()

    def llm_missing_fields(self) -> list[str]:
        missing = []
        if not self.llm_provider:
            missing.append("LLM_PROVIDER")
        if not self.llm_chat_model:
            missing.append("LLM_CHAT_MODEL")
        if not self.llm_api_key:
            missing.append("LLM_API_KEY")
        return missing

    def agent_not_ready_reasons(self) -> list[str]:
        """AGENT_NOT_READY 的原因；空列表表示 Agent 可以生成草稿（第 15.2 节）。"""
        if not self.llm_enabled:
            return ["LLM_ENABLED=false：已停用全部大模型（不生成草稿、不翻译、不识图、不做向量检索）"]
        reasons = [f"{f} 未配置" for f in self.llm_missing_fields()]
        if self.llm_provider == "codex_cli":
            reasons.append("LLM_PROVIDER=codex_cli 已移除（不再支持 ChatGPT 账号通道），请改为 openai_compatible")
        elif self.llm_provider and self.llm_provider not in SUPPORTED_LLM_PROVIDERS:
            reasons.append(f"LLM_PROVIDER={self.llm_provider} 暂不支持（可用：{', '.join(sorted(SUPPORTED_LLM_PROVIDERS))}）")
        if self.llm_base_url and "{" in self.llm_base_url:
            reasons.append("LLM_BASE_URL 里还有未替换的 {…} 占位符")
        try:
            self.extra_body()
        except ValueError as e:
            reasons.append(f"LLM_EXTRA_BODY 不是合法 JSON 对象：{e}")
        if self.llm_structured_output not in STRUCTURED_OUTPUT_METHODS:
            reasons.append(f"LLM_STRUCTURED_OUTPUT={self.llm_structured_output} 无效")
        if self.agent_mode not in SUPPORTED_AGENT_MODES:
            reasons.append(f"AGENT_MODE={self.agent_mode} 尚未实现（当前仅 fixed_pipeline）")
        if self.llm_context_window and self.input_token_budget <= 0:
            reasons.append("LLM_CONTEXT_WINDOW 小于输出预留，无法组装提示")
        return reasons

    @property
    def effective_agent_deadline_seconds(self) -> int:
        return self.agent_deadline_seconds

    @property
    def llm_display_name(self) -> str:
        """给人看的模型名（记在生成步骤里，工作台“生成依据”展示）。"""
        return self.llm_chat_model or "未配置"

    @property
    def media_root(self) -> Path:
        path = Path(self.media_dir)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def media_allowed_host_list(self) -> list[str]:
        return [h.strip().lower().lstrip(".") for h in self.media_allowed_hosts.split(",") if h.strip()]

    @property
    def invoice_root(self) -> Path:
        path = Path(self.invoice_dir)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def kb_docs_root(self) -> Path:
        path = Path(self.kb_docs_dir)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def aftersales_root(self) -> Path:
        path = Path(self.aftersales_files_dir)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def feishu_ready(self) -> bool:
        return bool(self.feishu_case_enabled and self.feishu_app_id and self.feishu_app_secret and self.feishu_case_chat_id)

    @property
    def goodcang_ready(self) -> bool:
        return bool(self.goodcang_endpoint and self.goodcang_app_token and self.goodcang_app_key)

    @property
    def logistics_ticket_emails(self) -> list[str]:
        return [e.strip() for e in self.logistics_ticket_notify_emails.replace("；", ",").replace(";", ",").split(",")
                if "@" in e][:10]

    def track17_not_ready_reasons(self) -> list[str]:
        reasons = [] if self.track17_enabled else ["TRACK17_ENABLED 未开启"]
        if not self.track17_api_key:
            reasons.append("TRACK17_API_KEY 未填")
        return reasons

    def aftersales_mail_not_ready_reasons(self) -> list[str]:
        reasons = [] if self.aftersales_mail_enabled else ["AFTERSALES_MAIL_ENABLED 未开启"]
        for name, v in (("AFTERSALES_IMAP_HOST", self.aftersales_imap_host), ("AFTERSALES_MAIL_USER", self.aftersales_mail_user),
                        ("AFTERSALES_MAIL_PASSWORD", self.aftersales_mail_password)):
            if not v:
                reasons.append(f"{name} 未配置")
        return reasons

    @property
    def r2_endpoint_url(self) -> str | None:
        if self.r2_endpoint:
            return self.r2_endpoint
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com" if self.r2_account_id else None

    def upload_not_ready_reasons(self) -> list[str]:
        """上传页不能用的原因；空列表表示可以用。"""
        reasons = [] if self.upload_page_enabled else ["UPLOAD_PAGE_ENABLED 未开启"]
        for name, v in (("R2_ACCOUNT_ID 或 R2_ENDPOINT", self.r2_endpoint_url), ("R2_ACCESS_KEY_ID", self.r2_access_key_id),
                        ("R2_SECRET_ACCESS_KEY", self.r2_secret_access_key), ("R2_BUCKET", self.r2_bucket),
                        ("TURNSTILE_SITE_KEY", self.turnstile_site_key), ("TURNSTILE_SECRET_KEY", self.turnstile_secret_key),
                        ("UPLOAD_LINK_SECRET 或 CREDENTIALS_MASTER_KEY", self.upload_link_secret or self.credentials_master_key)):
            if not v:
                reasons.append(f"{name} 未配置")
        return reasons

    @property
    def aftersales_public_email(self) -> str | None:
        """AI 可以告诉买家的售后邮箱（开启分享且配置了邮箱才有）。"""
        addr = (self.aftersales_public_address or self.aftersales_mail_user or "").strip()
        return addr if self.aftersales_mail_share_with_buyers and addr else None

    def invoice_mail_not_ready_reasons(self) -> list[str]:
        """发票邮件不能真正发出的原因；空列表表示可以发。INVOICE_MAIL_ENABLED=false 时只模拟发送。"""
        reasons = [] if self.invoice_mail_enabled else ["INVOICE_MAIL_ENABLED 未开启（只模拟发送）"]
        for name, v in (("INVOICE_SMTP_HOST", self.invoice_smtp_host), ("INVOICE_SMTP_USER", self.invoice_smtp_user),
                        ("INVOICE_SMTP_PASSWORD", self.invoice_smtp_password)):
            if not v:
                reasons.append(f"{name} 未配置")
        return reasons

    def vision_not_ready_reasons(self) -> list[str]:
        """视觉观察不可用的原因；空列表表示可以读图。VISION_ENABLED=false 也算不可用。"""
        reasons = []
        if not self.llm_enabled:
            reasons.append("LLM_ENABLED=false：已停用全部大模型")
        if not self.vision_enabled:
            reasons.append("VISION_ENABLED 未开启")
        for name, v in (("VISION_PROVIDER", self.vision_provider), ("VISION_MODEL", self.vision_model),
                        ("VISION_API_KEY", self.vision_api_key)):
            if not v:
                reasons.append(f"{name} 未配置")
        if self.vision_provider and self.vision_provider != "openai_compatible":
            reasons.append(f"VISION_PROVIDER={self.vision_provider} 暂不支持")
        return reasons

    def vision_config_version(self) -> str:
        public = {"provider": self.vision_provider, "base_url": self.vision_base_url, "model": self.vision_model}
        return "vc-" + hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()[:10]

    def rag_not_ready_reasons(self) -> list[str]:
        """只检查配置；pgvector 是否可用要查库，见 knowledge.vector_index.readiness。"""
        reasons = []
        if self.knowledge_mode not in SUPPORTED_KNOWLEDGE_MODES:
            reasons.append(f"KNOWLEDGE_MODE={self.knowledge_mode} 不支持（full_context / retrieval）")
        elif self.knowledge_mode == "retrieval":
            if not (self.embedding_provider and self.embedding_model and self.embedding_api_key and self.embedding_dimension):
                reasons.append("检索模式需要独立配置 EMBEDDING_*（含维度）")
        return reasons

    def model_config_version(self) -> str:
        """模型配置版本：只对非密钥字段取哈希，便于追溯草稿用的是哪套配置。"""
        public = {
            "provider": self.llm_provider,
            "base_url": self.llm_base_url,
            "model": self.llm_chat_model,
            "max_output": self.llm_max_output_tokens,
            "temperature": self.llm_temperature,
            "structured": self.llm_structured_output,
            "extra_body": self.llm_extra_body,
        }
        digest = hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()[:10]
        return f"mc-{digest}"

    def masked_status(self) -> dict:
        """给前端看的脱敏状态：只说字段是否已配置，不返回 Key 和完整连接串。"""

        def filled(v) -> bool:
            return v not in (None, "")

        return {
            "llm": {
                "provider": self.llm_provider,
                "model": self.llm_chat_model,
                "base_url_configured": filled(self.llm_base_url),
                "api_key_configured": filled(self.llm_api_key),
                "context_window": self.llm_context_window,
                "max_output_tokens": self.llm_max_output_tokens,
                "structured_output": self.llm_structured_output,
                "extra_body": self.llm_extra_body,
            },
            "embedding": {
                "provider": self.embedding_provider,
                "model": self.embedding_model,
                "base_url_configured": filled(self.embedding_base_url),
                "api_key_configured": filled(self.embedding_api_key),
                "dimension": self.embedding_dimension,
            },
            "vision": {"provider": self.vision_provider, "model": self.vision_model,
                       "api_key_configured": filled(self.vision_api_key), "enabled": self.vision_enabled,
                       "not_ready_reasons": self.vision_not_ready_reasons(),
                       "max_images_per_job": self.vision_max_images_per_job},
            "media": {"dir": self.media_dir, "max_bytes": self.media_max_bytes,
                      "max_per_message": self.media_max_per_message, "allowed_hosts": self.media_allowed_host_list,
                      "retention_days": self.media_retention_days},
            "retention": {"days": self.data_retention_days, "cleanup_time": self.retention_cleanup_time,
                          "batch_size": self.retention_batch_size, "dry_run": self.retention_dry_run,
                          "audit_days": self.audit_retention_days},
            "database": self.database_display,
            "redis": self.redis_url or "未配置（进程内唤醒）",
            "business_source": self.business_source,
            "ebay_channel": self.ebay_channel,
            "agent_mode": self.agent_mode,
            "knowledge_mode": self.knowledge_mode,
            "knowledge_full_context_max_tokens": self.knowledge_full_context_max_tokens,
            "knowledge_retrieval": {"top_k": self.knowledge_retrieval_top_k,
                                    "candidates": self.knowledge_retrieval_candidates,
                                    "max_tokens": self.knowledge_retrieval_max_tokens},
            "input_token_budget": self.input_token_budget,
            "knowledge_date": self.knowledge_date().isoformat(),
            "merge_window_seconds": self.message_merge_window_seconds,
            "auto_send_enabled": self.auto_send_enabled,
            "model_config_version": self.model_config_version(),
        }


# 系统配置页（runtime_settings.py）里管理员改过的项，优先于 .env
_overrides: dict = {}


def set_overrides(values: dict) -> None:
    _overrides.clear()
    _overrides.update(values)
    get_settings.cache_clear()


@lru_cache
def get_settings() -> Settings:
    return Settings(**_overrides)
