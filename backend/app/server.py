"""FastAPI 应用：API + 工作台静态页面 + 进程内 Worker。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import APP_NAME, APP_VERSION
from .api import accounts, aftersales_reasons, best_offers, aftersales_cases, aftersales_files, customers, ebay, ebay_cases, internal_refs, kb_docs, order_extras, parcel_followups, upload_public, invoices, knowledge_admin, maintenance, message_templates, offers, performance, public_media, settings_admin, ship_notices, sync_admin, system, usage, webhooks, workbench
from .auth import auth_middleware, require_admin
from .config import PROJECT_ROOT, get_settings
from .aftersales_mail.service import worker as aftersales_mail_worker
from .logistics.tickets import worker as logistics_ticket_worker
from .review_ack import worker as review_ack_worker
from .aftersales_mail.message_links import worker as message_link_worker
from .translation import translator
from .ship_notices import worker as ship_notice_worker
from .feishu.cases import worker as feishu_case_worker
from .ebay.notifications import push_not_ready_reasons, push_worker, recheck_destination
from .ebay.scheduler import scheduler as ebay_scheduler
from .retention import scheduler as retention_scheduler
from .seed import bootstrap
from .tunnel import not_ready_reasons as tunnel_not_ready, tunnel
from .worker import worker

log = logging.getLogger(__name__)

FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    report = bootstrap()
    log.info("初始化完成：%s", report)
    log.info("数据库：%s；Redis：%s", s.database_display, s.redis_url or "未配置")
    reasons = s.agent_not_ready_reasons()
    if reasons:
        log.warning("AGENT_NOT_READY：%s（消息接入与人工队列照常可用）", "；".join(reasons))
    worker.start()
    if ebay_scheduler.start():
        from .ebay.stores import ebay_store_targets

        log.info("eBay 消息定时拉取已启动：%s 个店铺，每 %s 秒（只读）", len(ebay_store_targets()),
                 s.ebay_sync_interval_seconds)
    elif s.ebay_client_id or s.ebay_refresh_token:
        from .ebay.stores import ebay_not_ready_reasons

        log.warning("eBay 消息拉取未启动：%s", "；".join(ebay_not_ready_reasons()) or "EBAY_SYNC_ENABLED=false")
    # Cloudflare 隧道跟着本程序启停（.env 填了 CLOUDFLARED_PATH 和令牌才启动）
    if not tunnel.start() and (s.cloudflared_path or s.cloudflare_tunnel_token):
        log.warning("Cloudflare 隧道未启动：%s", "；".join(tunnel_not_ready()))
    if not push_not_ready_reasons():  # 开关（EBAY_PUSH_ENABLED）每条推送都读，数据同步页上改了不用重启
        push_worker.start()  # 推送触发的单店铺拉取；订阅在状态页“启用推送订阅”里建
        log.info("eBay 实时推送接收已就绪：%s", s.ebay_notify_endpoint)
        if tunnel.running:
            recheck_destination()
    if retention_scheduler.start():
        log.info("聊天记录保留期清理已启动：保留 %s 天，每天 %s%s", s.data_retention_days, s.retention_cleanup_time,
                 "（试运行，只统计不删除）" if s.retention_dry_run else "")
    if aftersales_mail_worker.start():  # 售后邮箱收信与每日清理（邮箱没配好时只做清理）
        log.info("售后邮箱任务已启动：%s", "；".join(s.aftersales_mail_not_ready_reasons()) or "每 %s 秒收信" % s.aftersales_mail_poll_seconds)
    if ship_notice_worker.start():  # 汇总当天已发货的订单，生成待审核的发货通知
        log.info("发货通知汇总已启动：每 %s 分钟", s.ship_notice_interval_minutes)
    if feishu_case_worker.start():  # 读飞书售后群里对工单的回复
        log.info("飞书售后群工单已启动：每 %s 秒读一次回复", s.feishu_case_poll_seconds)
    from .ebay_cases import worker as ebay_cases_worker

    from .best_offers import worker as best_offer_worker

    if best_offer_worker.start():  # Best offer：定时拉买家报价（只读）
        log.info("Best offer 同步已启动：每 %s 分钟", s.best_offer_sync_minutes)
    if ebay_cases_worker.start():  # 售后管理：定时拉 eBay 的咨询、纠纷、退货、付款纠纷、评价、取消、退款单（只读）
        log.info("售后管理同步已启动：每 %s 分钟", s.ebay_cases_sync_minutes)
    if translator.start():  # 买家来信自动译成中文
        log.info("买家消息自动翻译已启动")
    if logistics_ticket_worker.start():  # 每天同步谷仓查件单，谷仓回复后起草给买家的回复建议
        log.info("谷仓查件单同步已启动：每天 %s", s.logistics_ticket_sync_time)
    if review_ack_worker.start():  # 事事有回应：转人工的来信超时没人回复，先给买家发“已收到”
        log.info("转人工回执已启动：来信 %s 分钟没人回复就先回应买家", s.review_ack_delay_minutes)
    if message_link_worker.start():  # 买家消息里的 WeTransfer 视频链接：自动下载进售后附件
        log.info("买家消息里的视频链接自动下载已启动")
    yield
    message_link_worker.stop()
    best_offer_worker.stop()
    ebay_cases_worker.stop()
    review_ack_worker.stop()
    logistics_ticket_worker.stop()
    translator.stop()
    ship_notice_worker.stop()
    feishu_case_worker.stop()
    aftersales_mail_worker.stop()
    retention_scheduler.stop()
    push_worker.stop()
    tunnel.stop()
    ebay_scheduler.stop()
    worker.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="eBay 智能客服 Agent", version=APP_VERSION, lifespan=lifespan)
    from .ebay import usage_meter

    usage_meter.install()  # eBay 接口调用计数（数据同步页按功能模块展示）
    app.middleware("http")(auth_middleware)  # /api、/internal 一律要求登录（健康检查、登录除外）
    app.include_router(accounts.router)
    app.include_router(system.router)
    app.include_router(workbench.router)
    app.include_router(ship_notices.router)  # 发货通知：按负责店铺
    app.include_router(aftersales_cases.router)  # 飞书售后群工单：按负责店铺
    app.include_router(order_extras.router)  # 会话右侧相关信息的补充（售后、物流、查件、发票、评价、备注）
    app.include_router(internal_refs.router)  # 会话页“内部参考”：按场景列仅内部条款，按负责店铺
    app.include_router(best_offers.router)  # Best offer：买家报价与回应（按店铺范围；回应只限对接店铺）
    app.include_router(customers.router)  # 客户管理：客户列表、黑名单（按店铺范围）
    app.include_router(ebay_cases.router)  # 售后管理（咨询、纠纷、退货等，只读，按店铺范围）
    app.include_router(aftersales_reasons.router)  # 订单售后原因分类：按负责店铺；重建只有管理员
    app.include_router(aftersales_reasons.conv_router)  # 会话侧栏标记售后原因
    app.include_router(parcel_followups.router)  # 异常包裹跟进表（每人只看自己的，管理员看全部）
    app.include_router(invoices.router)  # 会话里的发票：按店铺范围；留底 PDF 只有管理员能看
    app.include_router(aftersales_files.router)  # 售后附件：管理员或账号打开“可看售后附件”，按店铺范围
    app.include_router(upload_public.router)  # 买家上传页 /upload：不需要登录（隧道只放行 /upload 开头）
    app.include_router(invoices.admin)  # 发票留底、开票主体、邮箱测试：只有管理员
    app.include_router(settings_admin.router)
    app.include_router(sync_admin.router)  # 数据同步页：只有管理员  # 系统配置页：只有管理员
    app.include_router(webhooks.router)
    app.include_router(public_media.router)  # 发给买家的附件公网地址（经隧道，签名链接，不需要登录）
    app.include_router(ebay.router, dependencies=[Depends(require_admin)])  # 只有管理员
    app.include_router(knowledge_admin.router)
    app.include_router(kb_docs.router)  # 参考文档库：知识条款按编号引用，只给人看
    app.include_router(maintenance.router, dependencies=[Depends(require_admin)])  # 只有管理员
    app.include_router(usage.router, dependencies=[Depends(require_admin)])  # 只有管理员
    app.include_router(performance.router)  # 绩效统计：按负责店铺
    app.include_router(message_templates.router)  # 消息模板：系统预定义 / 企业 / 个人
    app.include_router(offers.router)  # 议价：给感兴趣的买家发报价（只有真实收发的店铺）

    if (FRONTEND_DIST / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            # 接口路径不存在时返回 JSON 404，不要回首页 HTML（否则前端只会报“不是合法 JSON”，看不出是接口没有）
            if path.split("/", 1)[0] in ("api", "internal", "dev", "webhooks"):
                raise HTTPException(404, f"接口 /{path} 不存在：后端可能还是旧版本，请重启 main.py")
            target = (FRONTEND_DIST / path).resolve()
            if path and target.is_file() and FRONTEND_DIST in target.parents:
                return FileResponse(target)
            # 首页不缓存：前端重新构建后（资源文件名带哈希）普通刷新就能拿到新版
            return FileResponse(FRONTEND_DIST / "index.html", headers={"Cache-Control": "no-cache"})
    else:
        @app.get("/", include_in_schema=False)
        def no_frontend():
            return HTMLResponse(
                f"<h3>{APP_NAME} 后端已启动</h3><p>前端尚未构建：在 frontend 目录执行 npm install &amp;&amp; npm run build，"
                "或保持 .env 中 FRONTEND_AUTO_BUILD=true 后重新运行 main.py。</p><p>接口文档：<a href='/docs'>/docs</a></p>"
            )

    return app
