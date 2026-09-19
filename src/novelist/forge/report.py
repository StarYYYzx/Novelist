"""Forge 构建报告（docs/10 §9.5，M3l F5）：双写 `workspace/forge/report.md`（全量）+ `reports/stats/forge-<ts>.md`（摘要）。

数据来源全部确定性、零 LLM：

- 来源分布：`blueprint.provenance`（{path: {src, confidence}}），按 src 聚合 + 低置信缺口。
- 校验结果：`validate_project_full`（V1–V6）。
- 调用数与耗时：transcript `build.start/build.end`、`state.calls_used`、`build_node` 事件数。
- token 用量与估算成本：transcript `build_node` 的 tokens_in/tokens_out 聚合
  （F5b 起 usage 上链；fake/本地模型 usage=0 时成本记 0 并注明）。
- 每节点 decide+reason 摘要：`workspace/forge/nodes/*.json`。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..core.llm import COST_PER_M_CNY
from ..storage.workspace import Workspace
from .state import Blueprint, ForgeState, read_transcript
from .validate import validate_project_full

# 估算单价（¥/1M tokens）——取自 `core/llm.COST_PER_M_CNY`（单一价表，P0-1 集中）。
# forge 的 transcript 只聚合 tokens_in/out、不分命中/未命中，故按**未命中价**估输入
# （成本的保守上界）。report 中注明「估算」；fake/本地 provider tokens=0 时成本恒为 0。
COST_PER_M = {"in": COST_PER_M_CNY["in_miss"], "out": COST_PER_M_CNY["out"]}

FULL_REL = "workspace/forge/report.md"


def _provenance_stats(bp: Blueprint) -> dict:
    """来源分布 {src: count} 与低置信条目（confidence < 0.8）。"""
    prov = bp.data.get("provenance") or {}
    counts: dict[str, int] = {}
    conf_sum: dict[str, float] = {}
    low_conf: list[tuple[str, str, float]] = []
    for path, p in prov.items():
        if not isinstance(p, dict):
            continue
        src = str(p.get("src", "?"))
        conf = float(p.get("confidence", 0.0) or 0.0)
        counts[src] = counts.get(src, 0) + 1
        conf_sum[src] = conf_sum.get(src, 0.0) + conf
        if conf < 0.8:
            low_conf.append((path, src, conf))
    return {"counts": counts, "conf_sum": conf_sum, "low_conf": sorted(low_conf)}


def _transcript_summary(ws: Workspace, project_id: str) -> dict:
    """transcript 聚合：最近一轮 build/roll 的耗时、build_node 的 token 总量。"""
    rows = read_transcript(ws, project_id)
    tokens_in = tokens_out = node_calls = 0
    retried = 0
    started_at = ended_at = None
    for r in rows:
        ev = r.get("event")
        if ev == "build_node":
            node_calls += 1
            tokens_in += int(r.get("tokens_in", 0) or 0)
            tokens_out += int(r.get("tokens_out", 0) or 0)
            if r.get("retried"):
                retried += 1
        elif ev in ("build.start", "roll.start"):
            started_at = r.get("t")
        elif ev in ("build.end", "roll.end"):
            ended_at = r.get("t")
    return {"node_calls": node_calls, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "retried": retried, "started_at": started_at, "ended_at": ended_at,
            "events": len(rows)}


def _nodes_summary(ws: Workspace, project_id: str) -> list[dict]:
    d = ws._abs(f"{project_id}/workspace/forge/nodes")  # noqa: SLF001
    out: list[dict] = []
    if not d.exists():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            node = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"node": node.get("node_id") or f.stem,
                    "kind": node.get("kind", "?"),
                    "decide": node.get("decide", "?"),
                    "reason": (node.get("reason") or "")[:160]})
    return out


def render_report(ws: Workspace, project_id: str, *, smoke: bool = False) -> str:
    """渲染全量报告 markdown（含校验小节；有 block 也在报告里给出结论）。"""
    bp = Blueprint.load(ws, project_id)
    state = ForgeState.load(ws, project_id)
    meta = bp.get("meta") or {}
    scale = meta.get("scale") or {}
    vres = validate_project_full(ws, project_id, smoke=smoke)
    prov = _provenance_stats(bp)
    tr = _transcript_summary(ws, project_id)
    nodes = _nodes_summary(ws, project_id)

    L: list[str] = []
    L.append(f"# Forge 构建报告 — {project_id}")
    L.append("")
    L.append(f"- 蓝图 rev={bp.data.get('rev', '?')} | 阶段={state.stage} | 累计调用={state.calls_used}")
    L.append(f"- 标题：{meta.get('title', '—')} | 类型：{meta.get('genre', '—')}"
             f" | 模板：{meta.get('template', '—')}")
    if meta.get("logline"):
        L.append(f"- 卖点：{meta['logline']}")
    L.append(f"- 规模：{scale.get('volumes', '?')} 卷 × {scale.get('chapters_per_volume', '?')} 章"
             f"（每章目标 {scale.get('target_words_per_chapter', '?')} 字）")
    L.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    L.append("")

    L.append("## 1. 来源分布（provenance）")
    L.append("")
    L.append("| 来源 | 条目数 | 平均置信度 |")
    L.append("|---|---|---|")
    if prov["counts"]:
        for src in sorted(prov["counts"], key=lambda s: -prov["counts"][s]):
            n = prov["counts"][src]
            avg = prov["conf_sum"][src] / n if n else 0.0
            L.append(f"| {src} | {n} | {avg:.2f} |")
    else:
        L.append("|（无 provenance 记录）| 0 | — |")
    L.append("")
    if prov["low_conf"]:
        L.append("**低置信缺口（confidence < 0.8）**：")
        for path, src, conf in prov["low_conf"][:15]:
            L.append(f"- `{path}`（{src}, {conf:.2f}）")
    else:
        L.append("低置信缺口：无")
    L.append("")

    L.append("## 2. 校验结果（V1–V6）")
    L.append("")
    L.append(f"**结论：{'通过（可推进细纲）' if vres.ok else '不通过（有阻断项）'}**"
             f" ｜ block {len(vres.blocks)} / warn {len(vres.warns)}"
             f"{' ｜ 冒烟已跑且通过' if vres.smoke_ok else ''}"
             f"{' ｜ 冒烟未跑' if not vres.smoke_ran else ''}")
    if vres.blocks:
        L.append("")
        L.append("**阻断项**：")
        for f_ in vres.blocks:
            L.append(f"- [{f_.code}] {f_.message}")
    if vres.warns:
        L.append("")
        L.append("**提示项**：")
        for f_ in vres.warns:
            L.append(f"- [{f_.code}] {f_.message}")
    L.append("")

    L.append("## 3. 调用与耗时")
    L.append("")
    L.append(f"- 构建节点调用（transcript build_node）：{tr['node_calls']}"
             f"（其中重试 {tr['retried']}）")
    L.append(f"- 累计调用（project.json forge.calls_used）：{state.calls_used}")
    L.append(f"- transcript 事件总数：{tr['events']}")
    if tr["started_at"] and tr["ended_at"]:
        L.append(f"- 最近一轮起止：{tr['started_at']} → {tr['ended_at']}")
    L.append("")

    L.append("## 4. token 用量与估算成本")
    L.append("")
    L.append(f"- tokens_in 合计：{tr['tokens_in']}")
    L.append(f"- tokens_out 合计：{tr['tokens_out']}")
    if tr["tokens_in"] or tr["tokens_out"]:
        cost = (tr["tokens_in"] / 1_000_000) * COST_PER_M["in"] \
            + (tr["tokens_out"] / 1_000_000) * COST_PER_M["out"]
        L.append(f"- 估算成本：¥{cost:.4f}"
                 f"（单价 ¥{COST_PER_M['in']}/1M in + ¥{COST_PER_M['out']}/1M out，"
                 f"DeepSeek 参考价，仅供参考）")
    else:
        L.append("- 估算成本：¥0（usage 未上报——fake/本地 provider 不计费）")
    L.append("")

    L.append("## 5. 节点摘要（decide + reason）")
    L.append("")
    if nodes:
        L.append("| 节点 | kind | decide | reason |")
        L.append("|---|---|---|---|")
        for n in nodes:
            reason = n["reason"].replace("|", "\\|").replace("\n", " ")
            L.append(f"| `{n['node']}` | {n['kind']} | {n['decide']} | {reason} |")
    else:
        L.append("（无 nodes/ 产物——尚未构建）")
    L.append("")
    return "\n".join(L)


def write_reports(ws: Workspace, project_id: str, *, smoke: bool = False) -> tuple[Path, Path]:
    """双写：全量 → workspace/forge/report.md；摘要 → reports/stats/forge-<ts>.md。

    摘要 = 全量的头部（来源分布表 + 校验结论 + 调用/token 行），供 stats 目录纵览。
    """
    full = render_report(ws, project_id, smoke=smoke)
    full_path = ws._abs(f"{project_id}/{FULL_REL}")  # noqa: SLF001
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(full, encoding="utf-8")

    # 摘要：取 # 标题到「3. 调用与耗时」之前（含校验小节）即可
    marker = "## 3. 调用与耗时"
    head = full.split(marker, 1)[0].rstrip() if marker in full else full
    stats_path = ws._abs(  # noqa: SLF001 - 2026-09-19 修复：补项目前缀（此前写沙箱根，跨项目串扰）
        f"{project_id}/reports/stats/forge-{time.strftime('%Y%m%d-%H%M%S')}.md")
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(head + "\n", encoding="utf-8")
    return full_path, stats_path
