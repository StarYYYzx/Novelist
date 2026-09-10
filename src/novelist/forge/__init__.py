"""构建层 Forge（docs/10）。

对外入口：`show`（F0）/ `run_seed` + `build`（F1）/ F2–F5 补 ingest / ask / validate / report。
模块布局见 docs/10 §11：state / slots / genres / ask / io_console / seed / ingest /
engine / nodes / validate / report。
"""

from __future__ import annotations

from .ask import ConsultResult, run_consult
from .engine import (BuildResult, DiffPlan, RollResult, RollWindowResult, build,
                     diff_affected, roll, roll_window)
from .genres import load_pack, load_pack_for, list_packs
from .ingest import IngestResult, run_ingest
from .io_console import AnswerIO, ConsoleIO
from .report import render_report, write_reports
from .seed import SeedResult, SeedSpec, run_seed
from .shell import ShellResult, run_shell
from .console import ConsoleState, run_console
from .slots import Slot, detect_gaps, default_slots, group_slots, slots_for_genre
from .snapshot import (latest_snapshot, restore_snapshot, snapshots_dir,
                       take_snapshot)
from .state import Blueprint, ForgeState, append_transcript, read_transcript
from .validate import ValidateResult, validate_project_full

__all__ = [
    "Blueprint",
    "ForgeState",
    "append_transcript",
    "read_transcript",
    "load_pack",
    "load_pack_for",
    "list_packs",
    "Slot",
    "default_slots",
    "slots_for_genre",
    "detect_gaps",
    "group_slots",
    "SeedSpec",
    "SeedResult",
    "run_seed",
    "BuildResult",
    "build",
    "RollResult",
    "roll",
    "RollWindowResult",
    "roll_window",
    "DiffPlan",
    "diff_affected",
    "IngestResult",
    "run_ingest",
    "AnswerIO",
    "ConsoleIO",
    "ConsultResult",
    "run_consult",
    "ShellResult",
    "run_shell",
    "ValidateResult",
    "validate_project_full",
    "render_report",
    "write_reports",
    "take_snapshot",
    "latest_snapshot",
    "restore_snapshot",
    "snapshots_dir",
]


def show_summary(bp: Blueprint, state: ForgeState) -> list[str]:
    """`forge show` 的文本渲染（无 LLM，F0）。"""
    lines: list[str] = []
    meta = bp.get("meta") or {}
    scale = meta.get("scale") or {}
    lines.append(f"蓝图 rev={bp.data.get('rev', '?')} | 阶段={state.stage} | 调用={state.calls_used}")
    lines.append(
        f"  标题: {meta.get('title', '—')} | 类型: {meta.get('genre', '—')}"
        f" | 模板: {meta.get('template', '—')}"
    )
    if meta.get("logline"):
        lines.append(f"  卖点: {meta['logline']}")
    lines.append(
        f"  规模: {scale.get('volumes', '?')} 卷 × {scale.get('chapters_per_volume', '?')} 章 × "
        f"{scale.get('target_words_per_chapter', '?')} 字"
    )
    wv = bp.get("worldview") or {}
    if wv.get("name"):
        lines.append(f"  世界观: {wv['name']}")
    if wv.get("power_system") and wv["power_system"].get("levels"):
        lines.append(f"  境界: {'-'.join(wv['power_system']['levels'])}")
    lines.append(
        f"  人物: {len(bp.section('characters'))} | 伏笔: {len(bp.section('threads'))}"
        f" | 卷: {len(bp.section('volumes'))} | 章细纲: {len(bp.section('chapters'))}"
    )
    # 缺口
    gaps = detect_gaps(bp)
    if gaps:
        lines.append(f"  缺口: {len(gaps)} 处")
        for g in gaps[:8]:
            mark = "未填" if g.reason == "unfilled" else "低置信"
            lines.append(f"    [{mark}] {g.slot.key}（{g.slot.label}）")
        if len(gaps) > 8:
            lines.append(f"    …另有 {len(gaps) - 8} 处")
    else:
        lines.append("  缺口: 无")
    # provenance 分布
    prov = bp.provenance_summary()
    lines.append(
        "  来源: " + " ".join(f"{k}={v}" for k, v in prov.items() if v) or " 来源: （无登记）"
    )
    return lines
