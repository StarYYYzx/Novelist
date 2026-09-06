import sqlite3
from pathlib import Path

pj = Path(r"E:\360MoveData\Users\Administrator\Desktop\novelist\novel_workspace\proj-20260906201649")
db = sqlite3.connect(str(pj / ".index.db"))
tables = [r[0] for r in db.execute(
    "select name from sqlite_master where type='table'")]
print("tables:", tables)
for t in tables:
    try:
        n = db.execute(f"select count(*) from {t}").fetchone()[0]
        cols = [c[1] for c in db.execute(f"pragma table_info({t})")]
        print(f"  {t}: {n} rows; cols={cols}")
    except Exception as e:
        print("  ", t, e)

# look for chapter text content in tables
for t in tables:
    cols = [c[1] for c in db.execute(f"pragma table_info({t})")]
    text_cols = [c for c in cols if any(k in c for k in ("text", "content", "summary", "body", "draft"))]
    if text_cols:
        for c in text_cols:
            try:
                rows = db.execute(f"select {c} from {t} where {c} like '%杨铭%' limit 3").fetchall()
                if rows:
                    print(f"  [{t}.{c}] found yangming: {len(rows)}; sample len={len(rows[0][0] or '')}")
            except Exception:
                pass