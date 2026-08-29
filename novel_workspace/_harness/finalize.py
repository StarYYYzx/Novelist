"""系统整体测试收尾：转正 → 一致性 → 扫描 → 导出 → 统计。

其中「扫描」部分（未建档人物 / 禁用词 / 性别漂移 / 章节完整性）是系统**不提供**的检查，
本脚本代为执行，用以暴露一致性规则引擎的覆盖盲区（R-REF 只校验 relationships.target，不扫正文）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.core.export import collect_stats, export_project  # noqa: E402
from novelist.core.session import SessionInfo  # noqa: E402
from novelist.core.tools import PermissionGate  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402
from novelist.tools import build_registry  # noqa: E402
from novelist.consistency import run_consistency  # noqa: E402

PID = "proj-duanyu"
WS = ROOT / "novel_workspace"
ws = Workspace(root=str(WS))
sess = SessionInfo(project_id=PID, agent="orchestrator")

bible = json.loads((WS / PID / "bible/characters.json").read_text(encoding="utf-8"))
style = json.loads((WS / PID / "bible/style.json").read_text(encoding="utf-8"))
names = [c["name"] for c in bible] + [a for c in bible for a in (c.get("aliases") or [])]
proto = style.get("protagonist") or {}
proto_name = proto.get("name", "")


def promote_all() -> int:
    """草稿转正。promote_draft 是 sensitive，默认 ask → 需策略放行；CLI 无 promote 命令。"""
    gate = PermissionGate(profiles={"supervised": {"sensitive": "allow", "danger": "deny", "tools": {}}})
    reg = build_registry(ws, gate=gate)
    n = 0
    for f in sorted((WS / PID / "drafts" / "chapters").glob("*.md")):
        m = re.match(r"(\d+)-(\d+)\.md$", f.name)
        if not m:
            continue
        r = reg.invoke(sess, "promote_draft", {"vol": int(m.group(1)), "ch": int(m.group(2))})
        if r.status == "ok":
            n += 1
    return n


def scan_chapters() -> dict:
    """系统不做的正文扫描：未建档人物 / 禁用词 / 性别漂移 / 截断。"""
    found_unknown: dict[str, list[str]] = {}
    banned_hits: list[str] = []
    pronoun_drift: list[str] = []
    truncated: list[str] = []
    word_total = 0
    per_chapter: dict[str, int] = {}

    for f in sorted((WS / PID / "chapters").glob("*.md")):
        text = f.read_text(encoding="utf-8")
        word_total += len(text)
        per_chapter[f.stem] = len(text)

        # 未建档人物：形如「某某长老/师兄/师姐」或直接出现的双字以上人名
        for m in re.finditer(r"([一-龥]{2,4})(?=长老|师兄|师姐|师弟|师妹|宗主|侯|老祖|护法)", text):
            nm = m.group(1)
            if nm not in names and len(nm) >= 2:
                found_unknown.setdefault(nm, []).append(f.stem)
        for w in style.get("forbidden_words") or []:
            if w and w in text:
                banned_hits.append(f"{f.stem}:{w}")
        # 性别漂移：主角名后紧邻「她」
        for m in re.finditer(re.escape(proto_name) + r".{0,12}她", text):
            pronoun_drift.append(f"{f.stem}: {m.group(0)[:20]}")
        # 截断：末行不是句末标点
        last = [ln for ln in text.strip().splitlines() if ln.strip()]
        if last and last[-1].strip()[-1] not in "。！？」）…\"'":
            truncated.append(f.stem)

    # 人物覆盖度：圣经建档的人物有几个真在正文里出场
    all_text = "".join(
        f.read_text(encoding="utf-8") for f in sorted((WS / PID / "chapters").glob("*.md"))
    )
    appeared = [c["name"] for c in bible if c["name"] in all_text]
    absent = [c["name"] for c in bible if c["name"] not in all_text]

    return {
        "unknown_characters": {k: sorted(set(v)) for k, v in found_unknown.items()},
        "banned_word_hits": banned_hits,
        "pronoun_drift": pronoun_drift,
        "truncated_chapters": truncated,
        "total_chars": word_total,
        "per_chapter_chars": per_chapter,
        "characters_appeared": appeared,
        "characters_absent": absent,
    }


def main() -> None:
    print("=== 1) 草稿转正（promote_draft，sensitive，策略放行）===")
    n = promote_all()
    print(f"   转正 {n} 章 -> chapters/")

    print("\n=== 2) 一致性规则引擎（系统自带）===")
    alerts = run_consistency(ws, PID)
    print(f"   告警 {len(alerts)} 条")
    for a in alerts[:10]:
        print(f"     [{a.level}/{a.rule_id}] {a.object_ref}: {a.detail}")

    print("\n=== 3) 正文扫描（系统不提供，本脚本代做）===")
    scan = scan_chapters()
    print(f"   总字数: {scan['total_chars']}")
    print(f"   未建档人物: {scan['unknown_characters'] or '无'}")
    print(f"   禁用词命中: {scan['banned_word_hits'] or '无'}")
    print(f"   性别漂移:   {scan['pronoun_drift'] or '无'}")
    print(f"   疑似截断:   {scan['truncated_chapters'] or '无'}")
    print(f"   人物出场覆盖: {len(scan['characters_appeared'])}/{len(bible)}")
    print(f"   未出场:      {scan['characters_absent'] or '无'}")

    print("\n=== 4) 记忆层统计 ===")
    pe = json.loads((WS / PID / "memory/plot_events.json").read_text(encoding="utf-8"))
    synthetic = [e for e in pe if e.get("type") == "chapter"]
    real = [e for e in pe if e.get("type") != "chapter"]
    print(f"   事件总数 {len(pe)}：章级合成事件 {len(synthetic)}，真实情节事件 {len(real)}")
    idx = json.loads((WS / PID / "memory/fragment_index.json").read_text(encoding="utf-8"))
    print(f"   记忆碎片 {len(idx['fragments'])}（索引 revision {idx['revision']}）")
    hist_dir = WS / PID / "memory/character_histories"
    hist = list(hist_dir.glob("*.json")) if hist_dir.exists() else []
    print(f"   有经历史的人物 {len(hist)} / 建档 {len(bible)}")

    print("\n=== 5) 导出与统计 ===")
    text = export_project(ws, PID)
    out = WS / "_harness" / "断玉青冥.md"
    out.write_text(text, encoding="utf-8")
    s = collect_stats(ws, PID)
    print(f"   发布包: {out}  ({len(text)} 字符)")
    print(f"   已发布章节 {s.chapters} | 草稿 {s.drafts} | 字数 {s.total_words} "
          f"| 剧情事件 {s.plot_events} | 人物 {s.characters}")

    (WS / "_harness" / "scan_report.json").write_text(
        json.dumps({"scan": scan, "consistency_alerts": [
            {"level": a.level, "rule_id": a.rule_id, "object_ref": a.object_ref, "detail": a.detail}
            for a in alerts
        ]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n扫描报告 -> novel_workspace/_harness/scan_report.json")


if __name__ == "__main__":
    main()
