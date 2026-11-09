"""定时拉取 eBay 消息（第 9.3 节“补偿拉取”，默认每 5 分钟）。凭证不齐时不启动；
EBAY_SYNC_ENABLED、EBAY_SYNC_INTERVAL_SECONDS 每轮都重新读（数据同步页上改了不用重启）。"""
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
        self.last_at = None
        self.running_now = False

    def start(self) -> bool:
        from .stores import ebay_not_ready_reasons

        if ebay_not_ready_reasons():
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

    def run_once(self, force: bool = False) -> dict | None:
        """拉一轮；关着时只有 force（数据同步页“立即运行”）才拉。"""
        from ..db import utcnow

        if not force and not get_settings().ebay_sync_enabled:
            return None
        self.running_now = True
        try:
            self.last_result = EbayMessageSync().run()
            log.info("eBay 同步：%s", _summary(self.last_result))
        except Exception as e:
            log.exception("eBay 同步异常")
            self.last_result = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        finally:
            self.last_at = utcnow()
            self.running_now = False
        return self.last_result

    def run_in_background(self) -> bool:
        if self.running_now:
            return False
        threading.Thread(target=self.run_once, kwargs={"force": True}, name="ebay-sync-now", daemon=True).start()
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._now.wait(max(60, get_settings().ebay_sync_interval_seconds))
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
