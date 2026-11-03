"""数据表（开发方案第 13 章的首期子集）。

字段名尽量与方案保持一致；尚未用到的表（outbox_events、history_links、conversation_summaries 等）
等对应功能落地时再加，避免空表误导。

约定：每张表、每个字段都必须写 comment（中文），PostgreSQL 启动时会同步成 COMMENT ON，
tests/test_models.py 会检查漏写。时间字段一律存无时区的 UTC 时间。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    TYPES = ["退货", "退款", "赔偿/部分退款", "不适配", "破损/错发/缺件", "质量问题", "物流异常/未收到", "物流超时需判断补发", "取消订单",
             "改地址", "其他"]
    # AI 识别的意图 → 标记时默认选中的类型（只是建议，客服可改）
    INTENT_HINT = {"return_refund": "退货", "refund_status": "退款", "damaged_wrong": "破损/错发/缺件",
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
    platform_read: Mapped[bool | None] = mapped_column(
        Boolean, comment="入库时 eBay 侧是否已读（readStatus）；之后的变化以会话的 platform_unread_count 为准")


class MessageTag(Base):
    __tablename__ = "message_tags"
    __table_args__ = {"comment": "会话标记的可选项（管理员维护），如 查适配中、不适配、等买家回复"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    name: Mapped[str] = mapped_column(String(32), unique=True, comment="标记名称")
    color: Mapped[str] = mapped_column(String(16), default="primary",
                                       comment="颜色：primary 蓝 / success 绿 / warning 橙 / danger 红 / info 灰")
    sort: Mapped[int] = mapped_column(Integer, default=100, comment="排序，小的在前")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否可选；停用后已有标记照常显示，不能再选")
    created_by: Mapped[str | None] = mapped_column(String(64), comment="创建人")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")


class ConversationMark(Base):
    __tablename__ = "conversation_marks"
    __table_args__ = {"comment": "会话标记：客服给整段会话标当前状态（如 查适配中、不适配）并写备注；一个会话一个标记"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="主键")
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), unique=True, comment="被标记的会话 ID")
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间（UTC）")


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
    __table_args__ = {"comment": "公司知识条款：政策/FAQ/SOP/话术，按条款存储，带适用范围、可见级别、版本和生效期"}

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
    source: Mapped[str | None] = mapped_column(String(64), comment="资料来源部门或渠道")
    content_hash: Mapped[str] = mapped_column(String(64), comment="正文 SHA-256，导入时据此判断是否变化")
    import_id: Mapped[int | None] = mapped_column(Integer, index=True,
                                                  comment="来自哪次 Excel 上传（knowledge_imports.id）；空表示由 JSONL 文件导入")
    search_text: Mapped[str | None] = mapped_column(
        Text, comment="全文检索用的分词结果（jieba 中文分词 + 英德文小写词，空格分隔），对它建 PostgreSQL 全文索引")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, comment="最后更新时间（UTC）")


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
    origin: Mapped[str | None] = mapped_column(String(16), comment="来源：空/SYNC 从谷仓同步；AI_DRAFT 系统按物流规则保存的草稿")
    io_desc: Mapped[str | None] = mapped_column(String(300), comment="查件需求（系统建草稿时写的说明）")
    created_by: Mapped[str | None] = mapped_column(String(64), comment="建单人（系统建的为 AI（物流规则））")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, comment="系统建草稿的时间（UTC）")
