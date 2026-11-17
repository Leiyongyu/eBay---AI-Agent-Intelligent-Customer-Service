"""eBay 接口调用计数（进程内）：每类接口今天、本次启动以来的调用次数、失败次数、最近一次时间与报错。

只在几个统一的调用入口记录（只读查询 EbayReadOnlyApi、Message API、sendMessage、apiz 主机、Browse、议价），
数据同步页按功能模块展示。重启后从零开始；独立运行的脚本（如手动补拉）不在这里统计。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime

_lock = threading.Lock()
_stats: dict[str, dict] = {}
_ID = re.compile(r"/(?=[^/]*\d)[^/]+")  # 带数字的路径段（订单号、会话 ID、退货号等）→ /{id}


def key_for(method: str, path: str) -> str:
    """接口统计键：REST 为“GET /sell/fulfillment/v1/order/{id}”，Trading 为“Trading GetItem”。"""
    if method == "TRADING":
        return f"Trading {path}"
    p = path.split("?")[0]
    p = _ID.sub(lambda m: m.group(0) if re.fullmatch(r"/v\d+", m.group(0)) else "/{id}", p)  # 版本号 v1 / v2 保留
    return f"{method} {p}"


def record(key: str, ok: bool, error: str | None = None) -> None:
    now = datetime.now()
    today = now.date().isoformat()
    with _lock:
        s = _stats.setdefault(key, {"day": today, "today": 0, "today_fail": 0, "total": 0, "fail": 0,
                                    "last_at": None, "last_error": None, "last_error_at": None})
        if s["day"] != today:
            s.update(day=today, today=0, today_fail=0)
        s["today"] += 1
        s["total"] += 1
        s["last_at"] = now.isoformat(timespec="seconds")
        if not ok:
            s["today_fail"] += 1
            s["fail"] += 1
            s["last_error"], s["last_error_at"] = (error or "")[:300], now.isoformat(timespec="seconds")


def snapshot() -> dict[str, dict]:
    today = datetime.now().date().isoformat()
    with _lock:
        out = {}
        for k, s in _stats.items():
            v = dict(s)
            if v["day"] != today:
                v.update(today=0, today_fail=0)
            out[k] = v
        return out


def reset() -> None:
    with _lock:
        _stats.clear()


# ------------------------------------------------------------------ 挂到统一的调用入口上
_installed = False


def _metered(fn, key_of):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = key_of(*args, **kwargs)
        try:
            out = fn(*args, **kwargs)
        except Exception as e:
            record(key, False, f"{type(e).__name__}: {e}")
            raise
        record(key, True)
        return out

    wrapper.__metered__ = True
    return wrapper


def install() -> None:
    """给 eBay 调用入口加上计数（幂等）。在 create_app 里调用一次。"""
    global _installed
    if _installed:
        return
    from ..sources import ebay as src
    from .. import ebay_cases
    from . import browse_api, message_api

    api = src.EbayReadOnlyApi
    api.get = _metered(api.get, lambda self, path, *a, **k: key_for("GET", path))
    api.trading = _metered(api.trading, lambda self, call, *a, **k: key_for("TRADING", call))
    mc = message_api.MessageApiClient
    mc._get = _metered(mc._get, lambda self, path, *a, **k: key_for("GET", "/commerce/message/v1" + path))
    message_api.send_message = _metered(message_api.send_message,
                                        lambda *a, **k: "POST /commerce/message/v1/send_message")
    ebay_cases._apiz_get = _metered(ebay_cases._apiz_get, lambda tokens, path, *a, **k: key_for("GET", path))
    bc = browse_api.BrowseClient if hasattr(browse_api, "BrowseClient") else None
    if bc is not None and hasattr(bc, "get_item_by_legacy_id"):
        bc.get_item_by_legacy_id = _metered(bc.get_item_by_legacy_id,
                                            lambda *a, **k: "GET /buy/browse/v1/item/get_item_by_legacy_id")
    from .. import best_offers

    best_offers.respond = _metered(best_offers.respond, lambda *a, **k: "Trading RespondToBestOffer")
    try:
        from .. import offers

        offers.find_eligible = _metered(offers.find_eligible, lambda *a, **k: "GET /sell/negotiation/v1/find_eligible_items")
        offers.send = _metered(offers.send, lambda *a, **k: "POST /sell/negotiation/v1/send_offer_to_interested_buyers")
    except Exception:  # noqa: BLE001
        pass
    _installed = True
