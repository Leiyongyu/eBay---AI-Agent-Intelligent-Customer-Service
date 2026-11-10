"""数据表（开发方案第 13 章的首期子集）。

字段名尽量与方案保持一致；尚未用到的表（outbox_events、history_links、conversation_summaries 等）
等对应功能落地时再加，避免空表误导。

约定：每张表、每个字段都必须写 comment（中文），PostgreSQL 启动时会同步成 COMMENT ON，
tests/test_models.py 会检查漏写。时间字段一律存无时区的 UTC 时间。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text as sql_text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


class JobState:
    """reply_jobs.state（第 13 章状态机）。"""

    RECEIVED = "RECEIVED"  # 已入队，等待合并窗口结束
    RUNNING = "RUNNING"  # Worker 正在运行 Agent
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    AUTO_ELIGIBLE = "AUTO_ELIGIBLE"  # 符合自动候选条件；P3 之前同样进入人工审核
    SENDING = "SENDING"
    SENT = "SENT"
    SEND_UNCERTAIN = "SEND_UNCERTAIN"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    FAILED_HANDOFF = "FAILED_HANDOFF"
    CANCELLED_STALE = "CANCELLED_STALE"
    RESOLVED_NO_REPLY = "RESOLVED_NO_REPLY"  # 审核员判定无需回复（如致谢）

    PENDING_REVIEW = (REVIEW_REQUIRED, AUTO_ELIGIBLE, FAILED_HANDOFF)
    REPLACEABLE = (RECEIVED, RUNNING, REVIEW_REQUIRED, AUTO_ELIGIBLE, FAILED_HANDOFF, RETRYABLE_ERROR)
    LOCKED_FOR_SEND = (SENDING, SEND_UNCERTAIN, SENT)


JOB_STATE_DOC = ("RECEIVED 等待合并窗口 / RUNNING 生成中 / REVIEW_REQUIRED 待审核 / AUTO_ELIGIBLE 自动候选 / "
                 "SENDING 发送中 / SENT 已发送 / SEND_UNCERTAIN 发送结果不确定 / RETRYABLE_ERROR 待重试 / "
                 "FAILED_HANDOFF 失败转人工 / CANCELLED_STALE 被新消息合并作废 / RESOLVED_NO_REPLY 无需回复")


class Role:
    ADMIN = "ADMIN"  # 管理员：账号、店铺分配、知识库、系统设置，全部店铺
    CS = "CS"  # 客服：处理分配店铺的全部来信，回复不了的分类标记转售后
    AFTERSALES = "AFTERSALES"  # 售后：处理客服标记的售后会话
    LABELS = {ADMIN: "管理员", CS: "客服", AFTERSALES: "售后"}


class Aftersales:
    """客服标记转售后的问题类型与状态。"""
    PENDING, IN_PROGRESS, DONE = "PENDING", "IN_PROGRESS", "DONE"
    STATUS_LABELS = {PENDING: "待售后处理", IN_PROGRESS: "售后处理中", DONE: "售后已完成"}
    TYPES = ["退货", "退款", "赔偿/部分退款", "不适配", "破损/错发/缺件", "质量问题", "物流异常/未收到", "物流问题需判断补发", "延迟发货", "取消订单",
             "改地址", "其他"]
    # AI 识别的意图 → 标记时默认选中的类型（只是建议，客服可改）
    INTENT_HINT = {"return_refund": "退货", "refund_status": "退款", "damaged_wrong": "破损/错发/缺件", "quality_issue": "质量问题",
                   "cancel_request": "取消订单", "address_change": "改地址", "delivery_delay": "物流异常/未收到",
                   "fitment": "不适配"}


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"comment": "工作台账号：只有管理员能创建；密码只存 scrypt 哈希"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    username: Mapped[str] = mapped_column(String(64), unique=True, comment="登录名（不区分大小写，存小写）")
    display_name: Mapped[str] = mapped_column(String(64), comment="显示名：工作台署名、审计与“处理人”用")
    role: Mapped[str] = mapped_column(String(16), comment="角色：ADMIN 管理员 / CS 客服 / AFTERSALES 售后")
    password_hash: Mapped[str] = mapped_column(String(255), comment="scrypt 密码哈希（含盐与参数），不存明文")
    all_stores: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否负责全部店铺（管理员始终为全部）")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用；停用后不能登录，已登录会话立即失效")
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, comment="下次登录须修改密码（管理员新建或重置密码后）")
    can_aftersales_files: Mapped[bool | None] = mapped_column(
        Boolean, default=False, comment="可看售后附件（售后邮箱收到的照片、视频）；管理员始终可看，范围仍按负责的店铺")
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, comment="连续登录失败次数，成功后清零")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, comment="连续失败过多后锁定到此时间（UTC）")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最近登录时间（UTC）")
    created_by: Mapped[str | None] = mapped_column(String(64), comment="创建人（管理员登录名）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="最近修改时间（UTC）")


class UserStore(Base):
    __tablename__ = "user_stores"
    __table_args__ = {"comment": "账号负责的店铺（客服、售后只看得到、只处理这些店铺）"}

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True, comment="账号 ID")
    store_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="店铺 ID")


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = {"comment": "登录会话：浏览器 Cookie 里只放随机令牌，库里只存令牌的 SHA-256"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, comment="会话令牌的 SHA-256（不存明文令牌）")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, comment="账号 ID")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="登录时间（UTC）")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="最近访问时间（UTC）")
    expires_at: Mapped[datetime] = mapped_column(DateTime, comment="过期时间（UTC），访问时顺延")
    ip: Mapped[str | None] = mapped_column(String(64), comment="登录 IP")
    user_agent: Mapped[str | None] = mapped_column(String(300), comment="浏览器标识")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, comment="退出登录或被停用/改密码时间（UTC）")


class Store(Base):
    __tablename__ = "stores"
    __table_args__ = {"comment": "店铺：内部店铺主键映射已授权的 eBay 卖家账号，一个账号可覆盖多个站点"}

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="内部店铺 ID，如 STORE_A")
    name: Mapped[str] = mapped_column(String(200), comment="店铺显示名称")
    ebay_user_id: Mapped[str | None] = mapped_column(String(200), comment="eBay 卖家用户 ID")
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", comment="店铺状态：ACTIVE 启用 / 其他值为停用或隔离")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", comment="店铺时区（IANA 名称），用于计算截单时间等")
    default_marketplace_id: Mapped[str] = mapped_column(String(32), comment="默认 eBay 站点，如 EBAY_US、EBAY_GB")
    auto_send_enabled: Mapped[bool] = mapped_column(Boolean, default=False, comment="店铺“AI 自动回复”开关，默认关闭：开启后 24 小时内的新买家来信自动交给 AI，"
                                                            "通过闸门（含知识库依据、全局 AUTO_SEND_ENABLED）的回复自动发出（只读阶段为模拟发送）")
    retention_days: Mapped[int | None] = mapped_column(
        Integer, comment="本店铺聊天记录保留天数；空 = 用全局 DATA_RETENTION_DAYS，0 = 不清理")
    invoice_issuer_id: Mapped[int | None] = mapped_column(
        Integer, comment="开票主体 ID（invoice_issuers）；空 = 本店铺还不能开发票")


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("store_id", "ebay_conversation_id"),
                      {"comment": "买家会话：一个店铺内的一条 eBay 会话，按 (店铺, eBay 会话 ID) 唯一"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="本系统会话主键")
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), index=True, comment="所属店铺 ID")
    ebay_conversation_id: Mapped[str] = mapped_column(String(128), comment="eBay 平台会话 ID（不透明键）")
    conversation_type: Mapped[str] = mapped_column(String(32), default="FROM_MEMBERS",
                                                   comment="会话类型：FROM_MEMBERS 买卖双方 / FROM_EBAY 系统会话等")
    marketplace_id: Mapped[str] = mapped_column(String(32), default="UNKNOWN",
                                                comment="站点；无法可靠判断时为 UNKNOWN，不套用站点专属政策")
    marketplace_source: Mapped[str | None] = mapped_column(
        String(64), comment="站点来源：listing 刊登（Browse API 查得）/ order 订单 / simulator 模拟器 / "
                            "listing_not_enabled:XX 刊登站点不在店铺站点清单内等")
    buyer_key: Mapped[str] = mapped_column(String(128), comment="买家身份键（平台不透明 ID，不当作显示名解析，不跨店铺合并）")
    item_id: Mapped[str | None] = mapped_column(String(64), comment="会话关联的 eBay 刊登 ID")
    handoff_state: Mapped[str] = mapped_column(String(16), default="NONE", comment="人工接管状态：NONE 未接管 / HUMAN 人工接管")
    channel_source: Mapped[str | None] = mapped_column(
        String(16), comment="会话来源：ebay 真实店铺拉取（只读阶段禁止从本系统发送）/ 空值为模拟器或评测")
    conversation_title: Mapped[str | None] = mapped_column(String(500), comment="eBay 会话标题（通常是刊登标题或系统通知标题）")
    platform_status: Mapped[str | None] = mapped_column(String(16), comment="eBay 侧会话状态：ACTIVE / ARCHIVED / DELETED 等")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后一条消息时间（UTC）")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后一次与平台同步时间（UTC）")
    read_seq: Mapped[int | None] = mapped_column(
        Integer, comment="团队已读位置：会话内顺序号，之后收到的消息算未读；空值表示从未读过（只前进，标为未读时回退）")
    read_by: Mapped[str | None] = mapped_column(
        String(64), comment="最后一次改已读状态的人：审核员名 / eBay后台（同步 eBay 侧已读）/ 初始化")
    read_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后一次改已读状态的时间（UTC）")
    platform_unread_count: Mapped[int | None] = mapped_column(
        Integer, comment="eBay 侧未读条数（最近一次拉取时会话列表的 unreadCount），只读同步、不写回 eBay")
    # 客服分类后转售后：客服判断回复不了的（退货、赔偿、不适配等）标记类型，售后按标记处理
    aftersales_status: Mapped[str | None] = mapped_column(
        String(16), index=True, comment="售后状态：空=未转售后 / PENDING 待售后处理 / IN_PROGRESS 售后处理中 / DONE 售后已完成")
    aftersales_type: Mapped[str | None] = mapped_column(String(32), comment="售后问题类型（客服标记），如 退货、退款、赔偿/部分退款、不适配")
    aftersales_note: Mapped[str | None] = mapped_column(Text, comment="转售后说明与售后处理备注")
    aftersales_marked_by: Mapped[str | None] = mapped_column(String(64), comment="标记转售后的客服")
    aftersales_marked_at: Mapped[datetime | None] = mapped_column(DateTime, comment="标记转售后时间（UTC）")
    aftersales_handled_by: Mapped[str | None] = mapped_column(String(64), comment="接手处理的售后")
    aftersales_updated_at: Mapped[datetime | None] = mapped_column(DateTime, comment="售后状态最近变更时间（UTC）")
    starred: Mapped[bool | None] = mapped_column(Boolean, default=False, index=True, comment="星标（同事共用，工作台可按星标筛选）")
    starred_by: Mapped[str | None] = mapped_column(String(64), comment="标星的人")
    starred_at: Mapped[datetime | None] = mapped_column(DateTime, comment="标星时间（UTC）")
    situation_stage: Mapped[str | None] = mapped_column(
        String(16), index=True, comment="会话情况阶段（AI 每次起草时更新）：presale 售前 / shipping 物流 / aftersales 售后 / invoice 发票 / other 其他")
    situation_label: Mapped[str | None] = mapped_column(String(64), comment="会话情况一句话概括（AI 起草时给出，中文）")
    situation_at: Mapped[datetime | None] = mapped_column(DateTime, comment="会话情况标签更新时间（UTC）")
    state_card: Mapped[dict | None] = mapped_column(
        JSON, comment="会话状态卡（起草时放进提示词）：topic 当前主题、issues 未解决问题（source=system 由程序按查件单、纠纷、"
                      "发票、异常包裹等写，模型不能改；source=model 由模型在起草时顺带更新）、resolved 已解决问题、"
                      "facts 关键事实（订单号、VIN、车型、SKU、已收到的图片视频）、promises 我们答应过买家的事")
    state_updated_at: Mapped[datetime | None] = mapped_column(DateTime, comment="会话状态卡最近更新时间（UTC）")
    row_version: Mapped[int] = mapped_column(Integer, default=1,
                                             comment="会话版本号：新消息、系统事件时 +1；批准发送时用于检测过期")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("store_id", "ebay_message_id"),
                      {"comment": "会话消息：买家来信、店铺实际发出的回复、eBay 系统消息；按 (店铺, 平台消息 ID) 去重"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="本系统消息主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="所属店铺 ID")
    ebay_message_id: Mapped[str] = mapped_column(String(128), comment="eBay 平台消息 ID（模拟器生成的以 SIM-/MOCK- 开头）")
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True, comment="所属会话 ID")
    conversation_seq: Mapped[int] = mapped_column(Integer, comment="会话内顺序号，从 1 开始")
    sender_role: Mapped[str] = mapped_column(String(16), comment="发送方：BUYER 买家 / SELLER 店铺 / SYSTEM eBay 系统")
    direction: Mapped[str] = mapped_column(String(8), comment="方向：IN 收到 / OUT 店铺发出")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="消息时间（UTC）")
    body: Mapped[str] = mapped_column(Text, comment="消息正文（纯文本；eBay 的 HTML 邮件通知已转成纯文本，原文见 body_html）")
    body_html: Mapped[str | None] = mapped_column(Text, comment="eBay 原始 HTML 正文（仅系统通知等 HTML 消息有），供工作台“查看原邮件”")
    # 机器中文译文，只供审核员理解，不进入会话记忆、不作为证据（第 5.2 节）
    zh_translation: Mapped[str | None] = mapped_column(Text, comment="机器中文译文，仅供审核员理解，不进入会话记忆、不作为证据")
    raw_ref: Mapped[str | None] = mapped_column(String(200), comment="原始平台载荷的引用位置（可选）")
    subject: Mapped[str | None] = mapped_column(String(500), comment="eBay 消息主题")
    sender_key: Mapped[str | None] = mapped_column(String(128), comment="发送方 eBay 用户名或不可变用户 ID（不透明键）")
    media: Mapped[list | None] = mapped_column(JSON, comment="附件元数据原样（eBay messageMedia：名称、类型、地址）；处理状态见 message_attachments")
    auto_kind: Mapped[str | None] = mapped_column(String(16), comment="系统自动发的消息类型：ACK 转人工后的“已收到”回执（不算正式回复）；空 = 普通消息")
    platform_read: Mapped[bool | None] = mapped_column(
        Boolean, comment="入库时 eBay 侧是否已读（readStatus）；之后的变化以会话的 platform_unread_count 为准")


class MessageTag(Base):
    __tablename__ = "message_tags"
    __table_args__ = {"comment": "会话标记的可选项，如 查适配中、不适配、等买家回复；所有账号都能在标记弹窗里新建、删除，管理员可改名、排序、停用"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    name: Mapped[str] = mapped_column(String(32), unique=True, comment="标记名称")
    color: Mapped[str] = mapped_column(String(16), default="primary",
                                       comment="颜色：#rrggbb 任意颜色 / none 无色（早期的 primary 蓝、success 绿、warning 橙、danger 红、info 灰也认）")
    sort: Mapped[int] = mapped_column(Integer, default=100, comment="排序，小的在前")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否可选；停用后已有标记照常显示，不能再选")
    created_by: Mapped[str | None] = mapped_column(String(64), comment="创建人")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")


class ConversationMark(Base):
    __tablename__ = "conversation_marks"
    __table_args__ = (UniqueConstraint("conversation_id", "tag_id", name="uq_conversation_marks_conv_tag"),
                      {"comment": "会话标记：给整段会话标当前状态（如 查适配中、不适配）并写备注；一个会话可以打多个标记，同一标记只打一次"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True, comment="被标记的会话 ID")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="所属店铺 ID")
    tag_id: Mapped[int] = mapped_column(ForeignKey("message_tags.id"), index=True, comment="标记项 ID")
    note: Mapped[str | None] = mapped_column(Text, comment="备注")
    marked_by: Mapped[str] = mapped_column(String(64), comment="标记人（显示名）")
    marked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="最近标记/修改时间（UTC）")


class AttachmentStatus:
    PENDING = "PENDING"  # 待下载（eBay 附件在 Agent 运行时按需下载）
    STORED = "STORED"  # 已校验、去除元数据并重新编码保存
    SKIPPED = "SKIPPED"  # 不处理：非图片、超出数量、来源域名不在白名单等
    FAILED = "FAILED"  # 下载或校验失败
    EXPIRED = "EXPIRED"  # 超过保留期，文件已删除


class MessageAttachment(Base):
    __tablename__ = "message_attachments"
    __table_args__ = (UniqueConstraint("message_id", "seq"),
                      {"comment": "消息附件：eBay messageMedia 或模拟器上传的图片；只保存校验后去除 EXIF、重新编码的副本（第 20 章）"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="附件主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="所属店铺 ID")
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True, comment="所属会话 ID")
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True, comment="所属消息 ID（messages.id）")
    seq: Mapped[int] = mapped_column(Integer, comment="消息内附件序号，从 1 开始")
    media_name: Mapped[str | None] = mapped_column(String(300), comment="原始附件名")
    media_type: Mapped[str] = mapped_column(String(16), comment="附件类型：IMAGE / PDF / DOC / TXT（eBay 取值）")
    source: Mapped[str] = mapped_column(String(16), comment="来源：ebay 平台附件 / simulator 模拟器上传")
    source_url: Mapped[str | None] = mapped_column(
        String(1000), comment="eBay 附件地址；只从这里下载（须 HTTPS 且域名在白名单内），不打开买家正文里的链接")
    status: Mapped[str] = mapped_column(
        String(16), default=AttachmentStatus.PENDING,
        comment="PENDING 待下载 / STORED 已保存 / SKIPPED 不处理 / FAILED 下载或校验失败 / EXPIRED 已过保留期删除")
    status_reason: Mapped[str | None] = mapped_column(String(300), comment="SKIPPED / FAILED 的原因")
    content_type: Mapped[str | None] = mapped_column(String(64), comment="保存副本的 MIME 类型（重新编码后）")
    size_bytes: Mapped[int | None] = mapped_column(Integer, comment="保存副本的字节数")
    width: Mapped[int | None] = mapped_column(Integer, comment="保存副本的宽度（像素）")
    height: Mapped[int | None] = mapped_column(Integer, comment="保存副本的高度（像素）")
    sha256: Mapped[str | None] = mapped_column(String(64), index=True, comment="保存副本的 SHA-256，用于识别结果缓存")
    local_path: Mapped[str | None] = mapped_column(String(500), comment="相对 MEDIA_DIR 的保存路径")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="登记时间（UTC）")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, comment="保留到期时间（UTC），到期删除文件（第 18.2 节）")


class ImageObservation(Base):
    __tablename__ = "image_observations"
    __table_args__ = {"comment": "图片观察：视觉模型对附件的客观描述（只观察不下结论），审核员可确认或纠正"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="观察记录主键")
    attachment_id: Mapped[int] = mapped_column(ForeignKey("message_attachments.id"), index=True, comment="对应附件 ID")
    sha256: Mapped[str] = mapped_column(String(64), index=True, comment="图片副本的 SHA-256（同图同配置复用结果）")
    model_config_version: Mapped[str] = mapped_column(String(32), comment="视觉模型配置版本（不含密钥的配置哈希）")
    prompt_version: Mapped[str] = mapped_column(String(32), comment="观察提示词版本（内容哈希）")
    status: Mapped[str] = mapped_column(String(16), comment="OK 成功 / FAILED 失败")
    observation: Mapped[dict | None] = mapped_column(JSON, comment="结构化观察结果（ImageObservationOutput），已二次脱敏")
    error: Mapped[str | None] = mapped_column(String(500), comment="失败原因")
    usage: Mapped[dict | None] = mapped_column(JSON, comment="token 用量")
    cached_from_id: Mapped[int | None] = mapped_column(Integer, comment="复用了哪条观察记录的结果（同一图片哈希）；空表示本次实际调用")
    review_status: Mapped[str] = mapped_column(String(16), default="UNREVIEWED",
                                               comment="审核：UNREVIEWED 未审核 / CONFIRMED 确认无误 / CORRECTED 已纠正")
    review_note: Mapped[str | None] = mapped_column(Text, comment="审核员纠正说明（正确的内容）")
    reviewed_by: Mapped[str | None] = mapped_column(String(64), comment="审核员")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, comment="审核时间（UTC）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")


class ConversationEvent(Base):
    __tablename__ = "conversation_events"
    __table_args__ = {"comment": "会话事件：eBay 系统消息、售后 case 等转成的事件，用于触发人工接管和风险升级"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="事件主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="所属店铺 ID")
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True, comment="所属会话 ID")
    event_type: Mapped[str] = mapped_column(String(64), comment="事件类型，如 RETURN_OPENED、CASE_ESCALATED")
    source: Mapped[str] = mapped_column(String(64), comment="事件来源，如 ebay_system_message、post_order_api")
    case_id: Mapped[str | None] = mapped_column(String(64), comment="关联的售后 case ID")
    detail: Mapped[str | None] = mapped_column(Text, comment="事件内容摘要")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="发生时间（UTC）")


class ReplyJob(Base):
    __tablename__ = "reply_jobs"
    __table_args__ = (UniqueConstraint("store_id", "conversation_id", "anchor_source_message_id"),
                      {"comment": "回复任务：对一组买家消息做一次回复决策的单位；合并窗口内的连续来信合并为一个任务"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="任务主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="所属店铺 ID")
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True, comment="所属会话 ID")
    anchor_source_message_id: Mapped[str] = mapped_column(String(128), comment="锚点消息：本任务覆盖的最后一条买家消息的平台 ID")
    state: Mapped[str] = mapped_column(String(32), default=JobState.RECEIVED, index=True, comment=f"任务状态：{JOB_STATE_DOC}")
    ready_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="可开始处理的时间（合并窗口结束或重试退避结束，UTC）")
    run_generation: Mapped[int] = mapped_column(Integer, default=1, comment="生成代次：重新生成时 +1，用于区分草稿与运行记录")
    attempt: Mapped[int] = mapped_column(Integer, default=0, comment="当前代次已尝试运行的次数")
    risk_level: Mapped[str | None] = mapped_column(String(4), comment="程序判定的风险等级 L0–L3（第 3.2 节）")
    decision_reasons: Mapped[list | None] = mapped_column(JSON, comment="进入审核/不能自动发送的原因码列表（见 guards/risk.py）")
    rule_version: Mapped[str | None] = mapped_column(String(64), comment="判定所用的规则版本")
    model_version: Mapped[str | None] = mapped_column(String(64), comment="判定所用的模型配置版本（不含密钥）")
    last_error: Mapped[str | None] = mapped_column(Text, comment="最近一次运行错误")
    ack_status: Mapped[str | None] = mapped_column(
        String(64), comment="转人工后给买家的“已收到”回执：SENT 已发；其他为不发的原因；空 = 还没到时间或还没处理（见 review_ack.py）")
    ack_sent_at: Mapped[datetime | None] = mapped_column(DateTime, comment="回执发出时间（UTC）")
    ack_message_id: Mapped[int | None] = mapped_column(Integer, comment="回执消息 ID（messages.id）")
    lock_version: Mapped[int] = mapped_column(Integer, default=1, comment="乐观锁版本号：状态变更时 +1，防止并发覆盖")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, comment="最后更新时间（UTC）")


class ReplyJobSource(Base):
    __tablename__ = "reply_job_sources"
    __table_args__ = (UniqueConstraint("store_id", "source_message_id"),
                      {"comment": "任务覆盖的买家消息：一条买家消息同一时刻只归属一个回复任务"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), comment="所属店铺 ID")
    source_message_id: Mapped[str] = mapped_column(String(128), comment="被覆盖的买家消息平台 ID")
    job_id: Mapped[int] = mapped_column(ForeignKey("reply_jobs.id"), index=True, comment="当前归属的回复任务 ID")
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="归属时间（UTC）")


class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (UniqueConstraint("job_id", "run_generation", "version"),
                      {"comment": "回复草稿：AI 生成或审核员撰写，记录依据、合规结果和所用版本，批准时绑定会话版本"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="草稿主键")
    job_id: Mapped[int] = mapped_column(ForeignKey("reply_jobs.id"), index=True, comment="所属回复任务 ID")
    run_generation: Mapped[int] = mapped_column(Integer, comment="生成代次")
    version: Mapped[int] = mapped_column(Integer, default=1, comment="任务内草稿版本号，从 1 递增")
    author: Mapped[str] = mapped_column(String(64), default="agent", comment="作者：agent 表示 AI 生成，否则为审核员名")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE",
                                        comment="草稿状态：ACTIVE 当前有效 / SUPERSEDED 已被替代 / APPROVED 已批准 / REJECTED 已拒绝")
    conversation_row_version: Mapped[int] = mapped_column(Integer, comment="生成时的会话版本号；与当前不一致即为过期草稿")
    text: Mapped[str] = mapped_column(Text, default="", comment="发给买家的回复正文（买家语言）")
    text_zh: Mapped[str | None] = mapped_column(Text, comment="正文的中文回译，仅供审核员理解")
    reasoning_zh: Mapped[str | None] = mapped_column(Text, comment="模型自述的中文回复思路，仅供审核员参考，不发给买家")
    language: Mapped[str | None] = mapped_column(String(16), comment="回复语言代码，如 en、de、zh")
    intent: Mapped[str | None] = mapped_column(String(32), comment="主要意图编码（第 3.1 节）")
    intents: Mapped[list | None] = mapped_column(JSON, comment="识别出的全部意图编码列表")
    action: Mapped[str | None] = mapped_column(String(16), comment="模型建议动作：answer 回答 / clarify 追问 / handoff 转人工")
    risk_level: Mapped[str | None] = mapped_column(String(4), comment="程序判定的风险等级 L0–L3")
    decision: Mapped[str | None] = mapped_column(String(32), comment="闸门结论：AUTO_ELIGIBLE 自动候选 / REVIEW_REQUIRED 需人工审核")
    claims: Mapped[list | None] = mapped_column(JSON, comment="关键陈述及其证据 ID 列表")
    evidence_ids: Mapped[list | None] = mapped_column(JSON, comment="草稿引用的全部证据 ID（工具结果 / 知识条款）")
    missing_facts: Mapped[list | None] = mapped_column(JSON, comment="回答所缺的事实")
    conflicts: Mapped[list | None] = mapped_column(JSON, comment="相互冲突的证据 ID")
    review_reasons: Mapped[list | None] = mapped_column(JSON, comment="需要人工审核的原因码")
    compliance_result: Mapped[dict | None] = mapped_column(JSON, comment="平台合规检查结果：PASS/REVIEW/BLOCK 及命中位置")
    compliance_rules_version: Mapped[str | None] = mapped_column(String(64), comment="合规规则集版本")
    prompt_version: Mapped[str | None] = mapped_column(String(64), comment="提示词版本（内容哈希）")
    model_config_version: Mapped[str | None] = mapped_column(String(64), comment="模型配置版本（不含密钥）")
    knowledge_versions: Mapped[list | None] = mapped_column(JSON, comment="本次注入的知识条款 ID@版本 列表")
    token_usage: Mapped[dict | None] = mapped_column(JSON, comment="模型 token 用量（输入/输出/合计）")
    intent_confidence: Mapped[str | None] = mapped_column(
        String(8), comment="模型对意图的把握度：high 明确 / medium 结合上下文判断 / low 看不出指哪件事（转 clarify 并加审核原因 INTENT_UNCERTAIN）")
    intent_basis: Mapped[list | None] = mapped_column(JSON, comment="模型判断意图依据的消息 ID 列表（eBay 消息 ID，已过滤掉本次上下文里没有的）")
    inherits_issue: Mapped[str | None] = mapped_column(
        String(80), comment="本次沿用的会话状态卡里的未解决问题 ID（如 sys:ticket:CJ123、m:delivery_delay:12-34567-89012）；空 = 新话题")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")


class EbayOrder(Base):
    __tablename__ = "ebay_orders"
    __table_args__ = (UniqueConstraint("store_id", "order_id"),
                      {"comment": "客户管理用的订单汇总（只读拉自 eBay getOrders）：只存买家账号、国家、金额、状态、商品，不存姓名地址电话"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    order_id: Mapped[str] = mapped_column(String(64), comment="eBay 订单号")
    buyer: Mapped[str | None] = mapped_column(String(128), index=True, comment="买家 eBay 用户名（小写）")
    country: Mapped[str | None] = mapped_column(String(8), comment="收货国家代码")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, comment="下单时间（UTC）")
    ebay_updated_at: Mapped[datetime | None] = mapped_column(DateTime, comment="eBay 上的最后修改时间（UTC）")
    total: Mapped[float | None] = mapped_column(Float, comment="订单总额")
    currency: Mapped[str | None] = mapped_column(String(8), comment="币种")
    status: Mapped[str | None] = mapped_column(String(32), comment="发货状态：NOT_STARTED / IN_PROGRESS / FULFILLED")
    payment_status: Mapped[str | None] = mapped_column(String(32), comment="付款状态")
    refunded: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否有退款")
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否已取消")
    item_id: Mapped[str | None] = mapped_column(String(32), comment="第一件商品的商品号")
    item_title: Mapped[str | None] = mapped_column(String(300), comment="第一件商品标题")
    items: Mapped[int | None] = mapped_column(Integer, comment="商品件数")
    skus: Mapped[str | None] = mapped_column(String(500), comment="订单里各商品的 SKU（eBay 上的自定义标签，逗号分隔），工作台按 SKU 筛选用")
    item_ids: Mapped[str | None] = mapped_column(String(300), comment="订单里各商品的商品号（逗号分隔）")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最近一次拉取时间（UTC）")


class EbayOrderLine(Base):
    __tablename__ = "ebay_order_lines"
    __table_args__ = (UniqueConstraint("store_id", "order_id", "line_item_id"),
                      {"comment": "订单里的每件商品（随订单同步每小时更新）：售后管理列表的 SKU、数量、单价从这里读，不再实时查 eBay"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    order_id: Mapped[str] = mapped_column(String(64), index=True, comment="eBay 订单号（新格式，退货 / 退款 / 取消单用它）")
    legacy_order_id: Mapped[str | None] = mapped_column(String(64), comment="旧格式订单号")
    line_item_id: Mapped[str] = mapped_column(String(64), comment="订单行号（= Trading 的交易号 TransactionID）")
    line_key: Mapped[str | None] = mapped_column(String(100), index=True,
                                                 comment="商品号-交易号：咨询、纠纷、评价里的订单号就是这个格式")
    item_id: Mapped[str | None] = mapped_column(String(32), index=True, comment="商品号")
    buyer: Mapped[str | None] = mapped_column(String(128), index=True, comment="买家 eBay 用户名（小写）")
    sku: Mapped[str | None] = mapped_column(String(100), comment="SKU（eBay 上的自定义标签）")
    title: Mapped[str | None] = mapped_column(String(300), comment="商品标题")
    quantity: Mapped[int | None] = mapped_column(Integer, comment="数量")
    price_value: Mapped[str | None] = mapped_column(String(32), comment="这一行的金额（lineItemCost）")
    price_currency: Mapped[str | None] = mapped_column(String(8), comment="币种")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, comment="下单时间（UTC）")


class EbayListingInfo(Base):
    __tablename__ = "ebay_listing_info"
    __table_args__ = (UniqueConstraint("store_id", "item_id"),
                      {"comment": "售后管理用的刊登信息（主图、标题、SKU）：售后同步时补拉新出现的商品，过期的重新拉"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    item_id: Mapped[str] = mapped_column(String(32), comment="商品号")
    title: Mapped[str | None] = mapped_column(String(300), comment="标题")
    sku: Mapped[str | None] = mapped_column(String(100), comment="SKU（多属性刊登为空）")
    picture_url: Mapped[str | None] = mapped_column(String(1000), comment="主图地址")
    found: Mapped[bool | None] = mapped_column(Boolean, comment="eBay 上查到了（False = 刊登不存在或不是本店的）")
    error: Mapped[str | None] = mapped_column(String(300), comment="最近一次拉取失败的原因")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, comment="最近一次拉取时间（UTC）")


class BestOffer(Base):
    __tablename__ = "best_offers"
    __table_args__ = (UniqueConstraint("store_id", "best_offer_id"),
                      {"comment": "Best offer：买家在刊登上出的报价（GetBestOffers 拉取）与卖家回应（接受 / 还价 / 拒绝）；不存买家邮箱地址"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    best_offer_id: Mapped[str] = mapped_column(String(64), comment="eBay 报价号")
    item_id: Mapped[str | None] = mapped_column(String(32), index=True, comment="商品号")
    title: Mapped[str | None] = mapped_column(String(300), comment="商品标题")
    picture_url: Mapped[str | None] = mapped_column(String(500), comment="商品主图")
    bin_price: Mapped[float | None] = mapped_column(Float, comment="一口价")
    currency: Mapped[str | None] = mapped_column(String(8), comment="币种")
    price: Mapped[float | None] = mapped_column(Float, comment="买家报价（单价）")
    quantity: Mapped[int | None] = mapped_column(Integer, comment="数量")
    buyer: Mapped[str | None] = mapped_column(String(128), index=True, comment="买家 eBay 用户名（小写）")
    buyer_feedback: Mapped[int | None] = mapped_column(Integer, comment="买家信用分")
    status: Mapped[str | None] = mapped_column(String(16), index=True, comment="Pending / Active 待处理；Accepted / Declined / Countered / Expired / Gone")
    offer_type: Mapped[str | None] = mapped_column(String(32), comment="BuyerBestOffer 买家报价 / BuyerCounterOffer 买家还价 / SellerCounterOffer 我方还价")
    buyer_message: Mapped[str | None] = mapped_column(Text, comment="买家留言")
    seller_message: Mapped[str | None] = mapped_column(Text, comment="卖家留言")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, comment="报价过期时间（UTC）")
    item_end_at: Mapped[datetime | None] = mapped_column(DateTime, comment="刊登结束时间（UTC）")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="第一次拉到（UTC）")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最近一次拉到（UTC）")
    responded_action: Mapped[str | None] = mapped_column(String(16), comment="本系统里的回应：Accept / Counter / Decline")
    responded_by: Mapped[str | None] = mapped_column(String(64), comment="回应人")
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, comment="回应时间（UTC）")
    counter_price: Mapped[float | None] = mapped_column(Float, comment="我方还价")
    response_message: Mapped[str | None] = mapped_column(String(300), comment="回应时给买家的留言")
    response_error: Mapped[str | None] = mapped_column(String(500), comment="回应失败原因")


class OrderNote(Base):
    __tablename__ = "order_notes"
    __table_args__ = {"comment": "客服内部备注：记在订单上（或只记在买家上），会话右侧“相关信息”里所有同事可见，不发给买家"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    order_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="订单号；空 = 记在买家上")
    buyer: Mapped[str] = mapped_column(String(128), index=True, comment="买家 eBay 用户名（小写）")
    text: Mapped[str] = mapped_column(Text, comment="备注内容")
    created_by: Mapped[str | None] = mapped_column(String(64), comment="写备注的人")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="时间（UTC）")


class BuyerBlacklist(Base):
    __tablename__ = "buyer_blacklist"
    __table_args__ = {"comment": "系统内黑名单：这些买家来信 AI 不起草回复、直接交人工，界面标红（eBay 屏蔽名单需在各店铺后台手动加）"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    buyer: Mapped[str] = mapped_column(String(128), index=True, comment="买家 eBay 用户名（小写）")
    store_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="只对这个店铺生效；空 = 全部店铺")
    reason: Mapped[str | None] = mapped_column(String(500), comment="原因")
    created_by: Mapped[str | None] = mapped_column(String(64), comment="加入的人")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="加入时间（UTC）")


class EbayCase(Base):
    __tablename__ = "ebay_cases"
    __table_args__ = (UniqueConstraint("store_id", "kind", "external_id"),
                      {"comment": "售后管理：从 eBay 拉来的咨询（未收到货）、纠纷、退货、付款纠纷、评价、取消交易、退款单（只读，定时更新）"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    kind: Mapped[str] = mapped_column(String(16), index=True, comment="类型：INQUIRY 咨询 / CASE 纠纷 / RETURN 退货 / PAYMENT_DISPUTE 付款纠纷 / "
                                                                    "FEEDBACK 评价 / CANCEL 取消交易 / REFUND 退款单")
    external_id: Mapped[str] = mapped_column(String(64), comment="eBay 上的编号（退货号、纠纷号、评价 ID、交易 ID 等）")
    order_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="订单号（咨询、纠纷为 商品号-交易号）")
    item_id: Mapped[str | None] = mapped_column(String(32), comment="商品号")
    buyer: Mapped[str | None] = mapped_column(String(128), index=True, comment="买家 eBay 用户名")
    status: Mapped[str | None] = mapped_column(String(64), comment="eBay 上的状态（原样）")
    is_open: Mapped[bool] = mapped_column(Boolean, default=False, index=True, comment="是否还需要处理（未关闭；评价为差评/中评且没回复）")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, comment="开始时间（UTC）")
    due_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, comment="卖家最晚处理时间（UTC，eBay 的 respond by）")
    amount: Mapped[float | None] = mapped_column(Float, comment="金额（退款、索赔、取消退款等）")
    currency: Mapped[str | None] = mapped_column(String(8), comment="币种")
    reason: Mapped[str | None] = mapped_column(String(128), comment="原因")
    title: Mapped[str | None] = mapped_column(String(300), comment="商品标题")
    comment: Mapped[str | None] = mapped_column(Text, comment="买家留言或评价内容")
    rating: Mapped[str | None] = mapped_column(String(16), comment="评价类型：Positive / Neutral / Negative")
    detail: Mapped[dict | None] = mapped_column(JSON, comment="其他信息（退货类型、eBay 处理链接等）")
    ebay_updated_at: Mapped[datetime | None] = mapped_column(DateTime, comment="eBay 上的最后更新时间（UTC）")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="第一次拉到的时间（UTC）")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最近一次拉到的时间（UTC）")
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime, comment="状态最近变化时间（UTC）")
    seen_by: Mapped[str | None] = mapped_column(String(64), comment="看过的客服（空 = 新的或状态有变化）")
    seen_at: Mapped[datetime | None] = mapped_column(DateTime, comment="标记已看时间（UTC）")
    # ---- 本系统里的处理记录（只在本系统，不写回 eBay；拉取更新时不覆盖） ----
    work_state: Mapped[str | None] = mapped_column(
        String(16), comment="本系统处理状态：PROCESSING 处理中 / DONE 已处理 / HOLD 暂不处理；空 = 按 eBay 是否关闭（开着=待处理）")
    hold_reason: Mapped[str | None] = mapped_column(String(64), comment="暂不处理原因（退货等）")
    starred: Mapped[bool | None] = mapped_column(Boolean, comment="星标（同事共用）")
    tag_ids: Mapped[list | None] = mapped_column(JSON, comment="打上的标签（ebay_case_tags.id 列表）")
    note: Mapped[str | None] = mapped_column(Text, comment="内部备注（同事共用）")
    note_by: Mapped[str | None] = mapped_column(String(64), comment="最后写备注的人")
    note_at: Mapped[datetime | None] = mapped_column(DateTime, comment="备注更新时间（UTC）")
    operator: Mapped[str | None] = mapped_column(String(64), comment="操作员（认领的客服）")
    operator_at: Mapped[datetime | None] = mapped_column(DateTime, comment="认领时间（UTC）")
    worked_by: Mapped[str | None] = mapped_column(String(64), comment="最后改处理状态的人")
    worked_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后改处理状态的时间（UTC）")
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime, comment="在本系统删除（隐藏）的时间；eBay 上不受影响，再拉取也不会恢复")
    hidden_by: Mapped[str | None] = mapped_column(String(64), comment="删除（隐藏）的人")


class EbayCaseTag(Base):
    __tablename__ = "ebay_case_tags"
    __table_args__ = {"comment": "售后管理的快捷标签（按类型分：咨询、纠纷、退货、付款纠纷、评价各自一套）"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    kind: Mapped[str] = mapped_column(String(16), index=True, comment="适用的类型：INQUIRY / CASE / RETURN / PAYMENT_DISPUTE / FEEDBACK …")
    name: Mapped[str] = mapped_column(String(32), comment="标签名，如 不退回、已询问VIN")
    color: Mapped[str] = mapped_column(String(16), default="primary", comment="颜色：primary / success / warning / danger / info")
    sort: Mapped[int] = mapped_column(Integer, default=0, comment="排序")
    created_by: Mapped[str | None] = mapped_column(String(64), comment="创建人")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    __table_args__ = {"comment": "消息模板：客服回复买家、回复评价用的常用话术，可带 {{buyer}} 等占位符和图片附件"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    scope: Mapped[str] = mapped_column(String(16), index=True,
                                       comment="类型：SYSTEM 系统预定义（管理员维护）/ COMPANY 企业模板（全员可见）/ PERSONAL 个人模板（只有创建人可见）")
    usage: Mapped[str] = mapped_column(String(16), default="MESSAGE", comment="用途：MESSAGE 消息 / FEEDBACK 评价")
    title: Mapped[str] = mapped_column(String(200), comment="标题")
    content: Mapped[str] = mapped_column(Text, comment="内容（最多 1800 字），可含 {{buyer}}、{{orderNumber}} 等占位符")
    tags: Mapped[list | None] = mapped_column(JSON, comment="标签（字符串列表）")
    attachments: Mapped[list | None] = mapped_column(JSON, comment="图片附件 [{file, name}]，文件存在 data/media/templates")
    starred: Mapped[bool | None] = mapped_column(Boolean, comment="星标（同事共用）")
    owner_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="创建人账号 ID（个人模板按它判断可见）")
    created_by: Mapped[str | None] = mapped_column(String(64), comment="创建人显示名（系统预定义为“系统”）")
    updated_by: Mapped[str | None] = mapped_column(String(64), comment="最后修改人")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, comment="最后修改时间（UTC）")


class OfferRecord(Base):
    __tablename__ = "offer_records"
    __table_args__ = {"comment": "议价：给“感兴趣的买家”（关注 / 加购了商品的买家）发的折扣报价记录（Negotiation API，一次一个商品）"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    marketplace_id: Mapped[str] = mapped_column(String(32), comment="站点，如 EBAY_DE（必须和刊登站点一致）")
    listing_id: Mapped[str] = mapped_column(String(32), index=True, comment="刊登号")
    discount_percentage: Mapped[str | None] = mapped_column(String(8), comment="折扣百分比（和 price 二选一）")
    price_value: Mapped[str | None] = mapped_column(String(32), comment="报价金额（和折扣二选一）")
    price_currency: Mapped[str | None] = mapped_column(String(8), comment="报价币种")
    quantity: Mapped[int] = mapped_column(Integer, default=1, comment="数量")
    duration_days: Mapped[int] = mapped_column(Integer, default=2, comment="报价有效天数")
    allow_counter: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否允许买家还价")
    message: Mapped[str | None] = mapped_column(Text, comment="给买家的留言")
    status: Mapped[str] = mapped_column(String(16), index=True, comment="SENT 已发出 / FAILED 没发出")
    offers_count: Mapped[int | None] = mapped_column(Integer, comment="eBay 返回的报价条数（发给了几位买家）")
    response: Mapped[dict | None] = mapped_column(JSON, comment="eBay 返回内容（报价 ID、状态、买家掩码名等）")
    error: Mapped[str | None] = mapped_column(Text, comment="失败原因")
    sent_by: Mapped[str] = mapped_column(String(64), comment="操作人")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, comment="发送时间（UTC）")


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    __table_args__ = {"comment": "待发消息（发件箱）：所有发给买家的消息先进这里，延迟 SEND_DELAY_SECONDS（默认 5 分钟）后由 Worker 发出，发出前可撤回"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="所属店铺 ID")
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True, comment="所属会话 ID")
    body: Mapped[str] = mapped_column(Text, comment="发给买家的正文")
    zh_translation: Mapped[str | None] = mapped_column(Text, comment="正文的中文对照（只给审核员看）")
    source: Mapped[str] = mapped_column(String(32), comment="来源：manual 人工 / auto AI 自动回复 / ack 已收到回执 / ticket 查件结果 / "
                                                            "logistics_notice 查件提交提醒 / ship_notice 发货通知 / invoice_notice 发票提醒")
    actor: Mapped[str] = mapped_column(String(64), comment="提交人（客服名或系统）")
    job_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="关联的回复任务（有的话）")
    state: Mapped[str] = mapped_column(String(16), default="PENDING", index=True,
                                       comment="PENDING 等待发送（可撤回）/ SENDING 发送中 / SENT 已发出 / RECALLED 已撤回 / "
                                               "FAILED 明确没发出 / UNCERTAIN 结果不确定（不自动重发，需核对）")
    send_after: Mapped[datetime] = mapped_column(DateTime, index=True, comment="最早发送时间（UTC）：之前都可以撤回")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="提交时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, comment="最后更新时间（UTC）")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, comment="实际发出时间（UTC）")
    channel: Mapped[str | None] = mapped_column(String(16), comment="发送渠道：ebay 真实发到 eBay / mock 只记在本地")
    platform_message_id: Mapped[str | None] = mapped_column(String(128), comment="eBay 返回的消息 ID（模拟发送为 SIM-OUT- 开头）")
    message_id: Mapped[int | None] = mapped_column(Integer, comment="发出后写进会话的消息 ID（messages.id）")
    error: Mapped[str | None] = mapped_column(Text, comment="发送失败或不确定的原因")
    recalled_by: Mapped[str | None] = mapped_column(String(64), comment="撤回人")
    recalled_at: Mapped[datetime | None] = mapped_column(DateTime, comment="撤回时间（UTC）")
    meta: Mapped[dict | None] = mapped_column(JSON, comment="来源相关信息（查件单 ID、发货通知 ID、附件等），发出或撤回时回写来源状态用")


class SendIntent(Base):
    __tablename__ = "send_intents"
    __table_args__ = {"comment": "发送意图：每个回复任务唯一的外发请求，人工与自动发送共用；记录平台回执"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="发送意图主键")
    job_id: Mapped[int] = mapped_column(ForeignKey("reply_jobs.id"), unique=True, comment="回复任务 ID（唯一：一个任务最多一个外发意图）")
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"), comment="被批准的草稿 ID")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, comment="幂等键：重复提交返回同一意图")
    actor: Mapped[str] = mapped_column(String(64), comment="提交人：审核员名，自动发送时为系统")
    text: Mapped[str] = mapped_column(Text, comment="最终外发正文")
    content_hash: Mapped[str] = mapped_column(String(64), comment="外发正文的 SHA-256")
    state: Mapped[str] = mapped_column(String(32), default="QUEUED",
                                       comment="发送状态：QUEUED 排队 / SENDING 发送中 / SENT 已发送 / SEND_UNCERTAIN 不确定 / FAILED 明确未发出")
    platform_message_id: Mapped[str | None] = mapped_column(String(128), comment="平台返回的消息 ID")
    last_error: Mapped[str | None] = mapped_column(Text, comment="最近一次发送错误")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, comment="平台确认发送时间（UTC）")


class SendAttempt(Base):
    __tablename__ = "send_attempts"
    __table_args__ = (UniqueConstraint("intent_id", "attempt_no"),
                      {"comment": "发送尝试：每次调用平台发送接口的记录，超时与对账证据不被后续请求覆盖"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    intent_id: Mapped[int] = mapped_column(ForeignKey("send_intents.id"), index=True, comment="发送意图 ID")
    attempt_no: Mapped[int] = mapped_column(Integer, comment="第几次尝试，从 1 开始")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="开始时间（UTC）")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, comment="结束时间（UTC）")
    response_status: Mapped[str | None] = mapped_column(String(64), comment="平台响应状态，如 HTTP 状态码")
    outcome: Mapped[str | None] = mapped_column(String(32), comment="结果：SENT 已发送 / NOT_SENT 明确未发出 / UNCERTAIN 不确定")


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = {"comment": "工具调用记录：Agent 预取/查询业务数据的每次调用，供审核核对与回放"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    job_id: Mapped[int] = mapped_column(ForeignKey("reply_jobs.id"), index=True, comment="所属回复任务 ID")
    run_generation: Mapped[int] = mapped_column(Integer, comment="生成代次")
    request_id: Mapped[str] = mapped_column(String(64), comment="本次运行内的请求编号，如 step-1")
    tool_name: Mapped[str] = mapped_column(String(64), comment="工具名，如 get_listing_context")
    tool_version: Mapped[str] = mapped_column(String(32), comment="工具版本")
    arguments_redacted: Mapped[dict | None] = mapped_column(JSON, comment="调用参数（已脱敏）")
    result_status: Mapped[str] = mapped_column(String(16), comment="结果状态：FOUND / NOT_FOUND / AMBIGUOUS / STALE / ERROR")
    result_data: Mapped[dict | list | None] = mapped_column(JSON, comment="返回数据与说明，供工作台“事实与依据”展示")
    source: Mapped[str | None] = mapped_column(String(64), comment="数据源名称，如 demo_fixture、business_mysql")
    evidence_ref: Mapped[str | None] = mapped_column(String(200), comment="证据 ID，草稿 claims 引用它")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, comment="耗时（毫秒）")
    queried_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="查询时间（UTC）")


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("job_id", "run_generation"),
                      {"comment": "Agent 运行记录：每个任务每个生成代次一条，关联提示词、模型和规则版本"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="运行主键")
    job_id: Mapped[int] = mapped_column(ForeignKey("reply_jobs.id"), index=True, comment="回复任务 ID")
    run_generation: Mapped[int] = mapped_column(Integer, comment="生成代次")
    thread_id: Mapped[str] = mapped_column(String(200), comment="运行标识 store:…:conversation:…:job:…:generation:…")
    mode: Mapped[str] = mapped_column(String(32), comment="运行模式：fixed_pipeline 固定流程 / planner_loop 规划循环")
    state: Mapped[str] = mapped_column(String(32), default="RUNNING", comment="运行状态：RUNNING / DONE / ERROR")
    prompt_version: Mapped[str | None] = mapped_column(String(64), comment="提示词版本")
    model_config_version: Mapped[str | None] = mapped_column(String(64), comment="模型配置版本")
    rule_version: Mapped[str | None] = mapped_column(String(64), comment="规则版本")
    error: Mapped[str | None] = mapped_column(Text, comment="运行错误信息")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="开始时间（UTC）")
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, comment="编排截止时间（UTC）")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, comment="结束时间（UTC）")


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = {"comment": "Agent 步骤：运行中每个节点的结果摘要，不保存模型思维过程"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True, comment="所属运行 ID")
    node: Mapped[str] = mapped_column(String(64), comment="节点名，如 prefetch、compose_reply")
    result_status: Mapped[str] = mapped_column(String(32), comment="节点结果，如 OK、SKIPPED、PARSE_ERROR")
    detail: Mapped[dict | None] = mapped_column(JSON, comment="节点结果摘要")
    usage: Mapped[dict | None] = mapped_column(JSON, comment="该节点的模型 token 用量")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, comment="耗时（毫秒）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="记录时间（UTC）")


class KnowledgeChunk(Base):
    """公司知识条款。首期把 knowledge_documents/knowledge_chunks 合成一张表，按条款存储。"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        # 起草取知识只查当前有效版本（PUBLISHED）：部分索引，已有的库由 vector_index.ensure_published_index 启动时补建
        Index("ix_knowledge_chunks_published", "store_id", "marketplace_id", "audience",
              postgresql_where=sql_text("review_state = 'PUBLISHED'"), sqlite_where=sql_text("review_state = 'PUBLISHED'")),
        {"comment": "公司知识条款：政策/FAQ/SOP/话术，按条款存储，带适用范围、可见级别、版本和生效期"})

    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="条款片段 ID（主键）")
    clause_id: Mapped[str] = mapped_column(String(64), comment="条款编号，如 SHP-US-001")
    document_id: Mapped[str] = mapped_column(String(64), index=True, comment="所属文档 ID，如 POL-SHP-US")
    version: Mapped[str] = mapped_column(String(32), comment="文档版本号")
    title: Mapped[str] = mapped_column(String(300), comment="条款标题")
    text: Mapped[str] = mapped_column(Text, comment="条款正文（含标题行）")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="适用店铺 ID；ALL 表示全部店铺")
    marketplace_id: Mapped[str] = mapped_column(String(32), comment="适用站点；ALL 表示全部站点")
    audience: Mapped[str] = mapped_column(String(16), comment="可见级别：PUBLIC_REPLY 可对买家转述 / INTERNAL_SOP 仅内部")
    language: Mapped[str] = mapped_column(String(16), comment="条款语言")
    topic: Mapped[str] = mapped_column(String(64), comment="主题，如 shipping、return、warranty")
    priority: Mapped[int] = mapped_column(Integer, comment="优先级：数值越小越优先，冲突时以小者为准")
    effective_at: Mapped[str] = mapped_column(String(10), comment="生效日期 YYYY-MM-DD")
    expires_at: Mapped[str | None] = mapped_column(String(10), comment="失效日期 YYYY-MM-DD；空表示长期有效")
    review_state: Mapped[str] = mapped_column(
        String(16), comment="状态：PUBLISHED 已发布（只有它会给 AI 用）/ DRAFT 未启用 / "
                            "RETIRED 已发布但被新版本替换下线 / WITHDRAWN 未启用且被替换下线；回滚时 RETIRED→PUBLISHED、WITHDRAWN→DRAFT")
    sku: Mapped[list | None] = mapped_column(JSON, comment="条款涉及的 SKU 列表")
    scenes: Mapped[list | None] = mapped_column(
        JSON, comment="适用场景（仅内部条款用）：会话属于这些场景时在会话页“内部参考”里显示，如 质量问题、退货退款；常驻 = 每个会话都显示")
    doc_refs: Mapped[list | None] = mapped_column(
        JSON, comment="引用的参考文档编号列表（kb_documents.doc_no，如 DOC-0001）；知识库页、会话页“内部参考”里可点开，AI 不读")
    source: Mapped[str | None] = mapped_column(String(64), comment="资料来源部门或渠道")
    content_hash: Mapped[str] = mapped_column(String(64), comment="正文 SHA-256，导入时据此判断是否变化")
    import_id: Mapped[int | None] = mapped_column(Integer, index=True,
                                                  comment="来自哪次 Excel 上传（knowledge_imports.id）；空表示由 JSONL 文件导入")
    search_text: Mapped[str | None] = mapped_column(
        Text, comment="全文检索用的分词结果（jieba 中文分词 + 英德文小写词，空格分隔），对它建 PostgreSQL 全文索引")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, comment="最后更新时间（UTC）")


class KbDocument(Base):
    __tablename__ = "kb_documents"
    __table_args__ = {"comment": "参考文档库：第三方文件和内部资料（报表、手册、供应商资料、物流手册），知识条款按编号引用；"
                                 "只给人看，AI 不读、不发给买家"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    doc_no: Mapped[str] = mapped_column(String(16), unique=True, comment="引用编号，如 DOC-0001；自动递增，删除后不复用")
    title: Mapped[str] = mapped_column(String(200), comment="标题")
    description: Mapped[str | None] = mapped_column(Text, comment="说明：这份文档是什么、什么时候用")
    category: Mapped[str] = mapped_column(String(32), default="其他", comment="分类：报表 / 手册 / 供应商资料 / 物流 / 其他")
    store_ids: Mapped[list | None] = mapped_column(JSON, comment="适用店铺 ID 列表；空表示全部店铺。非管理员只能看到自己负责店铺的文档")
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False,
                                            comment="敏感（如含买家个人信息）：只有管理员能下载，其他人只能看到标题")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE",
                                        comment="状态：ACTIVE 使用中 / ARCHIVED 已归档（列表默认不显示，已有引用仍可打开）")
    version: Mapped[int] = mapped_column(Integer, default=1, comment="当前版本号，替换时加 1")
    file_name: Mapped[str] = mapped_column(String(255), comment="当前版本的原始文件名")
    content_type: Mapped[str | None] = mapped_column(String(128), comment="当前版本的文件类型（MIME）")
    size: Mapped[int] = mapped_column(Integer, default=0, comment="当前版本的文件大小（字节）")
    sha256: Mapped[str] = mapped_column(String(64), comment="当前版本文件的 SHA-256")
    stored_path: Mapped[str] = mapped_column(String(500), comment="当前版本文件的存放路径（相对项目目录）")
    scan_status: Mapped[str | None] = mapped_column(String(16), comment="病毒扫描结果：CLEAN / SKIPPED 未扫描 / ERROR")
    uploaded_by: Mapped[str] = mapped_column(String(64), comment="上传人（第一次上传）")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="第一次上传时间（UTC）")
    updated_by: Mapped[str | None] = mapped_column(String(64), comment="最后修改人（替换版本、改信息、归档）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, comment="最后更新时间（UTC）")


class KbDocumentVersion(Base):
    __tablename__ = "kb_document_versions"
    __table_args__ = (UniqueConstraint("doc_id", "version"),
                      {"comment": "参考文档的历史版本：替换新版本时把旧版本记在这里，旧文件保留不删，可下载"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    doc_id: Mapped[int] = mapped_column(ForeignKey("kb_documents.id", ondelete="CASCADE"), index=True, comment="所属文档 ID")
    version: Mapped[int] = mapped_column(Integer, comment="版本号")
    file_name: Mapped[str] = mapped_column(String(255), comment="这个版本的原始文件名")
    content_type: Mapped[str | None] = mapped_column(String(128), comment="这个版本的文件类型（MIME）")
    size: Mapped[int] = mapped_column(Integer, default=0, comment="这个版本的文件大小（字节）")
    sha256: Mapped[str] = mapped_column(String(64), comment="这个版本文件的 SHA-256")
    stored_path: Mapped[str] = mapped_column(String(500), comment="这个版本文件的存放路径（相对项目目录）")
    uploaded_by: Mapped[str] = mapped_column(String(64), comment="这个版本的上传人")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="这个版本的上传时间（UTC）")


class EmbeddingProfile(Base):
    __tablename__ = "embedding_profiles"
    __table_args__ = (UniqueConstraint("provider", "model", "dimension", "distance", "normalize"),
                      {"comment": "向量模型档案：模型、维度、距离度量的组合；不同档案的向量不能混用（第 15.3 节）"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="档案 ID")
    provider: Mapped[str] = mapped_column(String(32), comment="向量模型服务商，如 openai_compatible")
    model: Mapped[str] = mapped_column(String(128), comment="向量模型 ID，如 qwen3.7-text-embedding")
    dimension: Mapped[int] = mapped_column(Integer, comment="向量维度（实际调用核对过）")
    distance: Mapped[str] = mapped_column(String(16), comment="距离度量：cosine")
    normalize: Mapped[bool] = mapped_column(Boolean, comment="应用层是否额外归一化")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="首次使用时间（UTC）")


class KnowledgeImport(Base):
    __tablename__ = "knowledge_imports"
    __table_args__ = {"comment": "知识库上传版本：每次在工作台上传 Excel 整体替换一个店铺的知识，记录一个版本，可回滚"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="版本号")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    file_name: Mapped[str] = mapped_column(String(255), comment="上传的文件名")
    file_sha256: Mapped[str] = mapped_column(String(64), comment="文件 SHA-256，用于确认预览与提交的是同一个文件")
    stored_path: Mapped[str | None] = mapped_column(String(500), comment="服务器上保存的原文件路径（data/knowledge_uploads/）")
    uploaded_by: Mapped[str] = mapped_column(String(64), comment="上传人（审核员名）")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="上传时间（UTC）")
    status: Mapped[str] = mapped_column(String(16),
                                        comment="版本状态：ACTIVE 当前生效 / SUPERSEDED 被新版本替换 / ROLLED_BACK 已回滚撤下")
    row_count: Mapped[int] = mapped_column(Integer, comment="表格中的有效条款行数")
    public_count: Mapped[int] = mapped_column(Integer, comment="可对买家说的条款数（按站点展开前）")
    internal_count: Mapped[int] = mapped_column(Integer, comment="仅内部的条款数")
    summary: Mapped[dict | None] = mapped_column(JSON, comment="校验摘要：各站点 token、提醒、与上一版本的差异")
    note: Mapped[str | None] = mapped_column(Text, comment="备注（如回滚原因）")


class WorkClaim(Base):
    __tablename__ = "work_claims"
    __table_args__ = (UniqueConstraint("store_id", "conversation_id"),
                      {"comment": "工作台认领锁：审核员打开会话即认领，过期前其他人只读，避免两人同时回复"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), comment="所属店铺 ID")
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), comment="被认领的会话 ID")
    claimed_by: Mapped[str] = mapped_column(String(64), comment="认领审核员")
    claimed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="认领或续期时间（UTC）")
    expires_at: Mapped[datetime] = mapped_column(DateTime, comment="到期时间（UTC），默认认领后 10 分钟")


class ReviewFeedback(Base):
    __tablename__ = "review_feedback"
    __table_args__ = {"comment": "审核反馈：批准、修改、拒绝、人工撰写的记录，是草稿采纳率等效率指标的来源"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id"), comment="相关草稿 ID（标记无需回复时为空）")
    job_id: Mapped[int] = mapped_column(ForeignKey("reply_jobs.id"), index=True, comment="回复任务 ID")
    reviewer: Mapped[str] = mapped_column(String(64), comment="审核员")
    action: Mapped[str] = mapped_column(String(16),
                                        comment="动作：APPROVE 原样批准 / EDIT_APPROVE 修改后批准 / REJECT 拒绝 / MANUAL 人工撰写 / NO_REPLY 无需回复")
    reason_codes: Mapped[list | None] = mapped_column(JSON, comment="修改或拒绝的结构化原因码")
    note: Mapped[str | None] = mapped_column(Text, comment="审核员补充说明")
    edit_ratio: Mapped[float | None] = mapped_column(Float, comment="修改幅度 0–1（相对 AI 草稿），人工撰写时为空")
    handling_ms: Mapped[int | None] = mapped_column(Integer, comment="审核耗时（毫秒）：打开会话到提交")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="记录时间（UTC）")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = {"comment": "审计日志：消息接入、审批、发送、开关变更等关键操作"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    actor: Mapped[str] = mapped_column(String(64), comment="操作者：审核员名或系统组件名")
    action: Mapped[str] = mapped_column(String(64), comment="操作类型，如 SEND_REQUESTED、HANDOFF_CHANGED")
    store_id: Mapped[str | None] = mapped_column(String(64), comment="相关店铺 ID")
    conversation_id: Mapped[int | None] = mapped_column(Integer, comment="相关会话 ID")
    detail: Mapped[dict | None] = mapped_column(JSON, comment="操作详情")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="发生时间（UTC）")


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = {"comment": "模型调用用量：每次调用大模型/视觉/向量模型记一行，供“用量统计”页汇总"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, comment="调用时间（UTC）")
    purpose: Mapped[str] = mapped_column(String(32), index=True,
                                         comment="用途：reply 回复生成 / repair 格式修正 / vision 识图 / translate 翻译 / "
                                                 "embedding_query 检索向量 / embedding_docs 知识向量化 / check 连通测试")
    provider: Mapped[str | None] = mapped_column(String(32), comment="通道：openai_compatible / codex_cli 等")
    model: Mapped[str | None] = mapped_column(String(128), comment="模型名")
    store_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="相关店铺 ID")
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="相关会话 ID")
    job_id: Mapped[int | None] = mapped_column(Integer, comment="相关回复任务 ID")
    input_tokens: Mapped[int | None] = mapped_column(Integer, comment="输入 token（服务商返回；Codex 不区分输入输出时为空）")
    output_tokens: Mapped[int | None] = mapped_column(Integer, comment="输出 token")
    total_tokens: Mapped[int | None] = mapped_column(Integer, comment="合计 token")
    estimated: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否为本地估算（向量模型不返回用量时按字数估算）")
    duration_ms: Mapped[int | None] = mapped_column(Integer, comment="耗时（毫秒）")
    ok: Mapped[bool] = mapped_column(Boolean, default=True, comment="调用是否成功")
    error: Mapped[str | None] = mapped_column(String(300), comment="失败原因摘要")


class EbaySyncState(Base):
    __tablename__ = "ebay_sync_state"
    __table_args__ = (UniqueConstraint("store_id", "conversation_type"),
                      {"comment": "eBay 消息同步游标：每个店铺、每种会话类型一行，记录上次成功拉取的时间与结果"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), comment="店铺 ID")
    conversation_type: Mapped[str] = mapped_column(String(16), comment="会话类型：FROM_MEMBERS 买家消息 / FROM_EBAY eBay 系统消息")
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最近一次开始同步的时间（UTC）")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最近一次成功同步的时间（UTC），下次从它往前重叠一段再拉")
    last_error: Mapped[str | None] = mapped_column(Text, comment="最近一次失败原因（不含令牌）")
    last_stats: Mapped[dict | None] = mapped_column(JSON, comment="最近一次同步统计：会话数、新消息数、接口调用数等")
    read_backfilled_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="已读状态初始化完成时间（UTC）：上线已读功能后第一次同步按保留期内 eBay 的未读数回填，之后为空才会再做")


class EbayListing(Base):
    __tablename__ = "ebay_listings"
    __table_args__ = {"comment": "eBay 刊登缓存：按刊登号从 Browse API 查到的基本信息，用于确定会话所属站点"}

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="eBay 刊登号（legacy item ID）")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="所属店铺 ID")
    lookup_status: Mapped[str] = mapped_column(String(16), comment="查询结果：FOUND 已查到 / NOT_FOUND 刊登不存在或已下架 / ERROR 查询失败")
    listing_marketplace_id: Mapped[str | None] = mapped_column(String(32), comment="eBay 返回的刊登站点原值，如 EBAY_DE、EBAY_MOTORS_US")
    marketplace_id: Mapped[str | None] = mapped_column(String(32), comment="归一后的站点：EBAY_MOTORS_US 归为 EBAY_US")
    title: Mapped[str | None] = mapped_column(String(300), comment="刊登标题")
    item_web_url: Mapped[str | None] = mapped_column(String(500), comment="刊登页面地址（域名随查询站点变化，不用于判断站点）")
    price_value: Mapped[str | None] = mapped_column(String(32), comment="价格数值（字符串原样保存）")
    price_currency: Mapped[str | None] = mapped_column(String(8), comment="价格币种，如 EUR、USD、GBP")
    last_error: Mapped[str | None] = mapped_column(Text, comment="最近一次查询错误")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="最近一次查询时间（UTC）；NOT_FOUND/ERROR 满 7 天后重查")


class EbayNotification(Base):
    __tablename__ = "ebay_notifications"
    __table_args__ = {"comment": "eBay 实时推送收件记录（Notification API，如 NEW_MESSAGE）：只用来触发对应店铺立即拉取，正文以拉取为准"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    notification_id: Mapped[str] = mapped_column(String(128), unique=True, comment="eBay 推送 ID，用于去重（eBay 失败会重发）")
    topic: Mapped[str | None] = mapped_column(String(64), comment="推送主题，如 NEW_MESSAGE")
    store_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="识别出的店铺 ID；识别不出时为空并触发全部店铺拉取")
    ebay_conversation_id: Mapped[str | None] = mapped_column(String(128), comment="推送里的 eBay 会话 ID（有的话）")
    status: Mapped[str] = mapped_column(String(16), default="QUEUED",
                                        comment="QUEUED 已排队拉取 / PROCESSED 已拉取 / ERROR 拉取失败 / IGNORED 非消息主题 / PAUSED 暂停拉取期间收到（只记录）")
    payload: Mapped[dict | None] = mapped_column(JSON, comment="推送内容（消息正文类字段已省略，只留结构与标识，便于排查）")
    error: Mapped[str | None] = mapped_column(Text, comment="拉取失败原因")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, comment="收到时间（UTC）")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, comment="拉取完成时间（UTC）")


class EbayPushSubscription(Base):
    __tablename__ = "ebay_push_subscriptions"
    __table_args__ = {"comment": "各店铺在 eBay Notification API 的推送订阅（一个店铺一行），由工作台“启用推送订阅”创建/更新"}

    store_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="店铺 ID")
    topic: Mapped[str] = mapped_column(String(64), default="NEW_MESSAGE", comment="订阅主题")
    subscription_id: Mapped[str | None] = mapped_column(String(64), comment="eBay 订阅 ID")
    destination_id: Mapped[str | None] = mapped_column(String(64), comment="eBay 推送目的地 ID（全店共用一个）")
    status: Mapped[str | None] = mapped_column(String(16), comment="eBay 侧订阅状态：ENABLED / DISABLED；失败时为 ERROR")
    last_error: Mapped[str | None] = mapped_column(Text, comment="最近一次创建/启用/测试失败原因（不含令牌）")
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最近一次请求 eBay 发测试推送的时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="最近更新时间（UTC）")


class DataDeletionRequest(Base):
    __tablename__ = "data_deletion_requests"
    __table_args__ = {"comment": "数据删除请求：eBay 账号删除通知与买家删除请求的执行跟踪，只存用户键哈希"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    source: Mapped[str] = mapped_column(String(32), comment="来源，如 ebay_account_deletion")
    notification_id: Mapped[str | None] = mapped_column(String(128), unique=True, comment="平台通知 ID（用于去重）")
    ebay_user_key_hash: Mapped[str] = mapped_column(String(64), comment="eBay 用户标识的 SHA-256 哈希，不存明文")
    status: Mapped[str] = mapped_column(String(16), default="RECEIVED",
                                        comment="处理状态：RECEIVED_UNVERIFIED 未验签 / RECEIVED / DONE 等")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="收到时间（UTC）")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, comment="删除完成时间（UTC）")


class StoreCredentialImport(Base):
    __tablename__ = "store_credential_imports"
    __table_args__ = {"comment": "店铺凭证导入批次：每次整表覆盖导入记一行（只记文件指纹与数量，不含任何秘钥）"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="批次主键")
    file_name: Mapped[str] = mapped_column(String(300), comment="导入的文件名（不含路径）")
    file_sha256: Mapped[str] = mapped_column(String(64), comment="导入文件的 SHA-256，用于核对是哪一版文件")
    rows: Mapped[int] = mapped_column(Integer, comment="导入的店铺数")
    rows_with_secret: Mapped[int] = mapped_column(Integer, comment="其中带秘钥的店铺数")
    replaced_rows: Mapped[int] = mapped_column(Integer, comment="被本次覆盖删除的旧记录数")
    key_version: Mapped[str] = mapped_column(String(16), comment="加密主密钥版本")
    imported_by: Mapped[str] = mapped_column(String(64), comment="执行导入的人或程序")
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="导入时间（UTC）")


class StoreCredential(Base):
    __tablename__ = "store_credentials"
    __table_args__ = {"comment": "店铺凭证（紫鸟店铺全量表）：账号、IP、browserId、秘钥整体 AES-256-GCM 加密；"
                                 "只能整表覆盖导入，数据库触发器禁止修改，接口不返回明文或密文"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_key: Mapped[str] = mapped_column(String(32), unique=True,
                                           comment="店铺标识：店铺名称+平台的哈希，覆盖导入后保持不变，用于关联本系统店铺")
    seq: Mapped[int | None] = mapped_column(Integer, comment="源文件序号")
    store_name: Mapped[str] = mapped_column(String(200), comment="店铺名称（明文，用于显示和匹配）")
    platform: Mapped[str] = mapped_column(String(64), comment="平台，如 eBay-德国（明文）")
    site_id: Mapped[str | None] = mapped_column(String(16), comment="紫鸟站点 ID（明文）")
    has_secret: Mapped[bool] = mapped_column(Boolean, comment="是否带秘钥（eBay refresh token）")
    secret_fingerprint: Mapped[str | None] = mapped_column(
        String(16), comment="秘钥指纹（SHA-256 前 12 位，不可逆），用于比对是否同一秘钥")
    encrypted_payload: Mapped[str] = mapped_column(
        Text, comment="加密载荷（Base64：nonce + 密文），内含店铺账号、店铺 IP、IP 是否过期、browserId、秘钥")
    key_version: Mapped[str] = mapped_column(String(16), comment="加密主密钥版本（主密钥只在 .env，不入库）")
    import_id: Mapped[int] = mapped_column(ForeignKey("store_credential_imports.id"), comment="所属导入批次")
    # 以下由导入时用该店秘钥只读查询 eBay（GetUser / GetStore）得到；只能随整表覆盖导入刷新
    store_id: Mapped[str | None] = mapped_column(String(64), comment="对应本系统店铺 ID（stores.id），供工作台店铺筛选")
    ebay_user_id: Mapped[str | None] = mapped_column(String(128), comment="eBay 卖家账号（UserID，明文）")
    ebay_store_name: Mapped[str | None] = mapped_column(String(200), comment="eBay 原始店铺名（GetStore，明文）；未开店铺时为空")
    ebay_store_url: Mapped[str | None] = mapped_column(String(300), comment="eBay 店铺页地址")
    ebay_registration_site: Mapped[str | None] = mapped_column(String(32), comment="eBay 账号注册站点，如 Germany、HongKong")
    ebay_profile_error: Mapped[str | None] = mapped_column(String(300), comment="查询 eBay 店铺信息失败的原因（如秘钥失效）")
    ebay_profile_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, comment="查询 eBay 店铺信息的时间（UTC）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="导入时间（UTC）")


# ---------------------------------------------------------------- 数据助手（第 21 章：店铺数据查询与报表）
class InvoiceStatus:
    ISSUED = "ISSUED"  # 已生成（留底），还没发给买家
    SENT = "SENT"  # 已通过邮件发给买家
    SIMULATED = "SIMULATED"  # 邮件发送未开启（INVOICE_MAIL_ENABLED=false），只记录、没有真正发出
    SEND_FAILED = "SEND_FAILED"  # 邮件发送失败，可重试
    VOID = "VOID"  # 作废（开错了；文件和号码保留）
    LABELS = {ISSUED: "已生成", SENT: "已发邮件", SIMULATED: "模拟发送（未真正发出）", SEND_FAILED: "发送失败", VOID: "已作废"}


class InvoiceRequestStatus:
    WAITING_EMAIL = "WAITING_EMAIL"  # 买家要发票，AI 已请买家给邮箱，还没收到
    OPEN = "OPEN"  # 买家已给邮箱，等人工审核后发送
    DONE = "DONE"  # 发票已发出
    CANCELLED = "CANCELLED"  # 不用开了
    LABELS = {WAITING_EMAIL: "等买家邮箱", OPEN: "待审核发送", DONE: "已发送", CANCELLED: "已取消"}


class AppSetting(Base):
    __tablename__ = "app_settings"
    __table_args__ = {"comment": "系统配置页：管理员在页面上改的配置项，优先于 .env；删掉一行就回到 .env 的值"}

    key: Mapped[str] = mapped_column(String(64), primary_key=True, comment="配置项（Settings 字段名，小写）")
    value: Mapped[Any] = mapped_column(JSON, comment="配置值（密码类也存在这里，接口不返回）")
    updated_by: Mapped[str | None] = mapped_column(String(64), comment="最后修改的管理员")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后修改时间（UTC）")


class InvoiceIssuer(Base):
    __tablename__ = "invoice_issuers"
    __table_args__ = {"comment": "开票主体：发票上的卖方公司信息；一个主体可给多个店铺用（stores.invoice_issuer_id）；只有管理员维护"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    name: Mapped[str] = mapped_column(String(200), comment="公司名称（发票抬头上的卖方，建议用拼音/英文）")
    address: Mapped[str] = mapped_column(Text, comment="公司地址（可多行）")
    postcode: Mapped[str | None] = mapped_column(String(32), comment="邮编")
    phone: Mapped[str | None] = mapped_column(String(64), comment="电话（可空）")
    email: Mapped[str | None] = mapped_column(String(200), comment="公司邮箱（可空，印在发票上）")
    vat_numbers: Mapped[dict | None] = mapped_column(JSON, comment="各国增值税号，如 {\"DE\": \"DE329615047\", \"GB\": \"GB345543105\"}")
    vat_rates: Mapped[dict | None] = mapped_column(JSON, comment="各国税率（%），如 {\"DE\": 19, \"GB\": 20}；收货国家没配的发票不列增值税")
    footer: Mapped[str | None] = mapped_column(Text, comment="发票页脚文字（可空）")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否已确认启用；未启用的主体不能开票")
    updated_by: Mapped[str | None] = mapped_column(String(64), comment="最近修改人")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="最近修改时间（UTC）")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = {"comment": "发票留底：每次生成的 PDF 都存一份（data/invoices/），开出后内容不改；只有管理员能查看留底。"
                                 "不随聊天记录保留期清理"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    number: Mapped[str] = mapped_column(String(64), unique=True, comment="发票号：开票日期-订单号后 5 位，同号再开加 -2、-3")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="来源会话 ID（会话删了这里保留）")
    request_id: Mapped[int | None] = mapped_column(Integer, comment="对应的发票申请 ID")
    order_id: Mapped[str] = mapped_column(String(64), index=True, comment="eBay 订单号")
    issuer_id: Mapped[int] = mapped_column(Integer, comment="开票主体 ID")
    issue_date: Mapped[str] = mapped_column(String(10), comment="开票日期 YYYY-MM-DD")
    kind: Mapped[str | None] = mapped_column(String(8), comment="发票种类：NORMAL 普通发票（同 eBay 订单单据）/ VAT 增值税发票（店铺 VAT 模板）")
    language: Mapped[str] = mapped_column(String(8), comment="发票语言：de / en / fr / it / es")
    currency: Mapped[str] = mapped_column(String(8), comment="币种")
    total: Mapped[float] = mapped_column(Float, comment="发票总额（含税）")
    vat_rate: Mapped[float | None] = mapped_column(Float, comment="增值税率（%），空 = 未列增值税")
    vat_amount: Mapped[float | None] = mapped_column(Float, comment="其中增值税额")
    snapshot: Mapped[dict] = mapped_column(JSON, comment="开票时的完整内容快照（卖方、买方、明细、金额），含买家姓名地址，只有管理员可看")
    file_path: Mapped[str] = mapped_column(String(500), comment="PDF 相对路径（相对发票目录）")
    sha256: Mapped[str] = mapped_column(String(64), comment="PDF 文件 SHA-256，核对留底未被改动")
    status: Mapped[str] = mapped_column(String(16), default="ISSUED", comment="ISSUED 已生成 / SENT 已发邮件 / SIMULATED 模拟发送 / SEND_FAILED 发送失败 / VOID 作废")
    created_by: Mapped[str] = mapped_column(String(64), comment="开票人（显示名；AI 自动生成为 agent）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="生成时间（UTC）")
    sent_to: Mapped[str | None] = mapped_column(String(200), comment="发送到的买家邮箱")
    sent_by: Mapped[str | None] = mapped_column(String(64), comment="审核并发送的人")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, comment="发送时间（UTC）")
    send_error: Mapped[str | None] = mapped_column(String(500), comment="最近一次发送失败原因")
    void_reason: Mapped[str | None] = mapped_column(String(500), comment="作废原因")


class InvoiceRequest(Base):
    __tablename__ = "invoice_requests"
    __table_args__ = {"comment": "发票申请：买家要发票时自动登记（先等买家邮箱，给了邮箱后待审核），在发票页人工审核后发邮件并通知买家；一个会话一条"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    conversation_id: Mapped[int] = mapped_column(Integer, unique=True, comment="会话 ID")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    email: Mapped[str | None] = mapped_column(String(200), comment="买家提供的接收发票邮箱")
    order_id: Mapped[str | None] = mapped_column(String(64), comment="要开票的订单号（AI 按买家订单自动选，或客服选）")
    buyer_company: Mapped[str | None] = mapped_column(String(200), comment="买家公司名（增值税发票抬头，可空）")
    buyer_vat: Mapped[str | None] = mapped_column(String(64), comment="买家增值税号（可空）")
    invoice_type: Mapped[str | None] = mapped_column(String(8), comment="买家要的发票种类：NORMAL 普通 / VAT 含增值税；空 = 还没问清")
    status: Mapped[str] = mapped_column(String(16), default="OPEN", comment="WAITING_EMAIL 等买家邮箱 / OPEN 待审核发送 / DONE 已发送 / CANCELLED 取消")
    source: Mapped[str] = mapped_column(String(16), default="agent", comment="来源：agent AI 识别 / manual 客服填写")
    note: Mapped[str | None] = mapped_column(String(500), comment="说明（如自动开票失败原因）")
    trigger_message_id: Mapped[str | None] = mapped_column(
        String(128), comment="最近一次触发开票的买家消息（平台 ID）：同一条消息重跑不重复开票发邮件，买家再要一次就再开一张")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="最近更新时间（UTC）")


class AsFileStatus:
    STORED = "STORED"  # 已保存，可查看
    INFECTED = "INFECTED"  # 病毒扫描发现威胁，文件已删除
    TOO_LARGE = "TOO_LARGE"  # 超过单文件上限，已丢弃
    BLOCKED_TYPE = "BLOCKED_TYPE"  # 文件类型不允许（可执行文件等），未保存
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"  # WeTransfer 等链接下载失败，需人工下载
    PURGED = "PURGED"  # 满保留期已清理，只留记录
    UPLOADING = "UPLOADING"  # 买家正在上传页上传（还在云端存储 R2）
    WAITING_FETCH = "WAITING_FETCH"  # 买家已传完，等系统从 R2 拉回本机
    LABELS = {STORED: "已保存", INFECTED: "发现病毒已删除", TOO_LARGE: "超过大小上限", BLOCKED_TYPE: "类型不允许",
              DOWNLOAD_FAILED: "下载失败（请人工下载）", PURGED: "已清理", UPLOADING: "买家上传中",
              WAITING_FETCH: "已上传，等系统拉回"}


class AftersalesMail(Base):
    __tablename__ = "aftersales_mails"
    __table_args__ = {"comment": "售后邮箱收到并处理过的邮件：认出的订单号、店铺、会话；处理完邮件移到“已处理”文件夹"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    message_id: Mapped[str] = mapped_column(String(300), unique=True, comment="邮件 Message-ID（无则用 文件夹+UID 生成），防止重复处理")
    sender: Mapped[str | None] = mapped_column(String(200), comment="发件人邮箱（买家个人信息，只用于匹配会话）")
    subject: Mapped[str | None] = mapped_column(String(500), comment="邮件主题")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, comment="邮件日期（UTC）")
    order_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="认出的 eBay 订单号")
    store_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="订单所属店铺；空 = 待认领")
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="关联的买家会话")
    match: Mapped[str | None] = mapped_column(String(32), comment="怎么认出来的：order_local 本地订单 / order_api eBay 查询 / invoice_email 发票邮箱 / manual 人工认领")
    status: Mapped[str] = mapped_column(String(16), default="PROCESSED", comment="PROCESSED 已处理 / UNMATCHED 待认领 / FAILED 处理失败")
    files: Mapped[int] = mapped_column(Integer, default=0, comment="保存的文件数")
    body_excerpt: Mapped[str | None] = mapped_column(Text, comment="正文前 2000 字（给人工认领看；满保留期清空）")
    error: Mapped[str | None] = mapped_column(String(500), comment="处理失败原因")
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, comment="处理时间（UTC）")


class MailboxCursor(Base):
    __tablename__ = "mailbox_cursors"
    __table_args__ = {"comment": "售后邮箱收信进度：收件箱里看过的最大 UID。只看新来的邮件；和售后无关的邮件不记录、不移动、不改已读状态"}

    folder: Mapped[str] = mapped_column(String(100), primary_key=True, comment="IMAP 文件夹名（如 INBOX）")
    uidvalidity: Mapped[str | None] = mapped_column(String(32), comment="文件夹的 UIDVALIDITY；变了说明 UID 重排，要按日期重新开始")
    last_uid: Mapped[int] = mapped_column(Integer, default=0, comment="已经看过的最大 UID（处理或判定无关都算）")
    skipped: Mapped[int] = mapped_column(Integer, default=0, comment="累计判定为和售后无关、没碰的邮件数")
    retry_uid: Mapped[int | None] = mapped_column(Integer, comment="处理出错、正在重试的邮件 UID")
    retries: Mapped[int] = mapped_column(Integer, default=0, comment="这封已经失败的次数；到 3 次跳过它（留在收件箱人工看），不挡后面的邮件")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后更新时间（UTC）")


class AftersalesFile(Base):
    __tablename__ = "aftersales_files"
    __table_args__ = {"comment": "售后邮件附件：邮件直接带的附件或 WeTransfer 链接里的文件，按店铺存放在 AFTERSALES_FILES_DIR；"
                                 "满保留期删文件、留记录"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    mail_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="来源邮件 ID（aftersales_mails）；上传页来的为空")
    upload_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="来源上传记录 ID（buyer_uploads）；邮件来的为空")
    store_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="店铺 ID；空 = 待认领")
    order_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="eBay 订单号")
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="关联的买家会话")
    source: Mapped[str] = mapped_column(String(16), comment="来源：ATTACHMENT 邮件附件 / WETRANSFER 链接下载 / LINK 其他分享链接 / UPLOAD 上传页")
    original_name: Mapped[str | None] = mapped_column(String(300), comment="原始文件名")
    content_type: Mapped[str | None] = mapped_column(String(100), comment="文件类型（MIME）")
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, comment="文件大小（字节）")
    sha256: Mapped[str | None] = mapped_column(String(64), comment="文件 SHA-256 指纹")
    rel_path: Mapped[str | None] = mapped_column(String(500), comment="相对 AFTERSALES_FILES_DIR 的路径；清理后保留原路径作记录")
    link_url: Mapped[str | None] = mapped_column(String(1000), comment="WeTransfer 原链接（下载失败时人工用）")
    status: Mapped[str] = mapped_column(String(16), comment="STORED / INFECTED / TOO_LARGE / BLOCKED_TYPE / DOWNLOAD_FAILED / PURGED")
    status_reason: Mapped[str | None] = mapped_column(String(500), comment="状态说明")
    scan_result: Mapped[str | None] = mapped_column(String(32), comment="病毒扫描：CLEAN / THREAT / SKIPPED / ERROR")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, comment="保存时间（UTC），按它算保留期")
    purged_at: Mapped[datetime | None] = mapped_column(DateTime, comment="清理时间（UTC）")
    claimed_by: Mapped[str | None] = mapped_column(String(64), comment="人工认领/改关联的人")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, comment="认领时间（UTC）")
    r2_key: Mapped[str | None] = mapped_column(String(300), comment="上传页：文件在 R2 存储桶里的位置（拉回后删除 R2 文件，这里保留作记录）")
    r2_upload_id: Mapped[str | None] = mapped_column(String(300), comment="上传页：R2 分块上传 ID")
    r2_state: Mapped[str | None] = mapped_column(String(16), comment="上传页拉回状态：UPLOADING 上传中 / UPLOADED 已传完 / FETCHED 已拉回 / ABORTED 已放弃")
    fetch_error: Mapped[str | None] = mapped_column(String(500), comment="上传页：最近一次拉回失败原因")


class BuyerUploadStatus:
    DRAFT = "DRAFT"  # 买家已通过订单校验（或客服生成了链接），还没提交
    SUBMITTED = "SUBMITTED"  # 买家已提交，等系统把文件从 R2 拉回
    RECEIVED = "RECEIVED"  # 文件已全部拉回本机（或其中有失败的，见文件状态）
    EXPIRED = "EXPIRED"  # 链接过期没提交
    LABELS = {DRAFT: "待买家提交", SUBMITTED: "已提交，拉回中", RECEIVED: "已收到", EXPIRED: "已过期"}


class BuyerUpload(Base):
    __tablename__ = "buyer_uploads"
    __table_args__ = {"comment": "买家上传页的上传记录：买家用订单号 + 邮编/eBay 用户名校验（或客服给的签名链接）后，"
                                 "填问题、传视频；文件先直传 Cloudflare R2，再由后台拉回本机售后附件目录"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    code: Mapped[str | None] = mapped_column(String(32), unique=True, comment="售后编号（提交后生成，给买家看），如 AS-20261103-0007")
    order_id: Mapped[str] = mapped_column(String(64), index=True, comment="eBay 订单号")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="订单所属店铺")
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="关联的买家会话（按订单买家找到的）")
    buyer_key: Mapped[str | None] = mapped_column(String(128), comment="订单买家的 eBay 账号")
    item_id: Mapped[str | None] = mapped_column(String(64), comment="买家选的是订单里哪个商品（刊登号）")
    item_title: Mapped[str | None] = mapped_column(String(300), comment="商品标题")
    issue_type: Mapped[str | None] = mapped_column(String(32), comment="问题类型：NOT_FIT 不适配 / INSTALL 安装问题 / DAMAGED 损坏 / "
                                                                     "NOT_WORKING 不工作 / WRONG_MISSING 错发缺件 / OTHER 其他")
    description: Mapped[str | None] = mapped_column(Text, comment="买家写的问题描述（满保留期清空）")
    vehicle: Mapped[dict | None] = mapped_column(JSON, comment="车辆信息：品牌、车型、年份、发动机、VIN（不适配/安装问题时填）")
    installed: Mapped[str | None] = mapped_column(String(16), comment="安装情况：INSTALLED 已安装 / TRIED 试装过 / NOT 未安装")
    language: Mapped[str | None] = mapped_column(String(8), comment="页面语言：de / en")
    verify_method: Mapped[str] = mapped_column(String(16), comment="校验方式：ZIP 邮编 / USERNAME eBay 用户名 / LINK 客服生成的链接")
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", comment="DRAFT 待提交 / SUBMITTED 拉回中 / RECEIVED 已收到 / EXPIRED 已过期")
    session_hash: Mapped[str | None] = mapped_column(String(64), index=True, comment="上传页会话令牌的 SHA-256（不存明文）")
    session_expires_at: Mapped[datetime | None] = mapped_column(DateTime, comment="会话令牌到期时间（UTC）")
    link_expires_at: Mapped[datetime | None] = mapped_column(DateTime, comment="客服生成的上传链接到期时间（UTC）")
    created_by: Mapped[str | None] = mapped_column(String(64), comment="生成链接的客服；买家自己校验进来的为空")
    ip_masked: Mapped[str | None] = mapped_column(String(64), comment="来源 IP（脱敏，如 203.0.113.x）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, comment="创建时间（UTC）")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, comment="买家提交时间（UTC）")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, comment="文件全部拉回时间（UTC）")


class TrackingNumber(Base):
    __tablename__ = "tracking_numbers"
    __table_args__ = {"comment": "物流号与 17TRACK 轨迹缓存：每个物流号只注册一次（注册扣额度），轨迹按缓存时长复用"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    number: Mapped[str] = mapped_column(String(64), unique=True, comment="物流号")
    store_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="首次查询时的店铺 ID")
    order_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="eBay 订单号")
    source: Mapped[str | None] = mapped_column(String(16), comment="物流号来源：ebay 订单发货记录 / goodcang 谷仓出库单")
    carrier_name: Mapped[str | None] = mapped_column(String(100), comment="来源给出的承运商名称，如 YunExpress、DHL Paket")
    carrier_code: Mapped[int | None] = mapped_column(Integer, comment="17TRACK 运输商代码（注册时明确传入或由 17TRACK 识别）")
    registered_at: Mapped[datetime | None] = mapped_column(DateTime, comment="在 17TRACK 注册的时间（UTC）；空 = 未注册")
    register_error: Mapped[str | None] = mapped_column(String(300), comment="注册被拒的原因（如运输商无法识别）")
    status: Mapped[str | None] = mapped_column(String(32), comment="17TRACK 主状态，如 InTransit、Delivered、Exception")
    sub_status: Mapped[str | None] = mapped_column(String(64), comment="17TRACK 子状态，如 Exception_Lost")
    delivered_at: Mapped[str | None] = mapped_column(String(40), comment="签收时间（运输商原始时间）")
    track: Mapped[dict | None] = mapped_column(JSON, comment="整理后的轨迹（状态、最新事件、事件列表、时效），不含收件人信息")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, comment="最近一次从 17TRACK 取到轨迹的时间（UTC）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")


class LogisticsTicket(Base):
    __tablename__ = "logistics_tickets"
    __table_args__ = {"comment": "谷仓查件单（同步自谷仓 OMS，含在谷仓后台手动建的）：关联到店铺和买家会话；谷仓回复后生成给买家的回复建议"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    io_code: Mapped[str] = mapped_column(String(40), unique=True, comment="谷仓查件单号，如 CJ202611033344")
    order_code: Mapped[str | None] = mapped_column(String(50), comment="谷仓出库单号")
    reference_no: Mapped[str | None] = mapped_column(String(64), index=True, comment="参考号 = eBay 订单号")
    tracking_no: Mapped[str | None] = mapped_column(String(64), index=True, comment="跟踪号")
    type_name: Mapped[str | None] = mapped_column(String(40), comment="查件类型，如 断更、申请POD、妥投未收到")
    io_status: Mapped[int | None] = mapped_column(Integer, comment="谷仓查件状态码，如 3 处理中 / 7 已回复待完结 / 8 已完结")
    io_status_text: Mapped[str | None] = mapped_column(String(40), comment="谷仓查件状态文字")
    trail_status_text: Mapped[str | None] = mapped_column(String(40), comment="谷仓记录的物流状态，如 派送完毕")
    sm_name: Mapped[str | None] = mapped_column(String(100), comment="物流产品名称")
    warehouse_code: Mapped[str | None] = mapped_column(String(20), comment="仓库代码")
    reply: Mapped[str | None] = mapped_column(Text, comment="谷仓客服回复（内部信息，不直接发给买家）")
    reject_reason: Mapped[str | None] = mapped_column(Text, comment="谷仓驳回原因")
    attachments: Mapped[list | None] = mapped_column(JSON, comment="谷仓回复附件 [{name, url, file_id}]（file_id 为存进售后附件后的 ID）")
    added_at: Mapped[str | None] = mapped_column(String(20), comment="谷仓创建时间（北京时间）")
    replied_at: Mapped[str | None] = mapped_column(String(20), comment="谷仓回复时间（北京时间）")
    store_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="订单所属店铺；空 = 还没定位到")
    buyer_key: Mapped[str | None] = mapped_column(String(128), comment="订单买家 eBay 账号（只用于匹配会话）")
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="关联的买家会话")
    suggestion: Mapped[str | None] = mapped_column(Text, comment="AI 根据谷仓回复起草的给买家的回复（买家语言）")
    suggestion_zh: Mapped[str | None] = mapped_column(Text, comment="回复建议的中文回译")
    needs_human: Mapped[bool | None] = mapped_column(Boolean, comment="结果是否需要人工处理（如妥投未收到、退回、可能补发）")
    suggested_at: Mapped[datetime | None] = mapped_column(DateTime, comment="生成回复建议的时间（UTC）")
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, comment="客服处理完（已回复买家）的时间（UTC）；空 = 待处理")
    handled_by: Mapped[str | None] = mapped_column(String(64), comment="处理的客服")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="最近同步时间（UTC）")
    origin: Mapped[str | None] = mapped_column(String(16), comment="来源：空 从谷仓同步；AI_DRAFT 系统按物流规则起草（草稿只存本系统，查件单号为 DRAFT- 开头，提交后换成谷仓单号）")
    io_desc: Mapped[str | None] = mapped_column(String(300), comment="查件需求（系统起草、客服可改）")
    created_by: Mapped[str | None] = mapped_column(String(64), comment="起草人（系统起草的为 AI（物流规则））")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, comment="系统起草的时间（UTC）")
    it_type_id: Mapped[int | None] = mapped_column(Integer, comment="查件类型 ID（谷仓查件类型接口的 value）")
    sm_code: Mapped[str | None] = mapped_column(String(32), comment="物流方式代码（选查件类型用），如 DEDHL")
    submitted_by: Mapped[str | None] = mapped_column(String(64), comment="在工作台提交到谷仓的客服")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, comment="提交到谷仓的时间（UTC）")
    send_status: Mapped[str | None] = mapped_column(String(16), comment="查件结果发给买家：SENT 已发送 / NEEDS_HUMAN 需人工 / FAILED 发送失败 / NO_BUYER 找不到买家；空 = 还没回复或未处理")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, comment="查件结果发给买家的时间（UTC）")
    sent_message_id: Mapped[int | None] = mapped_column(Integer, comment="发给买家的消息 ID（messages.id）")
    send_error: Mapped[str | None] = mapped_column(String(300), comment="没自动发送的原因")
    outcome: Mapped[str | None] = mapped_column(
        String(24), comment="谷仓回复的结果类别（规则判定，见 logistics/tickets.py OUTCOMES）：如 IN_TRANSIT 已催派、POD 已给签收证明、"
                            "LOST 疑似丢失、ADDRESS 地址问题、UNKNOWN 看不懂；谷仓再次回复时清空重判")
    notice_status: Mapped[str | None] = mapped_column(
        String(200), comment="提交后给买家的安抚消息：SENT 已发送；其他为没发的原因（如买家还问了别的问题、还有其他需人工的原因）")
    notice_sent_at: Mapped[datetime | None] = mapped_column(DateTime, comment="安抚消息发出时间（UTC）")


class ShipNotice(Base):
    __tablename__ = "ship_notices"
    __table_args__ = {"comment": "发货通知：每天汇总当天在 eBay 标记已发货的订单，按站点语言写好订单号、承运商、跟踪号、查件链接，人工审核后发给买家（目前为模拟发送）"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="订单所属店铺")
    order_id: Mapped[str] = mapped_column(String(64), unique=True, comment="eBay 订单号（一个订单只发一次）")
    buyer_key: Mapped[str | None] = mapped_column(String(128), comment="买家 eBay 账号")
    marketplace_id: Mapped[str | None] = mapped_column(String(32), comment="订单站点，决定消息语言")
    ship_date: Mapped[str] = mapped_column(String(10), index=True, comment="发货日期（北京时间 YYYY-MM-DD，按 eBay 标记发货时间）")
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime, comment="eBay 标记发货时间（UTC）")
    packages: Mapped[list] = mapped_column(JSON, comment="包裹 [{carrier, tracking_no, url}]")
    item_ids: Mapped[list | None] = mapped_column(JSON, comment="订单商品的刊登号（找买家同一商品的会话用）")
    language: Mapped[str] = mapped_column(String(8), comment="消息语言 de / en / fr / it / es")
    text: Mapped[str] = mapped_column(Text, comment="发给买家的消息（可在审核时修改）")
    text_zh: Mapped[str | None] = mapped_column(Text, comment="中文对照")
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True, comment="PENDING 待审核 / SENT 已发送 / SKIPPED 不发")
    skip_reason: Mapped[str | None] = mapped_column(String(200), comment="不发的原因（如订单已取消、有退款，或审核员跳过）")
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="发到的买家会话（没有会话的发送时按订单新建）")
    sent_message_id: Mapped[int | None] = mapped_column(Integer, comment="发出的消息 ID（messages.id）")
    sent_by: Mapped[str | None] = mapped_column(String(64), comment="发送（审核通过）的人")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, comment="发送时间（UTC）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="生成时间（UTC）")


class AftersalesCase(Base):
    __tablename__ = "aftersales_cases"
    __table_args__ = {"comment": "飞书售后群工单：需技术/售后判断的问题整理后发到飞书售后群，群里回复后 AI 起草买家回复（人工审核后才发）"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    conversation_id: Mapped[int] = mapped_column(Integer, index=True, comment="买家会话")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺（卡片里不显示）")
    order_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="订单号；没有订单 = 售前")
    buyer_key: Mapped[str | None] = mapped_column(String(128), comment="买家 eBay 账号")
    marketplace_id: Mapped[str | None] = mapped_column(String(32), comment="站点")
    case_type: Mapped[str] = mapped_column(String(8), comment="售后 / 售前（有订单 = 售后）")
    category: Mapped[str] = mapped_column(String(32), comment="问题类别：质量问题 / 破损错发缺件 / 安装技术问题 / 适配核对")
    sku: Mapped[str | None] = mapped_column(String(100), comment="原 SKU（去掉平台别名前缀后缀）")
    problem_zh: Mapped[str] = mapped_column(Text, comment="问题（中文）")
    fault_info: Mapped[str | None] = mapped_column(Text, comment="故障码 / VIN")
    original_text: Mapped[str] = mapped_column(Text, comment="买家原文")
    attachment_ids: Mapped[list | None] = mapped_column(JSON, comment="随工单发到群里的买家图片（message_attachments.id）")
    file_ids: Mapped[list | None] = mapped_column(
        JSON, comment="已发到工单话题里的售后附件（aftersales_files.id）：买家通过售后邮箱、上传页发来的视频和图片")
    missing: Mapped[list | None] = mapped_column(JSON, comment="还缺的资料（发群前建议先向买家要），如 照片、故障码、VIN")
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", index=True,
                                        comment="DRAFT 待发送 / SENT 已发待回复 / REPLIED 群已回复待审核 / ANSWERED 已回复买家 / CLOSED 已完结")
    origin: Mapped[str | None] = mapped_column(String(16), comment="AI 按划分规则建议 / MANUAL 客服手动")
    chat_id: Mapped[str | None] = mapped_column(String(64), comment="飞书群")
    message_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="工单卡片的飞书消息 ID")
    thread_id: Mapped[str | None] = mapped_column(String(64), comment="卡片话题 ID")
    replies: Mapped[list | None] = mapped_column(JSON, comment="群里的回复 [{message_id, sender, text, file_ids, at, via: thread|mention}]")
    last_buyer_seq: Mapped[int | None] = mapped_column(Integer, comment="已同步到话题的买家消息序号（之后的买家追问追加到话题）")
    draft_reply: Mapped[str | None] = mapped_column(Text, comment="AI 按群里回复起草的买家回复（买家语言，人工审核后才发）")
    draft_reply_zh: Mapped[str | None] = mapped_column(Text, comment="起草回复的中文对照")
    drafted_at: Mapped[datetime | None] = mapped_column(DateTime, comment="起草时间（UTC）")
    sent_by: Mapped[str | None] = mapped_column(String(64), comment="发到群里的人")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, comment="发到群里的时间（UTC）")
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, comment="已回复买家的时间（UTC）")
    closed_by: Mapped[str | None] = mapped_column(String(64), comment="完结人")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, comment="完结时间（UTC）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="生成时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）")


class ParcelFollowup(Base):
    __tablename__ = "parcel_followups"
    __table_args__ = {"comment": "异常包裹跟进表：断更、无轨迹、妥投未收到、退回、损坏、发错等包裹的处理记录（重发、退款、索赔…），"
                                 "替代原 Excel《异常包裹跟进表》。每人只看自己记的，管理员看全部；可关联到买家会话"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    owner_id: Mapped[int] = mapped_column(Integer, index=True, comment="负责人（记录的人）账号 ID：普通账号只能看、改自己的")
    owner_name: Mapped[str] = mapped_column(String(64), comment="负责人显示名")
    occurred_on: Mapped[str | None] = mapped_column(String(10), index=True, comment="日期（YYYY-MM-DD，发现异常 / 登记的日期）")
    store_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="店铺 ID（账号）")
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="关联的买家会话（点了直接打开）")
    buyer_key: Mapped[str | None] = mapped_column(String(128), index=True, comment="买家 eBay 用户名")
    order_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="eBay 订单号")
    sku: Mapped[str | None] = mapped_column(String(200), comment="SKU（多个用 + 连）")
    reason: Mapped[str] = mapped_column(String(32), index=True, comment="原因类型：断更 / 无轨迹 / 妥投未收到 / 物流异常退回 / 到货损坏 / 谷仓发错 / 供应商发错 / 产品问题 / 地址问题 / 其他")
    reason_note: Mapped[str | None] = mapped_column(String(300), comment="原因说明（如“4.30断更”“6.22妥投未收到 INR”）")
    carrier: Mapped[str | None] = mapped_column(String(64), comment="物流商（DHL、Royal Mail、EVRI…）")
    tracking_no: Mapped[str | None] = mapped_column(String(64), index=True, comment="原包裹跟踪号")
    action: Mapped[str | None] = mapped_column(String(32), comment="处理方式：重发 / 退款 / 部分退款 / 补偿 / 索赔 / 等待查件 / 等待买家 / 其他")
    result: Mapped[str | None] = mapped_column(String(500), comment="处理结果（如 已送达、已退款、未收到货纠纷、无库存不能重发）")
    reship_tracking_no: Mapped[str | None] = mapped_column(String(64), index=True, comment="重发包裹的跟踪号")
    amount: Mapped[float | None] = mapped_column(Float, comment="金额（退款 / 补偿 / 运费等）")
    currency: Mapped[str | None] = mapped_column(String(8), comment="币种")
    ticket_no: Mapped[str | None] = mapped_column(String(200), comment="谷仓查件单号（多个用逗号）")
    next_follow_on: Mapped[str | None] = mapped_column(String(10), index=True, comment="下次跟进日期（YYYY-MM-DD）")
    status: Mapped[str] = mapped_column(String(8), default="OPEN", index=True, comment="跟进状态：OPEN 跟进中 / DONE 已完结")
    note: Mapped[str | None] = mapped_column(Text, comment="备注")
    origin: Mapped[str | None] = mapped_column(String(8), index=True, comment="来源：AUTO 系统自动建 / 空 = 人工建")
    next_step: Mapped[str | None] = mapped_column(String(300), comment="下一步（系统按进度写：等查件结果、需决定补发或退款、可以完结…）")
    can_close: Mapped[bool | None] = mapped_column(Boolean, comment="系统判断可以完结（已退款、纠纷已关闭等），等人确认")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后修改时间（UTC）")
    updated_by: Mapped[str | None] = mapped_column(String(64), comment="最后修改的人")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, comment="完结时间（UTC）")


# ---------------------------------------------------------------- 上线前业务基线（第 2.1 节）
class BaselineBatch(Base):
    __tablename__ = "baseline_batches"
    __table_args__ = {"comment": "上线前业务基线补拉批次：每次从 eBay 只读补拉历史消息记一行，带进度和统计；断点续拉按批次接着拉"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键（批次号）")
    stores: Mapped[list] = mapped_column(JSON, comment="本批次补拉的店铺 ID 列表")
    period_start: Mapped[datetime] = mapped_column(DateTime, comment="基线时间段起点（UTC，含）")
    period_end: Mapped[datetime] = mapped_column(DateTime, comment="基线时间段终点（UTC，不含；如 2026-11-05 00:00 表示到 11-04 当天结束）")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="开始时间（UTC；续拉时为最近一次开始时间）")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, comment="完成时间（UTC）；没拉完为空")
    status: Mapped[str] = mapped_column(String(16), default="RUNNING", index=True,
                                        comment="状态：RUNNING 进行中（或中断待续拉）/ DONE 完成 / PARTIAL 完成但有店铺出错 / FAILED 失败")
    stats: Mapped[dict | None] = mapped_column(JSON, comment="统计：会话数、消息数（买家 / 店铺）、接口调用数、错误、eBay 实际能取到的最早时间、各店铺进度")


class BaselineConversation(Base):
    __tablename__ = "baseline_conversations"
    __table_args__ = (UniqueConstraint("store_id", "ebay_conversation_id"),
                      {"comment": "上线前基线会话：从 eBay 补拉的纯人工时期买家会话，只用于统计（上线效果页的基线对比），不进工作台和客户资料；随 BASELINE_RETENTION_DAYS 清理"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    ebay_conversation_id: Mapped[str] = mapped_column(String(128), comment="eBay 会话 ID（同店铺唯一）")
    buyer_key: Mapped[str | None] = mapped_column(String(128), index=True, comment="买家 eBay 用户名（小写）")
    item_id: Mapped[str | None] = mapped_column(String(64), comment="关联的刊登号（会话关联刊登时）")
    conversation_type: Mapped[str] = mapped_column(String(16), default="FROM_MEMBERS", comment="会话类型：只存 FROM_MEMBERS 买家会话")
    first_message_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, comment="基线时间段内第一条消息的时间（UTC）")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, comment="基线时间段内最后一条消息的时间（UTC）")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="最近一次从 eBay 拉取的时间（UTC）")
    batch_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="最近一次写入它的补拉批次（baseline_batches.id）")


class BaselineMessage(Base):
    __tablename__ = "baseline_messages"
    __table_args__ = (UniqueConstraint("store_id", "ebay_message_id"),
                      {"comment": "上线前基线消息：基线会话里时间段内的每条消息（买家 / 店铺同事在 eBay 后台的人工回复），只用于统计；正文是买家个人数据，只给管理员看，随 BASELINE_RETENTION_DAYS 清理"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    conversation_id: Mapped[int] = mapped_column(ForeignKey("baseline_conversations.id"), index=True,
                                                 comment="所属基线会话（baseline_conversations.id）")
    ebay_message_id: Mapped[str] = mapped_column(String(128), comment="eBay 消息 ID（同店铺唯一，重复拉取不重复写）")
    sender_role: Mapped[str] = mapped_column(String(8), comment="发送方：BUYER 买家 / SELLER 店铺（店铺自己的 eBay 账号）")
    sent_at: Mapped[datetime] = mapped_column(DateTime, index=True, comment="在 eBay 的发送时间（UTC）")
    body: Mapped[str | None] = mapped_column(Text, comment="正文纯文本（HTML 已转文字）：用于判断致谢、以后抽样标注意图")
    batch_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="写入它的补拉批次（baseline_batches.id）")


class BaselineSnapshot(Base):
    __tablename__ = "baseline_metric_snapshots"
    __table_args__ = (UniqueConstraint("batch_key", "store_id"),
                      {"comment": "上线前基线统计快照（上线效果页的基线对比优先读它）：按补拉批次 + 店铺存算好的各项指标值，"
                                  "不存买家正文；基线原始消息按 BASELINE_RETENTION_DAYS 清理后仍能对比"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    batch_key: Mapped[str] = mapped_column(String(64), comment="参与统计的完成批次号（baseline_batches.id，多个用逗号，如 1,2）")
    store_id: Mapped[str] = mapped_column(String(64), comment="店铺 ID；* = 全部店铺合计")
    period_start: Mapped[datetime] = mapped_column(DateTime, comment="基线时间段起点（UTC，含）")
    period_end: Mapped[datetime] = mapped_column(DateTime, comment="基线时间段终点（UTC，不含）")
    rules_version: Mapped[str] = mapped_column(String(32), comment="计算口径版本（round_metrics.RULES_VERSION）；口径改了且原始消息还完整时重算")
    message_count: Mapped[int] = mapped_column(Integer, comment="计算时用到的基线消息条数；原始消息被清理后条数对不上，就不再重算")
    observing: Mapped[int] = mapped_column(Integer, default=0, comment="计算时还在观察中的轮次数（追问 / 升级窗口没走完）；大于 0 且原始消息完整时会重算")
    metrics: Mapped[dict] = mapped_column(JSON, comment="各项指标值（轮次、分时段首次响应、按时、追问、升级、北极星、夜间积压、消息量），不含正文")
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="计算时间（UTC）")


# ---------------------------------------------------------------- 订单售后原因最终分类
class OrderAftersalesReason(Base):
    __tablename__ = "order_aftersales_reasons"
    __table_args__ = (UniqueConstraint("store_id", "order_id"),
                      {"comment": "订单维度的售后原因最终分类：一张有售后信号（退款、退货、咨询、纠纷、付款纠纷、取消、转售后、飞书工单、"
                                  "异常包裹、查件单）的订单一行；AI 按规则自动分类，人工可改（人工优先），用于按原因、SKU 统计退款率"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    store_id: Mapped[str] = mapped_column(String(64), index=True, comment="店铺 ID")
    order_id: Mapped[str] = mapped_column(String(64), comment="eBay 订单号（新格式，与 ebay_orders.order_id 一致）")
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="主要关联的买家会话（对话 ID，点了直接打开）")
    conversation_ids: Mapped[list | None] = mapped_column(JSON, comment="同一订单关联的全部买家会话 ID")
    baseline_conversation_ids: Mapped[list | None] = mapped_column(
        JSON, comment="上线前补拉的历史会话（baseline_conversations.id，和工作台会话不是同一套 ID）：按订单号或买家 + 刊登对上唯一订单的")
    buyer_key: Mapped[str | None] = mapped_column(String(128), index=True, comment="买家 eBay 用户名（小写）")
    marketplace_id: Mapped[str | None] = mapped_column(String(32), comment="站点（会话的站点，没有时用店铺默认站点）")
    skus: Mapped[str | None] = mapped_column(String(500), comment="订单商品 SKU（多个用 + 连接）")
    item_ids: Mapped[str | None] = mapped_column(String(300), comment="订单商品的刊登号（逗号分隔）")
    order_created_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, comment="下单时间（UTC）")
    order_total: Mapped[float | None] = mapped_column(Float, comment="订单总额")
    currency: Mapped[str | None] = mapped_column(String(8), comment="订单币种")
    refunded: Mapped[bool] = mapped_column(Boolean, default=False, index=True, comment="是否有退款（订单标记退款或有 eBay 退款单）")
    refund_amount: Mapped[float | None] = mapped_column(Float, comment="退款金额合计（eBay 退款单金额的绝对值之和）")
    refund_currency: Mapped[str | None] = mapped_column(String(8), comment="退款币种")
    refund_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最近一次退款时间（UTC）")
    events: Mapped[list | None] = mapped_column(JSON, comment="这张订单的售后事件：[{kind 类型, id 编号, reason 原因, status 状态, at 时间}]，"
                                                           "含退货 / 咨询 / 纠纷 / 付款纠纷 / 取消 / 退款单")
    ai_reason: Mapped[str | None] = mapped_column(String(16), comment="AI（规则）判断的原因代码：PRODUCT / FITMENT / WAREHOUSE / LISTING / CUSTOMER / LOGISTICS；空 = 判不出来")
    ai_note: Mapped[str | None] = mapped_column(String(300), comment="AI 说明：判断理由或缺什么信息")
    ai_basis: Mapped[list | None] = mapped_column(JSON, comment="AI 判断依据（按优先级），如 RETURN:DOES_NOT_FIT、INTENT:quality_issue、FEISHU:质量问题、PARCEL:断更")
    ai_at: Mapped[datetime | None] = mapped_column(DateTime, comment="AI 最近一次分类时间（UTC）")
    manual_reason: Mapped[str | None] = mapped_column(String(16), index=True, comment="人工标记的原因代码（AI 永不覆盖）")
    manual_note: Mapped[str | None] = mapped_column(String(500), comment="人工备注（“产品问题 XXXXX”里空格后面的部分）")
    manual_by: Mapped[str | None] = mapped_column(String(64), comment="人工标记人")
    manual_at: Mapped[datetime | None] = mapped_column(DateTime, comment="人工标记时间（UTC）")
    final_reason: Mapped[str | None] = mapped_column(String(16), index=True, comment="最终原因：人工优先，其次 AI，都没有为空")
    status: Mapped[str] = mapped_column(String(16), default="UNCLASSIFIED", index=True,
                                        comment="UNCLASSIFIED 未分类（AI 和人工都没标）/ AI 规则自动分类 / MANUAL 人工标记")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="建立时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="最后更新时间（UTC）")
