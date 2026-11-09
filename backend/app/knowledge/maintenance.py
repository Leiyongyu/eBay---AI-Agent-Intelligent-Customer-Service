"""知识库导出与清理（管理员）。

导出：把店铺当前生效版本的条款（已发布 + 上传了但未启用）按上传模板的格式写成一个 Excel，
      全部店铺时内容相同的条款合并成一行，“适用店铺”列写它在哪些店铺（所有店铺都有写“全部”）；
      改完可以直接再上传（选“全部店铺”时按“适用店铺”分给各店铺）。
清理：
  history —— 只删历史：被替换下线的条款（RETIRED / WITHDRAWN）、非当前版本的上传记录和服务器上保存的原文件；
             当前生效的知识不动，AI 回复不受影响，但删掉的历史版本不能再回滚；
  all     —— 清空：该店铺全部条款和上传记录都删掉，AI 回复时没有任何知识依据（相关消息会转人工），直到重新上传。
清理后顺带删掉不再被任何条款用到的检索向量（knowledge_embeddings，按正文哈希去重共用）。
"""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from ..config import DATA_DIR
from ..models import AuditEvent, KnowledgeChunk, KnowledgeImport
from . import excel_import as xi
from . import vector_index as vi

CURRENT = ("PUBLISHED", "DRAFT")
HISTORY = ("RETIRED", "WITHDRAWN")
TOPIC_ZH: dict[str, str] = {}
for _zh, _en in xi.TOPICS.items():
    TOPIC_ZH.setdefault(_en, _zh)  # 同一个主题有两个中文名时取第一个（发货 / 物流 → 发货）


# ---------------------------------------------------------------- 导出
def _store_rows(db: Session, store_ids: list[str]) -> dict[str, list[tuple]]:
    """每个店铺当前条款还原成模板的一行一条（同一条款展开到多个站点的合并回一行）：{店铺 ID: [行的内容元组]}。"""
    chunks = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.store_id.in_(store_ids),
                                                     KnowledgeChunk.review_state.in_(CURRENT))
                        .order_by(KnowledgeChunk.priority, KnowledgeChunk.clause_id, KnowledgeChunk.chunk_id)).all()
    groups: dict[tuple[str, str], list[KnowledgeChunk]] = {}
    for c in chunks:
        groups.setdefault((c.store_id, c.clause_id), []).append(c)
    out: dict[str, list[tuple]] = {}
    for (sid, clause_id), cs in groups.items():
        c = cs[0]
        body = c.text[len(c.title) + 1:] if c.text.startswith(c.title + "\n") else c.text  # 存库时正文前加了标题行
        sites = list(dict.fromkeys(x.marketplace_id for x in cs))
        source = c.source if c.source and not c.source.startswith("Excel:") else ""  # 没填来源时系统记的是文件名
        out.setdefault(sid, []).append((
            clause_id, c.title, body, "全部" if "ALL" in sites else ",".join(sites),
            "可以" if c.audience == "PUBLIC_REPLY" else "仅内部", TOPIC_ZH.get(c.topic, c.topic),
            c.priority, c.language, c.effective_at, c.expires_at or "", ",".join(c.sku or []),
            "是" if c.review_state == "PUBLISHED" else "否", source, ",".join(c.scenes or []), ",".join(c.doc_refs or [])))
    return out


def export_rows(db: Session, stores: list[tuple[str, str]], all_stores: bool = False) -> list[list]:
    """合并成一张表：内容完全相同的条款只出一行，“适用店铺”写它在哪些店铺；
    全部店铺导出时，凡是有知识的店铺都有的条款写“全部”。列顺序同上传模板（COLUMNS）。"""
    names = dict(stores)
    per = _store_rows(db, [sid for sid, _ in stores])
    with_kb = set(per)
    merged: dict[tuple, list[str]] = {}
    for sid in sorted(per, key=lambda x: names.get(x) or x):
        for row in per[sid]:
            merged.setdefault(row, []).append(sid)
    out = []
    for row, sids in merged.items():
        common = all_stores and set(sids) >= with_kb
        label = "全部" if common else ",".join(names.get(x) or x for x in sids)
        values = dict(zip(EXPORT_FIELDS, row))
        values["适用店铺"] = label
        out.append((0 if common else 1, "" if common else label, row[6], row[0], [values.get(c, "") for c, _, _ in xi.COLUMNS]))
    out.sort(key=lambda x: x[:4])  # 通用的在前；单店铺的按店铺名排
    return [x[4] for x in out]


EXPORT_FIELDS = ("编号", "标题", "正文", "适用站点", "能否对买家说", "主题", "优先级", "语言", "生效日期", "失效日期",
                 "相关SKU", "启用", "负责人/来源", "适用场景", "参考文档")


def export_workbook(db: Session, stores: list[tuple[str, str]], all_stores: bool = False) -> tuple[bytes, int]:
    """返回（xlsx 内容, 行数）。一个文件一张“知识条款”表，表头、列顺序与上传模板一致，多一列“适用店铺”。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    rows = export_rows(db, stores, all_stores)
    wb = Workbook()
    ws = wb.active
    ws.title = xi.SHEET
    ws.append([c for c, _, _ in xi.COLUMNS])
    for i, (_name, req, _) in enumerate(xi.COLUMNS, 1):
        cell = ws.cell(row=1, column=i)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C0392B" if req else "34495E")
    for r in rows:
        ws.append(r)
    widths = {"编号": 14, "标题": 30, "正文": 70, "适用站点": 12, "能否对买家说": 10, "主题": 10, "优先级": 7, "语言": 6,
              "生效日期": 11, "失效日期": 11, "相关SKU": 14, "启用": 6, "负责人/来源": 14, "适用场景": 14, "适用店铺": 24,
              "参考文档": 16}
    for i, (name, _, _) in enumerate(xi.COLUMNS, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = widths.get(name, 12)
    for row in ws.iter_rows(min_row=2, min_col=3, max_col=3):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions

    col = [c for c, _, _ in xi.COLUMNS].index("适用店铺")
    common = sum(1 for r in rows if r[col] == "全部")
    info = wb.create_sheet("导出说明")
    for line in (["导出日期", date.today().isoformat()],
                 ["店铺", "全部店铺" if all_stores else "、".join(n for _, n in stores)],
                 ["行数", f"{len(rows)} 行（所有店铺通用 {common} 行，只给部分店铺的 {len(rows) - common} 行）"],
                 ["内容", "各店铺当前生效的知识（含上传了但未启用的，“启用”列为“否”）；内容完全相同的条款合并成一行，"
                          "“适用店铺”列写它在哪些店铺（全部 = 所有店铺都有）"],
                 ["怎么清理", "不需要的整行删掉；要改的直接改；只给某些店铺的，在“适用店铺”填店铺名（多个用英文逗号）"],
                 ["怎么上传", "知识库页面上传时选“全部店铺”：每个店铺按“适用店铺”取自己的条款，各自生成一个新版本（各自可回滚）；"
                            "文件里一条都不适用的店铺保持不动。上传到单个店铺时，只取适用于它的行"],
                 ["注意", "同一个编号可以有多行（通用版 + 某店铺的特殊版），但适用店铺不能重叠"]):
        info.append(line)
    info.column_dimensions["A"].width = 12
    info.column_dimensions["B"].width = 110
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), len(rows)


def safe_name(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in s)[:60] or "store"


# ---------------------------------------------------------------- 清理
def stats(db: Session, store_ids: list[str]) -> dict:
    """清理前看一眼：当前条款、历史条款、历史版本数。"""
    def count(states):
        return db.scalar(select(func.count()).select_from(KnowledgeChunk).where(
            KnowledgeChunk.store_id.in_(store_ids), KnowledgeChunk.review_state.in_(states))) or 0

    clauses = db.scalar(select(func.count(func.distinct(KnowledgeChunk.store_id + ":" + KnowledgeChunk.clause_id)))
                        .where(KnowledgeChunk.store_id.in_(store_ids), KnowledgeChunk.review_state.in_(CURRENT))) or 0
    imports = db.execute(select(KnowledgeImport.status, func.count()).where(KnowledgeImport.store_id.in_(store_ids))
                         .group_by(KnowledgeImport.status)).all()
    by_status = dict(imports)
    return {"stores": len(store_ids), "current_clauses": clauses, "current_rows": count(CURRENT),
            "history_rows": count(HISTORY), "active_imports": by_status.get("ACTIVE", 0),
            "history_imports": sum(n for s, n in by_status.items() if s != "ACTIVE")}


def _remove_files(paths: list[str | None]) -> int:
    root = DATA_DIR.parent
    n = 0
    for p in paths:
        if not p:
            continue
        f = Path(p) if Path(p).is_absolute() else root / p
        try:
            f.resolve().relative_to(xi.UPLOAD_DIR.resolve())  # 只删上传目录里的文件
        except ValueError:
            continue
        if f.is_file():
            f.unlink()
            n += 1
    return n


def purge_orphan_vectors(db: Session) -> int:
    """删掉不再被任何条款用到的向量（向量表不存在或不可用时跳过）。"""
    from sqlalchemy import inspect

    if not inspect(db.connection()).has_table(vi.TABLE):  # 没装 pgvector 等：没有向量表
        return 0
    have = {vi.text_hash(t) for (t,) in db.execute(select(KnowledgeChunk.text))}
    hashes = [h for (h,) in db.execute(text(f"SELECT DISTINCT content_hash FROM {vi.TABLE}"))]
    orphan = [h for h in hashes if h not in have]
    for i in range(0, len(orphan), 500):
        part = orphan[i:i + 500]
        db.execute(text(f"DELETE FROM {vi.TABLE} WHERE content_hash IN ({','.join(':h' + str(k) for k in range(len(part)))})"),
                   {f"h{k}": h for k, h in enumerate(part)})
    return len(orphan)


def cleanup(db: Session, store_ids: list[str], mode: str, actor: str) -> dict:
    if mode not in ("history", "all"):
        raise ValueError("mode 只能是 history 或 all")
    before = stats(db, store_ids)
    states = HISTORY if mode == "history" else CURRENT + HISTORY
    chunks = db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.store_id.in_(store_ids),
                                                     KnowledgeChunk.review_state.in_(states))).rowcount or 0
    q = select(KnowledgeImport).where(KnowledgeImport.store_id.in_(store_ids))
    if mode == "history":
        q = q.where(KnowledgeImport.status != "ACTIVE")
    imps = db.scalars(q).all()
    files = _remove_files([i.stored_path for i in imps])
    for i in imps:
        db.delete(i)
    db.flush()
    vectors = purge_orphan_vectors(db)
    result = {"mode": mode, "stores": len(store_ids), "deleted_rows": chunks, "deleted_imports": len(imps),
              "deleted_files": files, "deleted_vectors": vectors, "before": before}
    db.add(AuditEvent(actor=actor, action="KNOWLEDGE_CLEANED",
                      store_id=store_ids[0] if len(store_ids) == 1 else None,
                      detail={**{k: v for k, v in result.items() if k != "before"},
                              "store_ids": store_ids if len(store_ids) <= 50 else store_ids[:50]}))
    db.flush()
    return result
