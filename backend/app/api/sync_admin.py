"""数据同步页（只有管理员）：各个从 eBay 拉数据的任务的开关、频率、范围、上次运行与报错，立即运行。

开关和频率存在系统配置（runtime_settings，优先于 .env），页面直接用 PUT /internal/admin/settings 保存，改了不用重启。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin
from ..config import get_settings
from ..db import get_db, utcnow
from ..models import AuditEvent, EbaySyncState, Store
from .deps import iso

router = APIRouter(prefix="/internal/admin/sync", tags=["sync-admin"], dependencies=[Depends(require_admin)])


def _live_stores(db: Session) -> list[Store]:
    from ..sending.channel import is_live_store

    return [s for s in db.scalars(select(Store).order_by(Store.name)) if is_live_store(s)]


def _states(db: Session, ctype: str, store_ids: list[str] | None = None) -> list[dict]:
    names = dict(db.execute(select(Store.id, Store.name)).all())
    q = select(EbaySyncState).where(EbaySyncState.conversation_type == ctype)
    if store_ids is not None:
        q = q.where(EbaySyncState.store_id.in_(store_ids))
    return [{"store_id": r.store_id, "store_name": names.get(r.store_id) or r.store_id,
             "last_started_at": iso(r.last_started_at), "last_success_at": iso(r.last_success_at),
             "last_error": r.last_error, "stats": r.last_stats}
            for r in sorted(db.scalars(q), key=lambda x: names.get(x.store_id) or x.store_id)]


@router.get("")
def sync_status(db: Session = Depends(get_db)):
    from ..ebay.notifications import status as push_status
    from ..ebay.stores import ebay_not_ready_reasons
    from ..ebay_cases import worker as cases_worker
    from ..models import EbayCase, EbayOrder, ShipNotice
    from ..security.store_credentials import ebay_store_rows
    from ..ebay.scheduler import scheduler as ebay_scheduler
    from ..ship_notices import worker as ship_notice_worker

    s = get_settings()
    live = _live_stores(db)
    all_ebay = len(ebay_store_rows(db))
    live_ids = [x.id for x in live]
    from ..ebay.stores import ebay_store_targets

    pulled = [t.store_id for t in ebay_store_targets()]  # 消息实际拉取的店铺（EBAY_SYNC_STORES，* = 全部）
    msg_states = _states(db, "FROM_MEMBERS", pulled)
    sys_states = {r["store_id"]: r for r in _states(db, "FROM_EBAY", pulled)}
    for r in msg_states:
        r["system_last_success_at"] = (sys_states.get(r["store_id"]) or {}).get("last_success_at")
    order_states = _states(db, "ORDERS")
    last_ship = db.scalar(select(func.max(ShipNotice.created_at)))
    from ..data_sources import modules
    from ..ebay import usage_meter

    calls = usage_meter.snapshot()
    mods = modules()
    for m in mods:  # 每个接口带上调用次数（同一个接口可能被几个模块共用，次数是这个接口的总数）
        for api in m["apis"]:
            api["calls"] = calls.get(api["key"]) if api.get("key") else None
    return {
        "modules": mods,
        "calls": [{"key": k, **v} for k, v in sorted(calls.items(), key=lambda kv: -kv[1]["today"])],
        "ebay_ready": not ebay_not_ready_reasons(), "ebay_not_ready": ebay_not_ready_reasons(),
        "live_stores": [{"id": x.id, "name": x.name, "marketplace_id": x.default_marketplace_id} for x in live],
        "all_ebay_stores": all_ebay,
        "tasks": [
            {"key": "messages", "label": "买家消息 · 定时拉取",
             "desc": "按间隔从 eBay 拉买家消息和 eBay 系统消息（有实时推送时作兜底）；拉进来的非对接店铺，发送只记在本地、不发到 eBay",
             "enabled": s.ebay_sync_enabled, "interval": f"每 {max(60, s.ebay_sync_interval_seconds)} 秒",
             "scope": f"拉取 {len(pulled)} 个店铺（真实发送限对接的 {len(live)} 个）", "running": ebay_scheduler.running_now,
             "worker_alive": ebay_scheduler.running, "last_at": iso(ebay_scheduler.last_at),
             "last": _msg_summary(ebay_scheduler.last_result),
             "settings": ["ebay_sync_enabled", "ebay_sync_stores", "ebay_sync_interval_seconds", "ebay_sync_lookback_days", "ebay_live_stores"],
             "stores": msg_states},
            {"key": "push", "label": "买家消息 · 实时推送",
             "desc": "买家来信时 eBay 马上推过来，系统立刻拉这个店铺（只拉对接店铺，其余只记录）",
             "enabled": s.ebay_push_enabled, "interval": "来信即拉", "scope": f"订阅 {all_ebay} 个店铺，拉取对接的 {len(live)} 个",
             "running": False, "last_at": None, "settings": ["ebay_push_enabled"], "push": push_status(db)},
            {"key": "cases", "label": "售后管理 + 客户订单",
             "desc": "咨询、纠纷、退货、付款纠纷、评价、取消交易、退款单，以及客户列表用的订单（只读）",
             "enabled": s.ebay_cases_sync_minutes > 0,
             "interval": f"每 {s.ebay_cases_sync_minutes} 分钟" if s.ebay_cases_sync_minutes > 0 else "已暂停",
             "scope": f"全部 {all_ebay} 个 eBay 店铺", "running": cases_worker.running_now, "worker_alive": bool(
                 cases_worker._thread and cases_worker._thread.is_alive()),
             "last_at": iso(cases_worker.last_at), "last": _cases_summary(cases_worker.last),
             "settings": ["ebay_cases_sync_minutes", "orders_sync_days"],
             "totals": {"cases": db.scalar(select(func.count(EbayCase.id))) or 0,
                        "orders": db.scalar(select(func.count(EbayOrder.id))) or 0},
             "stores": order_states},
            _best_offer_task(s, all_ebay, len(live)),
            {"key": "ship_notices", "label": "发货通知汇总",
             "desc": "汇总已发货的订单，生成待审核的发货通知（不自动发送）",
             "enabled": s.ship_notice_enabled, "interval": f"每 {s.ship_notice_interval_minutes} 分钟",
             "scope": f"对接的 {len(live)} 个店铺" if s.ship_notice_live_only else f"全部 {all_ebay} 个 eBay 店铺",
             "running": False, "worker_alive": ship_notice_worker.running, "last_at": iso(ship_notice_worker.last_at),
             "last": ship_notice_worker.last if isinstance(ship_notice_worker.last, dict) else None,
             "last_created_at": iso(last_ship),
             "settings": ["ship_notice_enabled", "ship_notice_interval_minutes", "ship_notice_live_only"]},
        ],
    }


def _best_offer_task(s, all_ebay: int, live: int) -> dict:
    from ..best_offers import worker as bo

    last = bo.last or None
    return {"key": "best_offers", "label": "Best offer（买家报价）", "desc": "拉全部店铺处理中的买家报价（只读）；回应只限对接店铺",
            "enabled": s.best_offer_sync_minutes > 0,
            "interval": f"每 {s.best_offer_sync_minutes} 分钟" if s.best_offer_sync_minutes > 0 else "已暂停",
            "scope": f"全部 {all_ebay} 个 eBay 店铺（回应限对接的 {live} 个）", "running": bo.running_now, "worker_alive": bo.running,
            "last_at": iso(bo.last_at),
            "last": last and {"ok": not last.get("error") and not last.get("errors"), "error": last.get("error"), "stores": last.get("stores"),
                              "new": last.get("new"), "offers": last.get("offers"), "failed": last.get("errors") or {}},
            "settings": ["best_offer_sync_minutes"]}


def _msg_summary(r: dict | None) -> dict | None:
    if not r:
        return None
    stores = r.get("stores") or {}
    new = sum(v.get("messages_new", 0) for st in stores.values() for v in (st.get("types") or {}).values())
    failed = {st.get("name") or sid: next((v.get("error") for v in (st.get("types") or {}).values() if not v.get("ok")), None)
              for sid, st in stores.items() if not st.get("ok")}
    return {"ok": r.get("ok"), "error": r.get("error"), "stores": len(stores), "messages_new": new, "failed": failed}


def _cases_summary(r: dict | None) -> dict | None:
    if not r:
        return None
    return {"ok": not r.get("error") and not r.get("errors"), "error": r.get("error"), "stores": r.get("stores"),
            "new": r.get("new"), "changed": r.get("changed"), "orders_new": r.get("orders_new"),
            "failed": {k: list(v)[:3] for k, v in (r.get("errors") or {}).items()}}


@router.post("/{key}/run")
def run_now(key: str, request: Request, db: Session = Depends(get_db)):
    """立即运行一次（后台进行，页面几秒后刷新看结果）。关着的任务也能手动跑一次。"""
    import threading

    from ..ebay_cases import worker as cases_worker
    from ..ebay.scheduler import scheduler as ebay_scheduler
    from ..ship_notices import worker as ship_notice_worker

    if key == "messages":
        started = ebay_scheduler.run_in_background()
    elif key == "cases":
        started = cases_worker.run_in_background()
    elif key == "best_offers":
        from ..best_offers import worker as bo

        started = bo.run_in_background()
    elif key == "ship_notices":
        threading.Thread(target=ship_notice_worker.run_now, name="ship-notices-now", daemon=True).start()
        started = True
    else:
        raise HTTPException(404, "没有这个任务")
    db.add(AuditEvent(actor=current_user(request).display_name[:64], action="SYNC_RUN_NOW", detail={"task": key, "started": started}))
    db.commit()
    return {"started": started, "at": iso(utcnow())}
