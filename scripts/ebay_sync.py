"""手动拉取一次 eBay 店铺消息（只读）：PyCharm 里直接 Run 本文件。

读取 .env 中的 EBAY_* 配置，把买家会话（FROM_MEMBERS）和 eBay 系统消息（FROM_EBAY）同步进数据库，
打印每类的会话数、新消息数和接口调用数。不打印令牌，不向 eBay 发送任何内容。
与 main.py 里的定时拉取共用同一套逻辑，重复运行不会产生重复消息。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.config import get_settings  # noqa: E402
from app.ebay.sync import EbayMessageSync  # noqa: E402
from app.seed import bootstrap  # noqa: E402

LABELS = {
    "conversations_listed": "列出的会话",
    "conversations_synced": "有新消息的会话",
    "conversations_unchanged": "无变化的会话",
    "messages_new": "新入库消息",
    "messages_buyer": "其中买家消息",
    "messages_seller": "其中店铺回复",
    "messages_system": "其中 eBay 系统消息",
    "messages_unknown": "其中发送方未知",
}


def main() -> int:
    s = get_settings()
    reasons = s.ebay_not_ready_reasons()
    if reasons:
        print("❌ eBay 配置不完整：" + "；".join(reasons))
        return 1
    bootstrap()
    print(f"开始拉取：店铺 {s.ebay_store_id}（{s.ebay_env}），首次回溯 {s.ebay_sync_lookback_days} 天 ……", flush=True)
    result = EbayMessageSync().run()
    if "types" not in result:
        print("❌ " + result.get("error", "未知错误"))
        return 1
    for ctype, r in result["types"].items():
        title = "买家会话 FROM_MEMBERS" if ctype == "FROM_MEMBERS" else "eBay 系统消息 FROM_EBAY"
        if not r.get("ok"):
            print(f"❌ {title}：{r.get('error')}")
            continue
        details = "，".join(f"{LABELS.get(k, k)} {v}" for k, v in r.items() if k in LABELS and v)
        print(f"✅ {title}（自 {r.get('since')} 起）：{details or '没有新消息'}")
        if r.get("warning"):
            print(f"   ⚠ {r['warning']}")
    print(f"接口调用 {result.get('api_calls')} 次。打开工作台“全部”分组即可查看（真实会话只读，不能从本系统发送）。")
    if not s.ebay_sync_create_reply_jobs:
        print("提示：EBAY_SYNC_CREATE_REPLY_JOBS=false，拉到的消息只入库展示，不会交给 AI 生成草稿。")
    print(json.dumps(result, ensure_ascii=False)[:2000] if "--json" in sys.argv else "", end="")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
