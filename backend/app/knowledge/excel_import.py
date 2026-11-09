"""正式知识库：Excel 模板、解析校验、整体替换与回滚（第 12.3 节“资料来源/审核与发布”）。

原则：知识库由人整理、审核后上传；本模块只做格式解析与规则校验，不调用大模型整理或改写内容。
唯一的模型调用是向量模型：把条款原文转成检索向量（第 15.3 节），在写库之前完成——
检索模式下向量化失败则整次上传不生效，旧版本保持不变。
一次上传 = 一个版本（knowledge_imports），提交时在同一事务里把该店铺原来生效的条款改为 RETIRED，
再写入新条款；旧版本保留，可回滚。

模板一行一条，列以表头名识别（顺序可调）：
  编号 | 标题* | 正文* | 适用站点* | 能否对买家说* | 主题 | 优先级 | 语言 | 生效日期 | 失效日期 | 相关SKU | 启用 | 负责人/来源
  | 适用场景 | 适用店铺
“适用店铺”留空或“全部” = 所有店铺；填店铺名（多个用英文逗号）= 只给这些店铺。同一个文件可以上传到“全部店铺”：
每个店铺按这一列取自己的条款，各自生成一个新版本（各自可回滚）；上传到单个店铺时，不适用于它的行跳过。
同一个编号可以出现多行，只要适用店铺不重叠（如通用版本 + 某个店铺的特殊版本）。
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import DATA_DIR, get_settings
from ..db import utcnow
from ..guards.compliance import ComplianceChecker
from ..models import AuditEvent, KnowledgeChunk, KnowledgeImport
from . import vector_index
from .retriever import estimate_tokens
from .scenes import SCENES, parse_scenes
from .tokenize import search_text

MAX_ROWS = 2000
MAX_FILE_BYTES = 10 * 1024 * 1024
LONG_TEXT = 3000
SOURCE_MAX = 64  # 与 knowledge_chunks.source 的长度一致
CLAUSE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
UPLOAD_DIR = DATA_DIR / "knowledge_uploads"

SHEET = "知识条款"
COLUMNS = [  # (表头, 是否必填, 说明)
    ("编号", False, "条款编号，字母数字和 - _ . ，最长 40；留空自动生成 KB-001…；同一文件内不能重复。AI 引用依据时用它"),
    ("标题", True, "一句话概括这条知识"),
    ("正文", True, "完整内容。可对买家说的条款会原样提供给 AI 作为回复依据；条件和例外请写在同一条里"),
    ("适用站点", True, "全部，或 EBAY_US / EBAY_DE / EBAY_GB，多个用英文逗号分隔"),
    ("能否对买家说", True, "可以 = AI 可据此回复买家；仅内部 = 只给审核员参考（如补偿上限），AI 不会对买家说"),
    ("主题", False, "发货、物流、退货、退款、质保、适配、商品、价格、发票税务、订单、售后、合规、安装、话术模板、客服、其他"),
    ("优先级", False, "1–99，数字越小越优先；条款冲突时以优先级高的为准。默认 50"),
    ("语言", False, "正文语言，如 zh、en、de。默认 zh"),
    ("生效日期", False, "YYYY-MM-DD，默认上传当天"),
    ("失效日期", False, "YYYY-MM-DD，留空表示长期有效"),
    ("相关SKU", False, "多个用英文逗号分隔"),
    ("启用", False, "是 / 否，默认是；否 = 上传但暂不生效"),
    ("负责人/来源", False, "谁维护这条，或来自哪份制度；最长 64 个字"),
    ("适用场景", False, "仅内部条款用：会话属于这些场景时，在会话页“内部参考”里列给客服看；多个用逗号分隔。"
                    "可填 " + "、".join(SCENES) + "（常驻 = 每个会话都显示）"),
    ("适用店铺", False, "全部（默认，留空也是全部），或店铺名，多个用英文逗号分隔；上传到单个店铺时，不适用于该店铺的行会跳过"),
    ("参考文档", False, "引用的参考文档编号（在知识库页“参考文档”里上传后得到），多个用英文逗号分隔，如 DOC-0001,DOC-0003；"
                    "知识库页和会话页“内部参考”里可以点开。AI 不读文档，也不会发给买家")
]
REQUIRED = [c for c, req, _ in COLUMNS if req]
ALL_STORE_WORDS = {"", "全部", "全部店铺", "all", "*"}

TOPICS = {
    "发货": "shipping", "物流": "shipping", "退货": "return", "退款": "refund", "质保": "warranty",
    "适配": "fitment", "商品": "product_info", "价格": "pricing", "发票税务": "tax", "订单": "order",
    "售后": "after_sales", "合规": "compliance", "安装": "installation", "话术模板": "template",
    "客服": "service", "其他": "other",
}
AUDIENCE = {"可以": "PUBLIC_REPLY", "仅内部": "INTERNAL_SOP", "PUBLIC_REPLY": "PUBLIC_REPLY",
            "INTERNAL_SOP": "INTERNAL_SOP"}
YES = {"是", "启用", "yes", "y", "true", "1"}
NO = {"否", "停用", "no", "n", "false", "0"}


class ExcelImportError(ValueError):
    """文件层面无法解析（不是 xlsx、缺工作表、缺必填列等）。"""


@dataclass
class ParsedRow:
    row_no: int
    clause_id: str
    title: str
    text: str
    sites: list[str]  # ["ALL"] 或具体站点
    audience: str
    topic: str
    priority: int
    language: str
    effective_at: str
    expires_at: str | None
    sku: list[str]
    enabled: bool
    source: str | None
    scenes: list[str] = field(default_factory=list)
    stores: list[str] | None = None  # 适用店铺 ID；None = 全部店铺（不进内容哈希）
    doc_refs: list[str] = field(default_factory=list)  # 引用的参考文档编号

    def for_store(self, store_id: str) -> bool:
        return self.stores is None or store_id in self.stores

    @property
    def content_hash(self) -> str:
        parts = [self.title, self.text, ",".join(self.sites), self.audience, self.topic, str(self.priority),
                 self.effective_at, self.expires_at or "", ",".join(self.sku), str(self.enabled)]
        if self.scenes:  # 没挂场景的条款哈希不变，加列前后上传不会显示成“变更”
            parts.append(",".join(self.scenes))
        if self.doc_refs:  # 同上：没引用文档的条款哈希不变
            parts.append("docs:" + ",".join(self.doc_refs))
        key = "\x1f".join(parts)
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)  # {row, column, message}
    warnings: list[dict] = field(default_factory=list)

    def error(self, row, column, message):
        self.errors.append({"row": row, "column": column, "message": message})

    def warn(self, row, column, message):
        self.warnings.append({"row": row, "column": column, "message": message})


# ---------------------------------------------------------------- 模板
def build_template(sites: list[str]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.append([c for c, _, _ in COLUMNS])
    for i, (name, req, _) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=i)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C0392B" if req else "34495E")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    widths = [12, 28, 60, 18, 14, 12, 8, 8, 12, 12, 18, 8, 16, 18, 20, 18]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"
    example_site = sites[0] if sites else "全部"
    ws.append(["", "（示例，可删除）退货期限", "收到商品后 30 天内可通过 eBay 订单页申请退货……", example_site, "可以", "退货", 20,
               "zh", date.today().isoformat(), "", "", "是", "客服主管"])
    for col, choices in (("E", ["可以", "仅内部"]), ("F", list(TOPICS)), ("L", ["是", "否"])):
        dv = DataValidation(type="list", formula1='"' + ",".join(choices) + '"', allow_blank=True)
        dv.add(f"{col}2:{col}{MAX_ROWS + 1}")
        ws.add_data_validation(dv)

    guide = wb.create_sheet("填写说明")
    guide.append(["列", "必填", "说明"])
    for name, req, desc in COLUMNS:
        guide.append([name, "是" if req else "否", desc])
    guide.append([])
    guide.append(["适用站点可填", "", "全部、" + "、".join(sites)])
    guide.append(["整体替换", "", "上传后会替换该店铺当前全部知识；旧版本保留在“历史版本”中，可回滚"])
    guide.append(["不要写进条款", "", "买家个人信息、站外联系方式（邮箱、电话、社交账号）、诱导站外交易或用利益换好评的内容"])
    guide.append(["引用文档", "", "报表、手册、供应商资料等文件先在知识库页“参考文档”上传，得到编号（DOC-0001），"
                              "再填到「参考文档」列；不要在正文里写本机文件路径（同事打不开）"])
    guide.column_dimensions["A"].width = 16
    guide.column_dimensions["C"].width = 100
    for row in guide.iter_rows(min_row=1, max_row=1):
        for cell in row:
            cell.font = Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- 解析与校验
def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _date(v, result: ParseResult, row: int, column: str) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = _text(v).replace("/", "-").replace(".", "-")
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date().isoformat()  # 兼容 2026-1-5
        except ValueError:
            result.error(row, column, f"日期格式应为 YYYY-MM-DD：{_text(v)}")
            return None


DOC_NO = re.compile(r"DOC-\d{4}")


def _doc_refs(raw: str, text: str, audience: str | None, stores: list[str] | None, known: dict[str, dict] | None,
              result: "ParseResult", row_no: int) -> list[str]:
    """「参考文档」列：编号格式、是否存在、已归档、可对买家说的条款引用文档、敏感文档的适用店铺；正文提到编号却没填这一列。"""
    refs: list[str] = []
    for x in [x.strip().upper() for x in re.split(r"[,，、;；\s]+", raw or "") if x.strip()]:
        if not DOC_NO.fullmatch(x):
            result.error(row_no, "参考文档", f"“{x}”不是文档编号（应为 DOC-0001 这样的格式）")
            continue
        if x in refs:
            continue
        refs.append(x)
        if known is None:
            continue
        doc = known.get(x)
        if doc is None:
            result.error(row_no, "参考文档", f"文档 {x} 不存在或已删除（先在知识库页“参考文档”里上传）")
            continue
        if doc.get("status") == "ARCHIVED":
            result.warn(row_no, "参考文档", f"文档 {x} 已归档，仍然能打开；确认还要引用它")
        scope = doc.get("store_ids") or None
        if doc.get("sensitive") and scope and (stores is None or not set(stores) <= set(scope)):
            result.warn(row_no, "参考文档", f"文档 {x} 是敏感文档，只适用于部分店铺，这条条款的适用店铺超出了它的范围")
    if refs and audience == "PUBLIC_REPLY":
        result.warn(row_no, "参考文档", "可对买家说的条款引用了文档：AI 不会读文档，也不会发给买家")
    mentioned = [m for m in dict.fromkeys(DOC_NO.findall(text or "")) if m not in refs]
    if mentioned:
        result.warn(row_no, "参考文档", f"正文提到了 {'、'.join(mentioned)}，请在「参考文档」列填写，才能点开")
    return refs


def parse_workbook(data: bytes, allowed_sites: list[str], today: date | None = None,
                   known_stores: dict[str, str] | None = None, known_docs: dict[str, dict] | None = None) -> ParseResult:
    """known_stores：{店铺名或 ID 的小写: 店铺 ID}，用来识别“适用店铺”列；不传时这一列不起作用（全部当作所有店铺）。
    allowed_sites：允许出现的站点（上传到多个店铺时是这些店铺站点的并集，各店铺的取舍见 for_store）。
    known_docs：{文档编号: {status, sensitive, store_ids, title}}，校验「参考文档」列；不传时只检查编号格式。"""
    from openpyxl import load_workbook

    if len(data) > MAX_FILE_BYTES:
        raise ExcelImportError("文件超过 10MB")
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise ExcelImportError("无法读取文件：请上传 .xlsx 格式（可先下载模板填写）") from None
    if SHEET not in wb.sheetnames:
        raise ExcelImportError(f"找不到名为“{SHEET}”的工作表（请使用模板）")
    rows = list(wb[SHEET].iter_rows(values_only=True))
    if not rows:
        raise ExcelImportError("工作表是空的")
    header = [_text(h) for h in rows[0]]
    index = {name: header.index(name) for name, _, _ in COLUMNS if name in header}
    missing = [c for c in REQUIRED if c not in index]
    if missing:
        raise ExcelImportError("缺少必填列：" + "、".join(missing))

    today = today or date.today()
    allowed = {s.upper() for s in allowed_sites}
    result = ParseResult()
    checker = ComplianceChecker()
    seen: dict[str, int] = {}
    auto_no = 0
    data_rows = [(i, r) for i, r in enumerate(rows[1:], start=2) if any(_text(v) for v in r)]
    if len(data_rows) > MAX_ROWS:
        raise ExcelImportError(f"条款超过 {MAX_ROWS} 行")

    for row_no, r in data_rows:
        get = lambda name: r[index[name]] if name in index and index[name] < len(r) else None  # noqa: E731
        title, text = _text(get("标题")), _text(get("正文"))
        if title.startswith("（示例"):
            result.warn(row_no, "标题", "示例行已跳过")
            continue
        if not title:
            result.error(row_no, "标题", "不能为空")
        if not text:
            result.error(row_no, "正文", "不能为空")
        elif len(text) > LONG_TEXT:
            result.warn(row_no, "正文", f"正文 {len(text)} 字，较长；建议按条件拆成多条")

        clause_id = _text(get("编号"))
        if not clause_id:
            auto_no += 1
            clause_id = f"KB-{auto_no:03d}"
            while clause_id in seen:
                auto_no += 1
                clause_id = f"KB-{auto_no:03d}"
        elif not CLAUSE_ID.match(clause_id):
            result.error(row_no, "编号", "只能用字母、数字和 - _ .，最长 40 个字符")
        seen.setdefault(clause_id, row_no)

        stores: list[str] | None = None
        raw_stores = [x.strip() for x in re.split(r"[,，、;；\n]", _text(get("适用店铺"))) if x.strip()]
        if known_stores is not None and raw_stores and not any(x.lower() in ALL_STORE_WORDS for x in raw_stores):
            stores = []
            for x in raw_stores:
                sid = known_stores.get(x.lower())
                if sid is None:
                    result.error(row_no, "适用店铺", f"找不到店铺“{x}”（填店铺名，多个用英文逗号分隔，或填“全部”）")
                elif sid not in stores:
                    stores.append(sid)

        raw_sites = [s.strip() for s in re.split(r"[,，、;；]", _text(get("适用站点"))) if s.strip()]
        sites: list[str] = []
        if not raw_sites:
            result.error(row_no, "适用站点", "不能为空（全部或具体站点）")
        elif any(s in ("全部", "ALL", "all") for s in raw_sites):
            sites = ["ALL"]
        else:
            for s in raw_sites:
                if s.upper() not in allowed:
                    result.error(row_no, "适用站点", f"站点 {s} 不在店铺开通的站点内（{'、'.join(sorted(allowed))}）")
                else:
                    sites.append(s.upper())
            sites = list(dict.fromkeys(sites))

        audience = AUDIENCE.get(_text(get("能否对买家说")))
        if not audience:
            result.error(row_no, "能否对买家说", "只能填“可以”或“仅内部”")

        topic_raw = _text(get("主题"))
        topic = TOPICS.get(topic_raw) or (topic_raw if topic_raw in TOPICS.values() else None)
        if topic is None:
            if topic_raw:
                result.warn(row_no, "主题", f"未识别的主题“{topic_raw}”，按“其他”处理")
            topic = "other"

        priority = 50
        p_raw = _text(get("优先级"))
        if p_raw:
            try:
                priority = int(float(p_raw))
                if not 1 <= priority <= 99:
                    raise ValueError
            except ValueError:
                result.error(row_no, "优先级", "应为 1–99 的整数")

        effective = _date(get("生效日期"), result, row_no, "生效日期") or today.isoformat()
        expires = _date(get("失效日期"), result, row_no, "失效日期")
        if expires and expires < effective:
            result.error(row_no, "失效日期", "早于生效日期")
        elif expires and expires < today.isoformat():
            result.warn(row_no, "失效日期", "已经过期，上传后不会生效")

        enabled_raw = _text(get("启用")).lower()
        enabled = True
        if enabled_raw in NO:
            enabled = False
            result.warn(row_no, "启用", "标为“否”，上传后暂不生效")
        elif enabled_raw and enabled_raw not in YES:
            result.error(row_no, "启用", "只能填“是”或“否”")

        source = _text(get("负责人/来源"))
        if len(source) > SOURCE_MAX:
            result.error(row_no, "负责人/来源", f"最长 {SOURCE_MAX} 个字，现在 {len(source)} 个字")

        scenes, unknown = parse_scenes(_text(get("适用场景")))
        if unknown:
            result.error(row_no, "适用场景", f"不认识的场景：{'、'.join(unknown)}（可填 {'、'.join(SCENES)}）")
        if scenes and audience == "PUBLIC_REPLY":
            result.warn(row_no, "适用场景", "可对买家说的条款不会显示在“内部参考”里，适用场景对它不起作用")

        if audience == "PUBLIC_REPLY" and text:
            hits = [h for h in checker.check(text).hits if h.severity == "BLOCK"]
            if hits:
                result.warn(row_no, "正文", "可对买家说的条款里含有可能违反 eBay 规则的内容："
                            + "；".join(f"{h.text}（{h.rule}）" for h in hits[:3]) + "。AI 可能照此回复，请确认")

        doc_refs = _doc_refs(_text(get("参考文档")), text, audience, stores, known_docs, result, row_no)

        result.rows.append(ParsedRow(
            row_no=row_no, clause_id=clause_id, title=title[:300], text=text, sites=sites,
            audience=audience or "INTERNAL_SOP", topic=topic, priority=priority,
            language=_text(get("语言")) or "zh", effective_at=effective, expires_at=expires,
            sku=[s.strip().upper() for s in re.split(r"[,，、]", _text(get("相关SKU"))) if s.strip()],
            enabled=enabled, source=source[:SOURCE_MAX] or None, scenes=scenes, stores=stores, doc_refs=doc_refs,
        ))
    # 同一编号可以有多行（通用版 + 某店铺的特殊版），但适用店铺不能重叠
    by_id: dict[str, list[ParsedRow]] = {}
    for r in result.rows:
        by_id.setdefault(r.clause_id, []).append(r)
    for cid, group in by_id.items():
        for k, a in enumerate(group):
            b = next((b for b in group[:k] if a.stores is None or b.stores is None or set(a.stores) & set(b.stores)), None)
            if b is not None:
                result.error(a.row_no, "编号", f"与第 {b.row_no} 行重复：{cid}" +
                             ("（同一编号的多行，适用店铺不能重叠）" if a.stores is not None or b.stores is not None else ""))
    if not result.rows and not result.errors:
        result.error(None, None, "没有可导入的条款")
    return result


def for_store(parsed: ParseResult, store_id: str, store_sites: list[str], strict: bool = True) -> ParseResult:
    """取这个店铺的条款：适用店铺包含它的行；具体站点只保留店铺开通的。
    strict（上传到单个店铺）：适用于它的行里有店铺没开通的站点 → 报错（和以前一样）；
    非 strict（上传到全部店铺）：没开通的站点去掉，一个都不剩就跳过（那一行给别的店铺用）。"""
    allowed = {x.upper() for x in store_sites}
    out = ParseResult(errors=list(parsed.errors), warnings=list(parsed.warnings))
    for r in parsed.rows:
        if not r.for_store(store_id):
            continue
        if r.sites != ["ALL"]:
            bad = [x for x in r.sites if x not in allowed]
            if bad and strict:
                out.error(r.row_no, "适用站点", f"站点 {'、'.join(bad)} 不在店铺开通的站点内（{'、'.join(sorted(allowed))}）")
                continue
            sites = [x for x in r.sites if x in allowed]
            if not sites:
                continue
            if sites != r.sites:
                r = ParsedRow(**{**r.__dict__, "sites": sites})
        out.rows.append(r)
    if not out.rows and not out.errors:
        out.error(None, None, "没有适用于这个店铺的条款")
    return out


# ---------------------------------------------------------------- 预览
def _site_tokens(rows: list[ParsedRow], sites: list[str], today: str) -> dict[str, int]:
    out = {}
    for site in sites:
        out[site] = sum(
            estimate_tokens(r.title + r.text) + 12 for r in rows
            if r.enabled and r.audience == "PUBLIC_REPLY" and r.effective_at <= today
            and (not r.expires_at or r.expires_at >= today) and ("ALL" in r.sites or site in r.sites))
    return out


def active_import(db: Session, store_id: str) -> KnowledgeImport | None:
    return db.scalar(select(KnowledgeImport).where(KnowledgeImport.store_id == store_id,
                                                   KnowledgeImport.status == "ACTIVE"))


def preview(db: Session, store_id: str, parsed: ParseResult, sites: list[str], budget: int,
            today: date | None = None) -> dict:
    today_s = (today or date.today()).isoformat()
    tokens = _site_tokens(parsed.rows, sites, today_s)
    warnings = list(parsed.warnings)
    for site, n in tokens.items():
        if n > budget:
            warnings.append({"row": None, "column": "适用站点", "message":
                             f"{site} 可对买家说的条款合计约 {n} token，超过预算 {budget}：该站点所有消息都会转人工。"
                             "请精简条款或调高 .env 的 KNOWLEDGE_FULL_CONTEXT_MAX_TOKENS"})
    current = {c.clause_id: c for c in db.scalars(select(KnowledgeChunk).where(
        KnowledgeChunk.store_id == store_id, KnowledgeChunk.review_state.in_(("PUBLISHED", "DRAFT"))))}
    new_ids = {r.clause_id: r for r in parsed.rows}
    changed = [cid for cid, r in new_ids.items() if cid in current and current[cid].content_hash != r.content_hash]
    return {
        "ok": not parsed.errors,
        "errors": parsed.errors,
        "warnings": warnings,
        "rows": [{"row": r.row_no, "clause_id": r.clause_id, "title": r.title, "sites": r.sites,
                  "audience": r.audience, "topic": r.topic, "priority": r.priority, "effective_at": r.effective_at,
                  "expires_at": r.expires_at, "enabled": r.enabled, "chars": len(r.text), "scenes": r.scenes,
                  "doc_refs": r.doc_refs}
                 for r in parsed.rows],
        "counts": {"rows": len(parsed.rows),
                   "public": sum(r.audience == "PUBLIC_REPLY" for r in parsed.rows),
                   "internal": sum(r.audience == "INTERNAL_SOP" for r in parsed.rows),
                   "disabled": sum(not r.enabled for r in parsed.rows)},
        "site_tokens": tokens,
        "budget_tokens": budget,
        "diff": {"added": sorted(set(new_ids) - set(current)), "removed": sorted(set(current) - set(new_ids)),
                 "changed": sorted(changed),
                 "unchanged": len([c for c in new_ids if c in current and c not in changed])},
        "current_import": (lambda a: {"id": a.id, "file_name": a.file_name, "uploaded_at": a.uploaded_at.isoformat()}
                           if a else None)(active_import(db, store_id)),
    }


# ---------------------------------------------------------------- 提交与回滚
# 下线时保留原状态的区别，回滚时据此恢复：已发布 → RETIRED，未启用的草稿 → WITHDRAWN
TAKE_DOWN = {"PUBLISHED": "RETIRED", "DRAFT": "WITHDRAWN"}
RESTORE = {v: k for k, v in TAKE_DOWN.items()}


def _take_down(db: Session, store_id: str) -> int:
    n = 0
    for c in db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.store_id == store_id,
                                                     KnowledgeChunk.review_state.in_(tuple(TAKE_DOWN)))):
        c.review_state = TAKE_DOWN[c.review_state]
        n += 1
    return n


def index_bodies(db: Session, bodies: list[str]) -> dict:
    """为条款正文生成向量。检索模式下失败直接抛出；全量注入模式下只是提前备好，失败记下原因不阻断。"""
    if get_settings().knowledge_mode == "retrieval":
        return vector_index.embed_texts(db, bodies)
    reasons = vector_index.readiness()
    if reasons:
        return {"skipped": "；".join(reasons)}
    try:
        with db.begin_nested():
            return vector_index.embed_texts(db, bodies)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def commit(db: Session, store_id: str, parsed: ParseResult, file_name: str, data: bytes, actor: str,
           summary: dict) -> KnowledgeImport:
    if parsed.errors:
        raise ValueError("有错误未修正，不能提交")
    # 先向量化（只有启用且可对买家说的条款进回复检索），失败时还没写任何东西
    index = index_bodies(db, [f"{r.title}\n{r.text}" for r in parsed.rows if r.enabled and r.audience == "PUBLIC_REPLY"])
    summary = {**summary, "index": index}
    sha = hashlib.sha256(data).hexdigest()
    previous = active_import(db, store_id)
    imp = KnowledgeImport(store_id=store_id, file_name=file_name[:255], file_sha256=sha, uploaded_by=actor,
                          status="ACTIVE", row_count=len(parsed.rows),
                          public_count=sum(r.audience == "PUBLIC_REPLY" for r in parsed.rows),
                          internal_count=sum(r.audience == "INTERNAL_SOP" for r in parsed.rows),
                          summary=summary)
    db.add(imp)
    db.flush()

    # 整体替换：该店铺原来的条款全部下线（保留原行，回滚时恢复）
    retired = _take_down(db, store_id)
    if previous:
        previous.status = "SUPERSEDED"

    version = f"K{imp.id}"
    for r in parsed.rows:
        for site in r.sites:
            suffix = "" if len(r.sites) == 1 else f"-{site.removeprefix('EBAY_')}"
            db.add(KnowledgeChunk(
                chunk_id=f"{version}-{r.clause_id}{suffix}"[:64], clause_id=r.clause_id,
                document_id=f"UPLOAD-{imp.id}", version=version, title=r.title, text=f"{r.title}\n{r.text}",
                store_id=store_id, marketplace_id=site, audience=r.audience, language=r.language, topic=r.topic,
                priority=r.priority, effective_at=r.effective_at, expires_at=r.expires_at,
                review_state="PUBLISHED" if r.enabled else "DRAFT", sku=r.sku, scenes=r.scenes or None,
                doc_refs=r.doc_refs or None,
                source=r.source or f"Excel:{file_name}"[:64], content_hash=r.content_hash, import_id=imp.id,
                search_text=search_text(f"{r.title}\n{r.text}"),
            ))

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", Path(file_name).name)[:120] or "upload.xlsx"
    path = UPLOAD_DIR / f"{imp.id}_{safe}"
    path.write_bytes(data)
    try:
        imp.stored_path = str(path.relative_to(DATA_DIR.parent))
    except ValueError:  # 上传目录不在项目内（如测试的临时目录）
        imp.stored_path = str(path)
    db.add(AuditEvent(actor=actor, action="KNOWLEDGE_IMPORTED", store_id=store_id, detail={
        "import_id": imp.id, "file": file_name, "sha256": sha, "rows": len(parsed.rows), "retired": retired,
        "previous_import": previous.id if previous else None}))
    db.flush()
    return imp


def rollback(db: Session, import_id: int, actor: str, note: str | None = None) -> KnowledgeImport:
    """回滚到指定的历史版本：当前版本下线，指定版本的条款恢复为发布状态。"""
    target = db.get(KnowledgeImport, import_id)
    if target is None:
        raise LookupError("版本不存在")
    if target.status == "ACTIVE":
        raise ValueError("该版本已经是当前生效版本")
    current = active_import(db, target.store_id)
    _take_down(db, target.store_id)
    if current:
        current.status = "ROLLED_BACK"
        current.note = note or f"{utcnow():%Y-%m-%d %H:%M} 由 {actor} 回滚到版本 {target.id}"
    for c in db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.import_id == target.id)):
        c.review_state = RESTORE.get(c.review_state, c.review_state)
    target.status = "ACTIVE"
    restored = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.import_id == target.id,
                                                       KnowledgeChunk.review_state == "PUBLISHED")).all()
    vector_index.fill_search_text(restored)
    index_bodies(db, [c.text for c in restored if c.audience == "PUBLIC_REPLY"])
    db.add(AuditEvent(actor=actor, action="KNOWLEDGE_ROLLBACK", store_id=target.store_id,
                      detail={"to_import": target.id, "from_import": current.id if current else None, "note": note}))
    db.flush()
    return target
