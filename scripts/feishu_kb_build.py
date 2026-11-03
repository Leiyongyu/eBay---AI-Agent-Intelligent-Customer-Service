"""飞书售后群 → 售后知识库整理：PyCharm 里直接 Run 本文件。

读取 scripts/feishu_export.py 导出的群聊（data/feishu_export/<群名>_<chat_id>/），按阶段处理，结果写到
FEISHU_KB_DIR（默认 data/feishu_kb，已在 .gitignore 中）：

    build    拼接案例 + 脱敏          → cases.jsonl（全部案例，含无回复的）
    extract  模型逐案提炼结构化字段   → cases_extracted.jsonl（可续跑，已提炼的跳过）
    clauses  按“产品线 + 问题类型”起草内部知识条款 → clauses_draft.jsonl
    review   生成人工审核 Excel        → review_<时间>.xlsx（每次新文件，不覆盖已填写的）
    import   读取审核后的 Excel，通过/修改的条款导入知识库 → internal_sop.jsonl + 数据库

运行哪些阶段、试点条数等都在根目录 .env 的 FEISHU_KB_* 中配置（模板见 .env.example）。
模型配置沿用 .env 的 LLM_*；发给模型的只有脱敏后的文字，不含图片（第 18.2 节）。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # SystemExit 的提示走 stderr

from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

import feishu_export as fe  # noqa: E402
from app.intents import INTENTS  # noqa: E402

ENV = fe.load_env()
KB_DIR = ROOT / (ENV.get("FEISHU_KB_DIR") or "data/feishu_kb")
STAGES = [s.strip() for s in (ENV.get("FEISHU_KB_STAGES") or "build,extract,clauses,review").split(",") if s.strip()]
LIMIT = int(ENV.get("FEISHU_KB_LIMIT") or 0) or None
PARALLEL = int(ENV.get("FEISHU_KB_PARALLEL") or 4)
TECH_NAMES = {n.strip() for n in (ENV.get("FEISHU_KB_TECH_NAMES") or "").split(",") if n.strip()}
MERGE_DAYS = 30

CASES_FILE = KB_DIR / "cases.jsonl"
EXTRACTED_FILE = KB_DIR / "cases_extracted.jsonl"
CLAUSES_FILE = KB_DIR / "clauses_draft.jsonl"
SOP_FILE = KB_DIR / "internal_sop.jsonl"


# ---------------- 通用 ----------------

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def ensure_salt() -> str:
    """脱敏哈希的盐：.env 未填时自动生成并写回 .env，之后保持不变，保证同一买家始终映射到同一代号。"""
    salt = ENV.get("FEISHU_KB_HASH_SALT")
    if salt:
        return salt
    salt = secrets.token_hex(16)
    env_file = ROOT / ".env"
    text = env_file.read_text(encoding="utf-8")
    if re.search(r"^FEISHU_KB_HASH_SALT=", text, re.M):
        text = re.sub(r"^FEISHU_KB_HASH_SALT=.*$", f"FEISHU_KB_HASH_SALT={salt}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\nFEISHU_KB_HASH_SALT={salt}\n"
    env_file.write_text(text, encoding="utf-8")
    print("已生成脱敏盐 FEISHU_KB_HASH_SALT 并写入 .env（不要修改，否则代号会变）")
    return salt


def source_dir() -> Path:
    export_root = ROOT / (ENV.get("FEISHU_EXPORT_DIR") or "data/feishu_export")
    chat_ids = [c.strip() for c in (ENV.get("FEISHU_CHAT_IDS") or "").split(",") if c.strip()]
    dirs = [d for d in export_root.iterdir() if d.is_dir() and (d / "messages.jsonl").exists()]
    if chat_ids:
        dirs = [d for d in dirs if any(d.name.endswith(c) for c in chat_ids)]
    if len(dirs) != 1:
        raise SystemExit(f"❌ 在 {export_root} 下找到 {len(dirs)} 个群导出目录，请用 FEISHU_CHAT_IDS 指定一个")
    return dirs[0]


# ---------------- 规则识别 ----------------

EBAY_ORDER = re.compile(r"\b\d{2}-\d{5}-\d{5}\b")
AMZ_ORDER = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")
# 其他平台的纯数字订单号（如 15 位的 200014659907608）；OE 号最长 11 位连续数字，不会被误伤
LONG_ORDER = re.compile(r"(?<!\d)\d{12,20}(?!\d)")
NAME_LINE = re.compile(r"^[A-Za-z][A-Za-z.'\-]*(?: [A-Za-z][A-Za-z.'\-]*){1,3}$")
PLATE = re.compile(r"(车牌(?:号)?\s*[:：]?\s*)([A-Za-z0-9\- ]{4,12})")
SKU = re.compile(r"\b([A-Z]{2,4})-\d{4,6}-\d{3,5}\b")
# 不用 \b：群里常见“vin1G1GZ37G5FR130958”这种与前缀粘连的写法
VIN = re.compile(r"(?<![A-HJ-NPR-Z0-9])([A-HJ-NPR-Z0-9]{11})([A-HJ-NPR-Z0-9]{6})(?![A-HJ-NPR-Z0-9])")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# 只认带国际区号的号码和国内手机号；不能用“10 位以上数字”这种宽规则，否则会误伤 OE 号（如 12 61 8638755）
PHONE = re.compile(r"(?<![\w-])\+\d[\d ()\-]{8,}\d(?![\w-])|(?<!\d)1[3-9]\d{9}(?!\d)")
URL = re.compile(r"https?://\S+")
BUYER_LABEL = re.compile(r"(?:买家|客户|顾客)\s*(?:ID|id|账号|名)?\s*[:：]\s*([A-Za-z0-9][\w.\-*]{2,40})")
USERNAME_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-*]{2,40}$")
# 兼容“AMZ-EU 售前”“AMZ/US3 售后”“AMZEU-售后”“eaby欧洲售后”等写法
PLATFORM = re.compile(
    r"(?i)(ebay|eaby|amz|amazon|亚马逊|walmart|沃尔玛)[\s\-_/]*"
    r"(美国|英国|德国|欧洲|澳洲|法国|意大利|西班牙|加拿大|eu|us|uk|de)?\d?[\s\-_/]*(售前|售后)?")
SITE_MAP = {
    "德国": "DE", "de": "DE", "英国": "GB", "uk": "GB", "美国": "US", "us": "US", "澳洲": "AU", "法国": "FR",
    "意大利": "IT", "西班牙": "ES", "加拿大": "CA", "欧洲": "EU", "eu": "EU",
}
NOISE = re.compile(r"^(这个|这单|这个单|麻烦|请|帮忙)?.{0,6}(有回复|回复一下|回复下|看一下|看下|跟进|还没回复|怎么样了|处理一下)"
                   r"[吗了呢吧？?！!。.\s]*$")
PLATFORM_WORDS = re.compile(r"(?i)^(ebay|amz|amazon|售前|售后|eu|us|uk)$")
# 案例首行里不是买家信息的英文词
HEADER_WORDS = {"amz", "amzeu", "amzus", "amzuk", "amzde", "ebay", "eaby", "sku", "order", "orderid", "id", "vin",
                "oe", "com", "www", "http", "https", "europe", "seller", "sellercentral", "buyer", "the", "and",
                "anzeu", "azmeu", "list", "orders", "products", "stock", "nebellampe"}


def is_vin(value: str) -> bool:
    return bool(re.search(r"[A-Z]", value) and re.search(r"\d", value))


def parse_platform(text: str) -> dict:
    for m in PLATFORM.finditer(text):
        raw_platform, raw_site, stage = m.group(1).lower(), (m.group(2) or "").lower(), m.group(3) or ""
        platform = ("ebay" if raw_platform in ("ebay", "eaby")
                    else "walmart" if raw_platform in ("walmart", "沃尔玛") else "amazon")
        site = SITE_MAP.get(raw_site, "")
        marketplace = f"{platform.upper()}_{site}" if site else "UNKNOWN"
        return {"platform": platform, "site": site or "未知", "marketplace_id": marketplace, "stage": stage or "未知"}
    return {"platform": "未知", "site": "未知", "marketplace_id": "UNKNOWN", "stage": "未知"}


def buyer_candidates(text: str) -> set[str]:
    found = set(BUYER_LABEL.findall(text))
    for line in text.splitlines()[:4]:
        token = line.strip().split("@")[0].strip()
        # 买家账号要么够长、要么带数字或 ._-；排除 BMW、FORD 这类全大写的品牌词
        looks_like_user = (len(token) >= 6 or re.search(r"[\d._\-]", token)) and not token.isupper()
        if (looks_like_user and USERNAME_LINE.match(token) and not SKU.search(token) and not EBAY_ORDER.search(token)
                and not AMZ_ORDER.search(token) and not PLATFORM_WORDS.match(token) and not token.isdigit()):
            found.add(token)
        # 单独一行的英文姓名（如“luis cepeda”），排除含平台词的行（如“amz eu”）
        elif NAME_LINE.match(token) and not PLATFORM.search(token) and not SKU.search(token):
            found.add(token)
    # 亚马逊案例常把买家姓名和 SKU、订单号写在同一行（如“AMZEU-售后-LR-110005-0002-SKLATINIOTIS-…”）
    for line in text.splitlines()[:2]:
        if not (SKU.search(line) or EBAY_ORDER.search(line) or AMZ_ORDER.search(line)):
            continue
        for tok in re.split(r"[\s\-/,，:：|]+", line):
            tok = tok.strip(".")
            if (re.fullmatch(r"[A-Za-z][A-Za-z.'_]{2,}", tok) and not (tok.isupper() and len(tok) <= 4)
                    and tok.lower() not in HEADER_WORDS and not re.search(r"(?i)amazon|ebay|seller", tok)):
                found.add(tok)
    return found


class Redactor:
    def __init__(self, salt: str, member_names: dict[str, str]):
        self.salt = salt
        self.people = sorted({n for n in member_names.values() if n}, key=len, reverse=True)

    def token(self, kind: str, value: str) -> str:
        return f"{kind}_{hashlib.sha256((self.salt + kind + value).encode()).hexdigest()[:8]}"

    @lru_cache(maxsize=4)
    def _buyer_pattern(self, buyers: frozenset[str]) -> re.Pattern:
        # 近千个买家标识合成一个正则，比逐个替换快两个数量级；长的在前，避免短名截断长名
        alternation = "|".join(re.escape(b) for b in sorted(buyers, key=len, reverse=True))
        return re.compile(rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z0-9])")

    def role_of(self, name: str) -> str:
        return "技术支持" if name in TECH_NAMES else "客服"

    def redact(self, text: str, buyers: set[str]) -> str:
        text = URL.sub("[链接]", text)
        text = EMAIL.sub("[邮箱]", text)
        text = EBAY_ORDER.sub(lambda m: self.token("ORDER", m.group()), text)
        text = AMZ_ORDER.sub(lambda m: self.token("ORDER", m.group()), text)
        text = LONG_ORDER.sub(lambda m: self.token("ORDER", m.group()), text)
        text = PLATE.sub(lambda m: m.group(1) + "[车牌]", text)
        text = VIN.sub(lambda m: m.group(1) + "******" if is_vin(m.group()) else m.group(), text)
        if buyers:
            text = self._buyer_pattern(frozenset(buyers)).sub(lambda m: self.token("BUYER", m.group()), text)
        text = PHONE.sub(lambda m: m.group() if len(re.sub(r"\D", "", m.group())) < 10 else "[电话]", text)
        text = re.sub(r"@所有人|@_all", "", text)
        for name in self.people:
            text = text.replace(f"@{name}", "").replace(name, self.role_of(name))
        text = re.sub(r"@[^\s@/，,。]{1,12}", "", text)
        return re.sub(r"[ \t]+\n", "\n", text).strip()


# ---------------- 阶段 1：拼接 + 脱敏 ----------------

def strip_attachment_lines(text: str) -> tuple[str, Counter]:
    counts: Counter = Counter()
    kept = []
    for line in text.splitlines():
        s = line.strip()
        tag = re.match(r"\[(图片|视频|语音|表情|文件)\]", s)
        if tag and (s == tag.group() or tag.group(1) in ("文件", "视频")):
            counts[tag.group(1)] += 1
            continue
        n = s.count("[图片]")
        if n:
            counts["图片"] += n
            line = line.replace("[图片]", "")
        kept.append(line)
    return "\n".join(kept).strip(), counts


def stage_build() -> list[dict]:
    src = source_dir()
    messages = read_jsonl(src / "messages.jsonl")
    names: dict[str, str] = json.loads((src / "chat.json").read_text(encoding="utf-8"))["members"]
    redactor = Redactor(ensure_salt(), names)
    files = sorted(p.name for p in (src / "files").iterdir()) if (src / "files").exists() else []
    by_id = {m["message_id"]: m for m in messages}
    live = [m for m in messages if not m.get("deleted") and m.get("msg_type") != "system"]

    def root_of(m: dict) -> str:
        seen = set()
        cur = m
        while cur.get("parent_id") and cur["message_id"] not in seen:
            seen.add(cur["message_id"])
            if cur.get("root_id") and cur["root_id"] in by_id:
                return cur["root_id"]
            parent = by_id.get(cur["parent_id"])
            if not parent:
                return cur["parent_id"]
            cur = parent
        return cur["message_id"]

    threads: dict[str, list[dict]] = defaultdict(list)
    for m in live:
        threads[root_of(m)].append(m)
    roots = [by_id[r] for r in threads if r in by_id and not by_id[r].get("deleted")]

    # 同一订单号、或同一买家 + 同一 SKU、30 天内重新发起的根消息合并为一个案例组
    parent: dict[str, str] = {r["message_id"]: r["message_id"] for r in roots}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    keys: dict[str, list[tuple[int, str]]] = defaultdict(list)
    root_text = {r["message_id"]: fe.readable_text(r, names) for r in roots}
    for r in roots:
        t, rid, ts = root_text[r["message_id"]], r["message_id"], int(r["create_time"])
        for o in EBAY_ORDER.findall(t) + AMZ_ORDER.findall(t):
            keys[f"order:{o}"].append((ts, rid))
        skus = {m.group() for m in SKU.finditer(t)}
        for b in buyer_candidates(t):
            for s in skus:
                keys[f"buyer_sku:{b}|{s}"].append((ts, rid))
    for items in keys.values():
        items.sort()
        for (t1, a), (t2, b) in zip(items, items[1:]):
            if t2 - t1 <= MERGE_DAYS * 86400 * 1000:
                parent[find(b)] = find(a)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in roots:
        groups[find(r["message_id"])].append(r)
    all_buyers = set().union(*(buyer_candidates(t) for t in root_text.values()))

    cases = []
    for head_id, group_roots in groups.items():
        group_roots.sort(key=lambda m: int(m["create_time"]))
        msgs = sorted((m for r in group_roots for m in threads[r["message_id"]]), key=lambda m: int(m["create_time"]))
        all_text = "\n".join(fe.readable_text(m, names) for m in msgs)
        head_text = "\n".join(root_text[r["message_id"]] for r in group_roots)
        buyers = all_buyers  # 同一买家可能在别的案例里被提到，按全群识别出的买家统一替换
        transcript, attachments, noise = [], Counter(), 0
        for m in msgs:
            sender = names.get((m.get("sender") or {}).get("id", ""), "")
            role = redactor.role_of(sender) if sender else "客服"
            body, counts = strip_attachment_lines(fe.readable_text(m, names))
            attachments.update(counts)
            body = redactor.redact(body, buyers)
            is_noise = m["message_id"] not in {r["message_id"] for r in group_roots} and (
                not body or (len(body) <= 20 and not counts and NOISE.match(body.replace(" ", "")) is not None))
            noise += is_noise
            transcript.append({
                "time": datetime.fromtimestamp(int(m["create_time"]) / 1000).strftime("%Y-%m-%d %H:%M"),
                "role": role,
                "text": body,
                "attachments": dict(counts),
                "noise": bool(is_noise),
                "message_id": m["message_id"],
            })
        head = group_roots[0]
        replies = [t for t in transcript if t["message_id"] not in {r["message_id"] for r in group_roots}]
        useful = [t for t in replies if not t["noise"]]
        tech_reply = next((t for t in transcript if t["role"] == "技术支持"
                           and t["message_id"] != head["message_id"] and not t["noise"]), None)
        opened = int(head["create_time"])
        first_reply_minutes = None
        if tech_reply:
            first = next(m for m in msgs if m["message_id"] == tech_reply["message_id"])
            first_reply_minutes = round((int(first["create_time"]) - opened) / 60000)
        skus = sorted({m.group() for m in SKU.finditer(head_text)})
        vins = sorted({m.group(1) + "******" for m in VIN.finditer(all_text) if is_vin(m.group())})
        orders = sorted({redactor.token("ORDER", o) for o in
                         EBAY_ORDER.findall(head_text) + AMZ_ORDER.findall(head_text) + LONG_ORDER.findall(head_text)})
        platform = parse_platform(head_text)
        # 没用“回复”功能的零散发言（如“好的，我催下”）也会成为根消息；没有 SKU、订单号、平台信息的不算案例
        looks_like_case = bool(skus or orders or platform["platform"] != "未知")
        cases.append({
            "case_id": f"FS-{head['message_id'][-10:]}",
            "status": "not_case" if not looks_like_case else ("ok" if useful else "no_reply"),
            "source_message_ids": [m["message_id"] for m in msgs],
            "merged_roots": len(group_roots),
            "opened_at": datetime.fromtimestamp(opened / 1000).isoformat(timespec="minutes"),
            "closed_at": datetime.fromtimestamp(int(msgs[-1]["create_time"]) / 1000).isoformat(timespec="minutes"),
            "first_reply_minutes": first_reply_minutes,
            **platform,
            "sku": skus,
            "product_line": sorted({s.split("-")[0] for s in skus}),
            "order_refs": orders,
            "vin_masked": vins,
            "attachments": dict(attachments),
            "attachment_files": [f for f in files if any(f.startswith(mid + "_") for mid in
                                                          (m["message_id"] for m in msgs))],
            "noise_messages": noise,
            "transcript": transcript,
        })
    cases.sort(key=lambda c: c["opened_at"])
    KB_DIR.mkdir(parents=True, exist_ok=True)
    write_jsonl(CASES_FILE, cases)
    status = Counter(c["status"] for c in cases)
    print(f"[build] 根消息 {len(roots)} → 案例 {len(cases)}（合并 {len(roots) - len(cases)} 个追问）；"
          f"可提炼 {status['ok']}，无回复 {status['no_reply']}，非案例的零散发言 {status['not_case']} → {CASES_FILE}")
    return cases


# ---------------- 阶段 2：模型提炼 ----------------

IntentCode = Literal[tuple(INTENTS)]  # type: ignore[valid-type]
FATAL_ERROR = re.compile(r"PermissionDenied|AuthenticationError|FreeTierOnly|Arrearage|InvalidApiKey|Error code: 40[13]")
CATEGORIES = ("产品故障", "安装问题", "不适配", "错发漏发", "破损", "物流", "退款退货", "售前咨询", "其他")
CATEGORY_CODE = {"产品故障": "FAULT", "安装问题": "INSTALL", "不适配": "FIT", "错发漏发": "WRONG", "破损": "DMG",
                 "物流": "SHIP", "退款退货": "RET", "售前咨询": "PRE", "其他": "OTH"}


class CaseExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: IntentCode = Field(description="买家诉求对应的意图编码")
    category: Literal[CATEGORIES]  # type: ignore[valid-type]
    symptom: str = Field(description="买家反馈的问题，中文概述")
    info_requested: list[str] = Field(default_factory=list, description="要求买家补充的资料")
    diagnosis_steps: list[str] = Field(default_factory=list, description="技术/客服给出的排查步骤，按顺序")
    root_cause: Literal["产品问题", "安装问题", "车辆其他故障", "不适配", "买家原因", "未知"]
    resolution: Literal["指导排查", "补发", "供应商补发", "退货退款", "部分退款", "二次上架", "拒绝售后",
                        "需买家补充信息", "无结论"]
    resolution_detail: str = ""
    fitment_notes: str = ""
    outcome_clarity: Literal["明确", "部分", "无"]
    evidence_in_images: bool
    reusable_tip: str = ""
    pii_flag: bool
    pii_note: str = ""


def render_case(case: dict) -> str:
    head = (f"case_id: {case['case_id']}\n平台: {case['platform']}  站点: {case['site']}  阶段: {case['stage']}\n"
            f"SKU: {', '.join(case['sku']) or '未识别'}\n")
    lines = []
    for t in case["transcript"]:
        if t["noise"]:
            continue
        att = "".join(f"[{k}×{v}]" for k, v in t["attachments"].items())
        lines.append(f"[{t['time']}][{t['role']}] {t['text']} {att}".rstrip())
    return head + "\n对话：\n" + "\n".join(lines)


def get_provider():
    from app.providers.chat import get_chat_provider

    provider = get_chat_provider()
    if provider is None:
        from app.config import get_settings

        raise SystemExit("❌ 模型未就绪：" + "；".join(get_settings().agent_not_ready_reasons()))
    return provider


def call_structured(provider, system: str, user: str, schema: type[BaseModel]):
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    result = provider.structured(messages, schema)
    if result.parsed is None:
        result = provider.repair(messages, schema, result.raw_text, result.error or "", result.usage)
    return result


def pick_pilot(cases: list[dict], limit: int) -> list[dict]:
    """试点样本按产品线分层随机抽取（固定种子），比只取最早的案例更有代表性。"""
    rng = random.Random(42)
    by_line: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        by_line[(c["product_line"] or ["未识别"])[0]].append(c)
    for v in by_line.values():
        rng.shuffle(v)
    picked: list[dict] = []
    while len(picked) < limit and any(by_line.values()):
        for line in sorted(by_line, key=lambda k: -len(by_line[k])):
            if by_line[line] and len(picked) < limit:
                picked.append(by_line[line].pop())
    return sorted(picked, key=lambda c: c["opened_at"])


def stage_extract() -> None:
    from app.prompts import load_prompt, prompt_version

    cases = [c for c in read_jsonl(CASES_FILE) if c["status"] == "ok"]
    if not cases:
        raise SystemExit("❌ 没有可提炼的案例，请先运行 build 阶段")
    done = {r["case_id"]: r for r in read_jsonl(EXTRACTED_FILE)}
    targets = pick_pilot(cases, LIMIT) if LIMIT else cases
    todo = [c for c in targets if c["case_id"] not in done or done[c["case_id"]].get("extraction") is None]
    print(f"[extract] 目标 {len(targets)} 个案例（{'试点' if LIMIT else '全量'}），待提炼 {len(todo)} 个，并发 {PARALLEL}")
    if not todo:
        return
    provider = get_provider()
    system = load_prompt("feishu_case_extract")
    pv = prompt_version("feishu_case_extract")
    lock = threading.Lock()
    usage = Counter()
    stop = threading.Event()  # 额度用完、Key 无效这类错误对所有案例都一样，遇到一次就停

    def work(case: dict) -> dict | None:
        if stop.is_set():
            return None
        started = time.monotonic()
        try:
            result = call_structured(provider, system, render_case(case), CaseExtraction)
            extraction = result.parsed.model_dump() if result.parsed else None
            error = None if result.parsed else (result.error or "解析失败")[:500]
            tokens = result.usage
        except Exception as e:  # 单个案例失败不影响其他案例
            extraction, error, tokens = None, f"{type(e).__name__}: {e}"[:500], {}
            if FATAL_ERROR.search(error):
                stop.set()
        return {
            "case_id": case["case_id"], "extraction": extraction, "error": error, "usage": tokens,
            "prompt_version": pv, "model": provider.settings.llm_chat_model,
            "seconds": round(time.monotonic() - started, 1),
            "extracted_at": datetime.now().isoformat(timespec="seconds"),
        }

    finished = 0
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        futures = [pool.submit(work, c) for c in todo]
        for fut in as_completed(futures):
            row = fut.result()
            if row is None:
                continue
            with lock:
                done[row["case_id"]] = row
                usage.update({k: v for k, v in (row["usage"] or {}).items() if isinstance(v, int)})
                finished += 1
                mark = "✅" if row["extraction"] else "❌"
                print(f"  {mark} {finished}/{len(todo)} {row['case_id']} {row['seconds']}s"
                      + (f" {row['error'][:80]}" if row["error"] else ""))
                write_jsonl(EXTRACTED_FILE, list(done.values()))
    if stop.is_set():
        reason = next(r["error"] for r in done.values() if r.get("error") and FATAL_ERROR.search(r["error"]))
        raise SystemExit(f"❌ 模型服务拒绝调用，已停止（未完成的案例下次运行会自动重试）：\n   {reason[:300]}")
    failed = sum(1 for c in todo if done.get(c["case_id"], {}).get("extraction") is None)
    print(f"[extract] 完成，失败 {failed} 个；token 用量 输入 {usage['input_tokens']} / 输出 {usage['output_tokens']}"
          f" → {EXTRACTED_FILE}")


# ---------------- 阶段 3：起草知识条款 ----------------

class ClauseDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    text: str
    source_case_ids: list[str]
    conflicts: str = ""


def merged_cases() -> list[dict]:
    cases = {c["case_id"]: c for c in read_jsonl(CASES_FILE)}
    rows = []
    for r in read_jsonl(EXTRACTED_FILE):
        if r.get("extraction") and r["case_id"] in cases:
            rows.append({**cases[r["case_id"]], **r["extraction"], "prompt_version": r["prompt_version"]})
    return rows


def stage_clauses() -> None:
    from app.prompts import load_prompt

    rows = [r for r in merged_cases() if r["outcome_clarity"] in ("明确", "部分") and r["reusable_tip"]]
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[((r["product_line"] or ["通用"])[0], r["category"])].append(r)
    print(f"[clauses] 可复用案例 {len(rows)} 个 → {len(groups)} 组")
    if not groups:
        write_jsonl(CLAUSES_FILE, [])
        return
    provider = get_provider()
    system = load_prompt("feishu_clause_draft")
    today = date.today().isoformat()

    def work(key: tuple[str, str], items: list[dict]) -> dict:
        payload = [{k: r[k] for k in ("case_id", "sku", "site", "stage", "symptom", "info_requested",
                                      "diagnosis_steps", "root_cause", "resolution", "resolution_detail",
                                      "fitment_notes", "reusable_tip")} for r in items]
        user = f"产品线：{key[0]}  问题类型：{key[1]}\n案例：\n" + json.dumps(payload, ensure_ascii=False, indent=1)
        result = call_structured(provider, system, user, ClauseDraft)
        if not result.parsed:
            return {"group": f"{key[0]}/{key[1]}", "error": (result.error or "解析失败")[:300]}
        d = result.parsed
        valid_ids = {r["case_id"] for r in items}
        sources = [c for c in d.source_case_ids if c in valid_ids] or sorted(valid_ids)
        clause_id = f"SOP-FS-{key[0]}-{CATEGORY_CODE[key[1]]}"
        # 产品排查经验与站点无关，统一 ALL；来源站点记在 source_sites 里供审核参考
        return {
            "chunk_id": clause_id, "clause_id": clause_id, "document_id": "SOP-FEISHU", "version": f"draft-{today}",
            "title": d.title, "text": d.text, "store_id": "ALL", "marketplace_id": "ALL",
            "source_sites": sorted({r["marketplace_id"] for r in items if r["case_id"] in sources}),
            "audience": "INTERNAL_SOP", "language": "zh", "topic": "after_sales", "priority": 50,
            "effective_at": today, "expires_at": None, "review_state": "DRAFT",
            "sku": sorted({s for r in items if r["case_id"] in sources for s in r["sku"]}),
            "source": "飞书售后咨询群：" + ",".join(sources),
            "content_hash": hashlib.sha256(d.text.encode("utf-8")).hexdigest(),
            "source_case_ids": sources, "conflicts": d.conflicts, "case_count": len(sources),
        }

    clauses, errors = [], []
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        futures = {pool.submit(work, k, v): k for k, v in groups.items()}
        for fut in as_completed(futures):
            row = fut.result()
            (errors if "error" in row else clauses).append(row)
    clauses.sort(key=lambda c: c["clause_id"])
    write_jsonl(CLAUSES_FILE, clauses)
    print(f"[clauses] 起草 {len(clauses)} 条，失败 {len(errors)} 组 → {CLAUSES_FILE}")
    for e in errors:
        print(f"  ❌ {e['group']}：{e['error'][:100]}")


# ---------------- 阶段 4：审核 Excel ----------------

REVIEW_CHOICES = '"通过,修改,驳回"'
ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # openpyxl 不接受的控制字符


def stage_review() -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    rows = merged_cases()
    clauses = read_jsonl(CLAUSES_FILE)
    failed = [r for r in read_jsonl(EXTRACTED_FILE) if not r.get("extraction")]
    wb = Workbook()
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    review_fill = PatternFill("solid", fgColor="FFF2CC")

    def sheet(title: str, headers: list[tuple[str, int]], data: list[list], review_cols: int):
        ws = wb.create_sheet(title)
        ws.append([h for h, _ in headers])
        for i, (_, width) in enumerate(headers, 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = width
            ws.cell(1, i).font = Font(bold=True)
            ws.cell(1, i).fill = review_fill if i > len(headers) - review_cols else header_fill
        for row in data:
            ws.append([ILLEGAL_XML.sub("", v) if isinstance(v, str) else v for v in row])
        for r in ws.iter_rows(min_row=2):
            for c in r:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "B2"
        if data:
            dv = DataValidation(type="list", formula1=REVIEW_CHOICES, allow_blank=True)
            col = ws.cell(1, len(headers) - review_cols + 1).column_letter
            dv.add(f"{col}2:{col}{len(data) + 1}")
            ws.add_data_validation(dv)
        return ws

    guide = wb.active
    guide.title = "说明"
    for line in [
        "售后知识整理审核表（数据来自飞书“售后咨询群”，已脱敏：BUYER_/ORDER_ 为代号，VIN 后 6 位已打码）",
        "",
        "1. “案例”表：核对模型提炼的字段是否符合对话原文。在黄色列填写：审核结论（通过 / 修改 / 驳回）、需要修改的字段和正确内容、原因、审核人。",
        "2. “知识条款”表：逐条审核。技术排查类由技术支持确认，补发、退款等处理方式类由售后主管确认。",
        "   通过 = 原文导入；修改 = 以“修改后正文”导入；驳回 = 不导入。导入后为 INTERNAL_SOP，只供内部参考，不会自动发给买家。",
        "3. “提炼失败”表：模型未能给出合规结果的案例，可人工补充或忽略。",
        "4. 填好后保存本文件，把 .env 中 FEISHU_KB_STAGES 设为 import 再运行脚本即可导入。",
        "",
        f"生成时间：{datetime.now():%Y-%m-%d %H:%M}；案例 {len(rows)} 个；条款 {len(clauses)} 条；提炼失败 {len(failed)} 个",
    ]:
        guide.append([line])
    guide.column_dimensions["A"].width = 120

    def dialog(r: dict) -> str:
        out = []
        for t in r["transcript"]:
            att = "".join(f"[{k}×{v}]" for k, v in t["attachments"].items())
            out.append(f"{'（催促）' if t['noise'] else ''}[{t['time']}][{t['role']}] {t['text']} {att}".strip())
        return "\n".join(out)[:30000]

    case_headers = [
        ("case_id", 16), ("平台/站点/阶段", 14), ("SKU", 18), ("对话（脱敏）", 70), ("附件", 10), ("意图", 14),
        ("问题类型", 10), ("症状", 30), ("要求补充资料", 22), ("排查步骤", 40), ("原因", 10), ("处理方式", 10),
        ("处理细节", 30), ("适配说明", 20), ("结论清晰度", 8), ("依赖图片", 8), ("可复用经验", 30), ("残留个人信息", 12),
        ("审核结论", 10), ("需修改的字段与正确内容", 30), ("原因", 20), ("审核人", 10),
    ]
    case_rows = [[
        r["case_id"], f"{r['platform']}/{r['site']}/{r['stage']}", "\n".join(r["sku"]), dialog(r),
        " ".join(f"{k}×{v}" for k, v in r["attachments"].items()), f"{r['intent']}（{INTENTS[r['intent']][0]}）",
        r["category"], r["symptom"], "\n".join(r["info_requested"]),
        "\n".join(f"{i}. {s}" for i, s in enumerate(r["diagnosis_steps"], 1)), r["root_cause"], r["resolution"],
        r["resolution_detail"], r["fitment_notes"], r["outcome_clarity"], "是" if r["evidence_in_images"] else "否",
        r["reusable_tip"], (r["pii_note"] or "有") if r["pii_flag"] else "", "", "", "", "",
    ] for r in rows]
    sheet("案例", case_headers, case_rows, 4)

    clause_headers = [
        ("chunk_id", 22), ("标题", 30), ("正文", 70), ("SKU", 18), ("来源站点", 12), ("来源案例数", 8), ("来源案例", 20),
        ("矛盾点", 24), ("审核结论", 10), ("修改后正文", 60), ("原因", 20), ("审核人", 10),
    ]
    clause_rows = [[
        c["chunk_id"], c["title"], c["text"], "\n".join(c["sku"]), "\n".join(c["source_sites"]), c["case_count"],
        "\n".join(c["source_case_ids"]), c["conflicts"], "", "", "", "",
    ] for c in clauses]
    sheet("知识条款", clause_headers, clause_rows, 4)

    sheet("提炼失败", [("case_id", 16), ("错误", 80)], [[r["case_id"], r.get("error") or ""] for r in failed], 0)

    stats = wb.create_sheet("统计")
    stats.append(["维度", "取值", "案例数"])
    for field in ("category", "intent", "root_cause", "resolution", "outcome_clarity", "marketplace_id"):
        for value, n in Counter(r[field] for r in rows).most_common():
            stats.append([field, value, n])
    stats.append(["evidence_in_images", "是", sum(r["evidence_in_images"] for r in rows)])
    stats.append(["pii_flag", "是", sum(r["pii_flag"] for r in rows)])
    for col, width in zip("ABC", (20, 20, 10)):
        stats.column_dimensions[col].width = width

    path = KB_DIR / f"review_{datetime.now():%Y%m%d_%H%M}.xlsx"
    wb.save(path)
    print(f"[review] 审核表 → {path}")
    return path


# ---------------- 阶段 5：导入审核通过的条款 ----------------

def stage_import() -> None:
    from openpyxl import load_workbook

    review_file = ENV.get("FEISHU_KB_REVIEW_FILE")
    path = ROOT / review_file if review_file else max(KB_DIR.glob("review_*.xlsx"), default=None,
                                                      key=lambda p: p.stat().st_mtime)
    if not path or not path.exists():
        raise SystemExit("❌ 找不到审核表，请设置 FEISHU_KB_REVIEW_FILE")
    drafts = {c["chunk_id"]: c for c in read_jsonl(CLAUSES_FILE)}
    ws = load_workbook(path)["知识条款"]
    header = [c.value for c in ws[1]]
    col = {h: i for i, h in enumerate(header)}
    approved, decisions = [], Counter()
    for row in ws.iter_rows(min_row=2, values_only=True):
        chunk_id, decision = row[col["chunk_id"]], (row[col["审核结论"]] or "").strip()
        decisions[decision or "未审核"] += 1
        if decision not in ("通过", "修改") or chunk_id not in drafts:
            continue
        clause = dict(drafts[chunk_id])
        if decision == "修改" and (row[col["修改后正文"]] or "").strip():
            clause["text"] = row[col["修改后正文"]].strip()
        clause.update({
            "review_state": "PUBLISHED", "version": f"{date.today().isoformat()}",
            "content_hash": hashlib.sha256(clause["text"].encode("utf-8")).hexdigest(),
            "reviewed_by": row[col["审核人"]] or "", "review_file": path.name,
        })
        approved.append(clause)
    write_jsonl(SOP_FILE, approved)
    print(f"[import] 审核表 {path.name}：{dict(decisions)}；导入 {len(approved)} 条 → {SOP_FILE}")
    if approved:
        from app.db import init_db, session_scope
        from app.knowledge.loader import import_jsonl

        init_db()
        with session_scope() as db:
            print(f"[import] 数据库：{import_jsonl(db, SOP_FILE)}")


def main() -> int:
    print(f"阶段：{','.join(STAGES)}；试点上限：{LIMIT or '不限'}；输出目录：{KB_DIR}")
    if not TECH_NAMES:
        print("⚠ FEISHU_KB_TECH_NAMES 未填写，所有发言人都会标为“客服”")
    handlers = {"build": stage_build, "extract": stage_extract, "clauses": stage_clauses,
                "review": stage_review, "import": stage_import}
    unknown = [s for s in STAGES if s not in handlers]
    if unknown:
        print(f"❌ 未知阶段：{unknown}，可选 {list(handlers)}")
        return 1
    for s in STAGES:
        handlers[s]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
