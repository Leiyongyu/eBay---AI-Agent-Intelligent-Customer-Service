"""把业务数据从 PostgreSQL（ebay_cs_agent）迁到 MySQL（jmh_ai_agent），表名加 cs_ 前缀；向量留在 PostgreSQL。

迁哪些：models.py 里的全部业务表（Base.metadata，按外键顺序）。不迁：
  - 向量：knowledge_embeddings、embedding_profiles（继续在 PostgreSQL，切换后由 VECTOR_DATABASE_URL 连）；
  - 已删除的数据助手留下的旧表（finance_transactions、sales_orders、data_* 等，代码早已不用）。

默认只检查（不建表、不写数据）：两边连得上；每张表源行数；目标库里有没有同名表、有没有数据；
MySQL 存不了的值（浮点 NaN / 无穷大）。
    python scripts/migrate_pg_to_mysql.py
确认后迁移（先停 main.py，避免迁移时还有新数据写进 PostgreSQL）：
    python scripts/migrate_pg_to_mysql.py --apply
  1. 在 MySQL 建表（cs_ 前缀，utf8mb4_0900_bin，带中文注释；已有同名表且有数据就停下，不覆盖）；
  2. 按外键顺序分批复制（每批 2000 行，主键原样保留，MySQL 自增从最大值后继续）；
  3. 逐表核对行数和主键范围，任一张对不上就报错，不改 .env；
  4. 建凭证表触发器等（init_db 里的步骤）。
不删 PostgreSQL 里的任何东西（旧表等确认后另外删）。

连接：源 = .env 的 DATABASE_URL（迁移前它指向 PostgreSQL）；目标默认 mysql+pymysql://root@127.0.0.1:3306/jmh_ai_agent，
密码用 .env 的 DATABASE_PASSWORD（--mysql-url 可改；密码不打印）。
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path

os.environ["TABLE_PREFIX"] = "cs_"  # 导入模型前设：目标表名带前缀
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PREFIX = "cs_"
BATCH = 2000
DEFAULT_MYSQL = "mysql+pymysql://root@127.0.0.1:3306/jmh_ai_agent?charset=utf8mb4"


def engines(mysql_url: str):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from app.config import get_settings
    from app.db import _engine_kwargs

    s = get_settings()
    src_url = s.effective_database_url
    if not src_url.startswith("postgresql"):
        raise SystemExit(f"源库不是 PostgreSQL（{make_url(src_url).render_as_string(hide_password=True)}）：已经切换过了？")
    dst = make_url(mysql_url)
    if not dst.password and s.database_password:
        dst = dst.set(password=s.database_password)
    src = create_engine(src_url, pool_pre_ping=True)
    tgt = create_engine(dst, pool_pre_ping=True, **_engine_kwargs("mysql"))
    return src, tgt


def float_columns(table):
    from sqlalchemy import Float

    return [c.name for c in table.columns if isinstance(c.type, Float)]


def check(src, tgt) -> dict:
    from sqlalchemy import inspect, text

    from app import models  # noqa: F401
    from app.db import Base

    src_tables = set(inspect(src).get_table_names())
    tgt_tables = set(inspect(tgt).get_table_names())
    out = {"tables": [], "problems": []}
    with src.connect() as c:
        for t in Base.metadata.sorted_tables:
            name = t.name[len(PREFIX):]
            row = {"source": name, "target": t.name, "rows": None, "target_rows": None}
            if name not in src_tables:
                row["rows"] = 0
                row["note"] = "源库没有这张表（新功能还没建过），目标建空表"
            else:
                row["rows"] = c.execute(text(f'SELECT count(*) FROM "{name}"')).scalar()
                for col in float_columns(t):
                    bad = c.execute(text(f"SELECT count(*) FROM \"{name}\" WHERE \"{col}\" = 'NaN'::float "
                                         f"OR \"{col}\" IN ('Infinity'::float, '-Infinity'::float)")).scalar()
                    if bad:
                        out["problems"].append(f"{name}.{col} 有 {bad} 个 NaN / 无穷大，MySQL 存不了（迁移时改成空）")
            if t.name in tgt_tables:
                with tgt.connect() as tc:
                    row["target_rows"] = tc.execute(text(f"SELECT count(*) FROM `{t.name}`")).scalar()
                if row["target_rows"]:
                    out["problems"].append(f"目标库已有 {t.name} 且有 {row['target_rows']} 行：不会覆盖，先确认")
            out["tables"].append(row)
    return out


def _clean(row: dict, floats: list[str]) -> dict:
    for k in floats:
        v = row.get(k)
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            row[k] = None
    return row


def apply(src, tgt) -> list[dict]:
    from sqlalchemy import inspect, select, text

    from app import models  # noqa: F401
    from app.db import Base

    Base.metadata.create_all(tgt)
    src_tables = set(inspect(src).get_table_names())
    report = []
    with src.connect() as sc, tgt.connect() as tc:
        tc.execute(text("SET FOREIGN_KEY_CHECKS = 0"))  # 按外键顺序复制；自引用 / 循环引用也不会卡住
        for t in Base.metadata.sorted_tables:
            name = t.name[len(PREFIX):]
            if name not in src_tables:
                report.append({"table": t.name, "rows": 0, "copied": 0, "seconds": 0})
                continue
            started = time.perf_counter()
            src_cols = {col["name"] for col in inspect(src).get_columns(name)}
            cols = [col for col in t.columns if col.name in src_cols]
            floats = [c for c in float_columns(t) if c in src_cols]
            pk = [col for col in t.primary_key.columns]
            q = select(*[text(f'"{col.name}"') for col in cols]).select_from(text(f'"{name}"'))
            if pk:
                q = q.order_by(*[text(f'"{col.name}"') for col in pk])
            copied = 0
            result = sc.execution_options(stream_results=True).execute(q)
            while True:
                chunk = result.fetchmany(BATCH)
                if not chunk:
                    break
                rows = [_clean(dict(zip([c.name for c in cols], r)), floats) for r in chunk]
                tc.execute(t.insert(), rows)
                tc.commit()
                copied += len(rows)
            report.append({"table": t.name, "rows": copied, "copied": copied,
                           "seconds": round(time.perf_counter() - started, 1)})
            print(f"  {t.name}: {copied} 行，{report[-1]['seconds']} 秒", flush=True)
        tc.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        tc.commit()
    return report


def verify(src, tgt) -> list[str]:
    from sqlalchemy import inspect, text

    from app.db import Base

    src_tables = set(inspect(src).get_table_names())
    bad = []
    with src.connect() as sc, tgt.connect() as tc:
        for t in Base.metadata.sorted_tables:
            name = t.name[len(PREFIX):]
            if name not in src_tables:
                continue
            a = sc.execute(text(f'SELECT count(*) FROM "{name}"')).scalar()
            b = tc.execute(text(f"SELECT count(*) FROM `{t.name}`")).scalar()
            pk = [c.name for c in t.primary_key.columns]
            if a != b:
                bad.append(f"{t.name}：源 {a} 行，目标 {b} 行")
            elif len(pk) == 1 and a:
                ra = sc.execute(text(f'SELECT min("{pk[0]}"), max("{pk[0]}") FROM "{name}"')).one()
                rb = tc.execute(text(f"SELECT min(`{pk[0]}`), max(`{pk[0]}`) FROM `{t.name}`")).one()
                if tuple(ra) != tuple(rb):
                    bad.append(f"{t.name}：主键范围不一致 源 {tuple(ra)} 目标 {tuple(rb)}")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="业务数据 PostgreSQL → MySQL（默认只检查）")
    ap.add_argument("--apply", action="store_true", help="建表并复制数据（先停 main.py）")
    ap.add_argument("--mysql-url", default=DEFAULT_MYSQL, help="目标 MySQL 连接串（不带密码时用 DATABASE_PASSWORD）")
    args = ap.parse_args(argv)
    from app import config

    config.get_settings.cache_clear()
    src, tgt = engines(args.mysql_url)
    print(f"源：{src.url.render_as_string(hide_password=True)}")
    print(f"目标：{tgt.url.render_as_string(hide_password=True)}")
    r = check(src, tgt)
    total = sum(x["rows"] or 0 for x in r["tables"])
    print(f"要迁的表 {len(r['tables'])} 张，共 {total} 行：")
    for x in sorted(r["tables"], key=lambda x: -(x["rows"] or 0)):
        tail = f"（目标已有 {x['target_rows']} 行）" if x.get("target_rows") else ""
        print(f"  {x['source']} → {x['target']}：{x['rows']} 行{tail}{('；' + x['note']) if x.get('note') else ''}")
    for p in r["problems"]:
        print("⚠ " + p)
    if not args.apply:
        print("只检查，没有建表、没有写数据。确认后先停 main.py，再加 --apply。")
        return 0
    if any("不会覆盖" in p for p in r["problems"]):
        print("⛔ 目标库已有数据，停止。")
        return 1
    print("开始迁移……")
    report = apply(src, tgt)
    bad = verify(src, tgt)
    if bad:
        print("❌ 核对不通过：\n  " + "\n  ".join(bad))
        return 1
    from app.db import install_credential_guards

    install_credential_guards(tgt)  # 凭证表“只能覆盖、不能修改”的触发器：建在目标 MySQL
    print(f"✅ 迁移完成：{len(report)} 张表、{sum(x['copied'] for x in report)} 行，逐表行数和主键范围都对上了。")
    print("接下来改 .env：DATABASE_URL 换成 MySQL、加 TABLE_PREFIX=cs_ 和 VECTOR_DATABASE_URL=原 PostgreSQL 连接串，再启动 main.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
