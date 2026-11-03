"""定时拉取 eBay 消息（第 9.3 节“补偿拉取”，默认每 5 分钟）。凭证不齐或 EBAY_SYNC_ENABLED=false 时不运行。"""
from __future__ import annotations

import logging
import threading

from ..config import get_settings
from .sync import EbayMessageSync

log = logging.getLogger(__name__)


class EbaySyncScheduler:
    def __init__(self):
        self._stop = threading.Event()
        self._now = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: dict | None = None

    def start(self) -> bool:
        s = get_settings()
        from .stores import ebay_not_ready_reasons

        if not s.ebay_sync_enabled or ebay_not_ready_reasons():
            return False
        self._thread = threading.Thread(target=self._loop, name="ebay-sync", daemon=True)
        self._thread.start()
        return True

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stop(self) -> None:
        self._stop.set()
        self._now.set()

    def trigger(self) -> None:
        self._now.set()

    def _loop(self) -> None:
        interval = max(60, get_settings().ebay_sync_interval_seconds)
        while not self._stop.is_set():
            try:
                self.last_result = EbayMessageSync().run()
                log.info("eBay 同步：%s", _summary(self.last_result))
            except Exception:
                log.exception("eBay 同步异常")
            self._now.wait(interval)
            self._now.clear()


def _summary(r: dict) -> str:
    if not r.get("ok") and "stores" not in r:
        return r.get("error", "")
    stores = r.get("stores", {})
    new = sum(v.get("messages_new", 0) for st in stores.values() for v in st["types"].values())
    failed = [f"{st['name']}：{next((v.get('error') for v in st['types'].values() if not v.get('ok')), '')[:80]}"
              for st in stores.values() if not st["ok"]]
    return f"{len(stores)} 个店铺，新消息 {new} 条" + (f"；失败 {len(failed)} 个：{'；'.join(failed[:3])}" if failed else "")


scheduler = EbaySyncScheduler()
