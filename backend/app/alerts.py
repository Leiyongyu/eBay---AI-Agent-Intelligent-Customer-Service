"""飞书告警（统一出口）：模型熔断 / 恢复、预算超标，以及之后的积压、eBay 403、发件箱失败、待审核超时等。

- 总开关 ALERTS_ENABLED（默认关）；关着时只记日志，不发；
- 只发一个群：ALERT_CHAT_ID，空 = 售后测试群（FEISHU_CASE_CHAT_ID）；
- 去重：同一个 key（默认 kind）ALERT_DEDUPE_MINUTES（默认 30）分钟内只发一次；
- 发送失败只记日志，绝不影响业务流程。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from .config import get_settings

log = logging.getLogger(__name__)

_last: dict[str, float] = {}
_lock = threading.Lock()
sent: list[dict] = []  # 最近发过（或本该发）的告警，系统状态页 / 测试用，最多 50 条


def _card(title: str, text: str, level: str, trace_id: str | None = None) -> dict:
    color = {"error": "red", "warning": "orange", "info": "blue", "ok": "green"}.get(level, "blue")
    elements = [{"tag": "markdown", "content": text[:2000]}]
    if trace_id:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": f"追踪号 {trace_id}"}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def notify(kind: str, title: str, text: str, *, level: str = "warning", key: str | None = None) -> bool:
    """发一条告警；返回是否真的发出。"""
    from .observability import current_trace_id

    s = get_settings()
    k = key or kind
    trace_id = current_trace_id()
    now = time.monotonic()
    with _lock:
        last = _last.get(k)
        if last is not None and now - last < max(0, s.alert_dedupe_minutes) * 60:
            return False
        _last[k] = now
        sent.append({"kind": kind, "key": k, "title": title, "text": text[:500], "level": level, "trace_id": trace_id,
                     "at": datetime.now().isoformat(timespec="seconds"), "delivered": False})
        del sent[:-50]
    log.warning("告警[%s] %s：%s", kind, title, text[:300])
    chat = s.alert_chat_id or s.feishu_case_chat_id
    if not s.alerts_enabled or not chat or not (s.feishu_app_id and s.feishu_app_secret):
        return False
    try:
        from .feishu.client import FeishuClient

        FeishuClient(s).send_card(chat, _card(title, text, level, trace_id))
        sent[-1]["delivered"] = True
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("飞书告警发送失败：%s", e)
        return False


def reset() -> None:
    """测试用：清空去重记录。"""
    with _lock:
        _last.clear()
        sent.clear()
