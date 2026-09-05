"""Forge 契约校验与定稿（docs/10 §9，M3l F5）：V1–V6。

- V1 双层 schema：core.bible.validate_project（文件层 + 条目层，F0' 契约）+ 蓝图自身 schema。
- V2 交叉引用：relationships.target 存在、threads.scope/target_vol 合法、
  volumes.chapter_range 连续无重叠无缝隙、细纲 characters/threads_involved 指向存在 id。
- V3 覆盖度：每章 ≥1 key_events、主角 vol1ch1 出场、settings ≥ settings_min（默认 5）、
  每卷 threads_to_payoff 非空、style.protagonist 与 characters 一致。
- V4 可写冒烟（smoke=True）：FakeProvider 跑 produce_chapter(1,1)，
  断言 ok 且 bible_injected=True 且 cast 非空（AG1 判据，"完全符合下一层需求"）。
- V5 质量提示（warn）：单章 key_events 数、新实体密度、卷末未回收伏笔数。
- V6 叙事质量软检查（warn，全确定性零 LLM，2026-09-01 拍板 §15-25）：
  章间因果链=相邻章 key_events/characters/threads 承接重合度（pov 不同豁免）；
  节奏曲线=turns/key_events 关键词统计（连续 5+ 章无冲突/高潮、相邻双高潮）；
  伏笔密度=卷内 planted:paid_off > 5:1 且零回收；实体密度=开篇章新人物对照
  PhasePolicy.opening_quota。

全部确定性规则、零 LLM（V4 冒烟的 FakeProvider 也非真实调用）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..core.bible import parse_gist, validate_project as validate_bible_contract
from ..storage.workspace import Workspace
from .state import Blueprint

# 覆盖度阈值（自定决策：docs 未定 N；修仙男频包模板 settings 约 5 条起步）
DEFAULT_SETTINGS_MIN = 5
# V5：单章 key_events 上限 / 出场人物上限 / 缺卡人物容忍数
MAX_KEY_EVENTS = 5
MAX_CAST = 6
MAX_UNCARD_CAST = 2
# V6 节奏关键词（确定性启发；turns 是自由文本，无结构化 conflict/climax 字段）
_CLIMAX_RE = re.compile(r"高潮|决战|爆发|摊牌|生死|对决|climax", re.I)
_TENSION_RE = re.compile(r"冲突|对抗|危机|伏击|袭击|追杀|战斗|阻拦|追兵|battle|conflict", re.I)
RHYTHM_RUN = 5  # 连续 N 章无冲突/高潮 → warn「平缓」


@dataclass
class Finding:
    """一条校验结论：code=V1..V6，level=block（阻断定稿）| warn（仅记录）。"""

    code: str
    level: str  # block | warn
    message: str


@dataclass
class ValidateResult:
    findings: list[Finding] = field(default_factory=list)
    smoke_ran: bool = False
    smoke_ok: bool | None = None

    def add(self, code: str, level: str, message: str) -> None:
        self.findings.append(Finding(code=code, level=level, message=message))

    @property
    def ok(self) -> bool:
        return not any(f.level == "block" for f in self.findings)

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "block"]

    @property
    def warns(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]


# ---- 数据装载辅助 ----
def _load_json(ws: Workspace, project_id: str, rel: str):
    p = ws._abs(f"{project_id}/{rel}")  # noqa: SLF001
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_list(ws: Workspace, project_id: str, rel: str) -> list:
    data = _load_json(ws, project_id, rel)
    return data if isinstance(data, list) else []


# ---- 主入口 ----
def validate_project_full(ws: Workspace, project_id: str, *,
                          smoke: bool = False,
                          settings_min: int = DEFAULT_SETTINGS_MIN) -> ValidateResult:
    """跑 V1–V6；返回结论清单。有 block 即不通过（不推进 pipeline）。"""
    res = ValidateResult()

    _v1_schema(ws, project_id, res)
    bp_data = _load_json(ws, project_id, "workspace/forge/blueprint.json")
    if not isinstance(bp_data, dict):
        return res  # V1 已报「无蓝图/蓝图损坏」

    vols = _load_list(ws, project_id, "outline/volumes.json")
    chars = _chars_of(ws, project_id, bp_data)
    threads = _threads_of(ws, project_id, bp_data)
    style = _style_of(ws, project_id, bp_data)
    gists = _gists_of(ws, project_id, vols)

    _v2_crossref(res, bp_data, vols, chars, threads, gists)
    _v3_coverage(res, vols, chars, gists)
    _v3_settings(ws, project_id, bp_data, settings_min, res)
    _v3_volume_and_style(res, vols, chars, style)
    if smoke:
        _v4_smoke(ws, project_id, res)
    _v5_quality(res, vols, chars, threads, gists)
    _v6_narrative(ws, project_id, res, vols, chars, threads, gists)
    return res


def _chars_of(ws: Workspace, project_id: str, bp_data: dict) -> list[dict]:
    """人物卡：磁盘 bible/characters.json 优先（事实源），蓝图回退（含 role）。"""
    disk = _load_list(ws, project_id, "bible/characters.json")
    if disk:
        bp_by_id = {c.get("id"): c for c in bp_data.get("characters") or []
                    if isinstance(c, dict)}
        out = []
        for c in disk:
            if not isinstance(c, dict):
                continue
            merged = dict(c)
            bp_row = bp_by_id.get(c.get("id"))
            if bp_row and bp_row.get("role"):
                merged["role"] = bp_row["role"]  # 落盘剥离的字段从蓝图补回
            out.append(merged)
        return out
    return [c for c in bp_data.get("characters") or [] if isinstance(c, dict)]


def _threads_of(ws: Workspace, project_id: str, bp_data: dict) -> list[dict]:
    disk = _load_list(ws, project_id, "bible/plot_threads.json")
    if disk:
        return [t for t in disk if isinstance(t, dict)]
    return [t for t in bp_data.get("threads") or [] if isinstance(t, dict)]


def _style_of(ws: Workspace, project_id: str, bp_data: dict) -> dict:
    disk = _load_json(ws, project_id, "bible/style.json")
    if isinstance(disk, dict):
        bp_st = bp_data.get("style") or {}
        # protagonist 派生字段以蓝图为准（sync_bible 也会写，双保险）
        if not disk.get("protagonist") and bp_st.get("protagonist"):
            disk = dict(disk)
            disk["protagonist"] = bp_st["protagonist"]
        return disk
    st = bp_data.get("style")
    return st if isinstance(st, dict) else {}


def _gists_of(ws: Workspace, project_id: str, vols: list[dict]) -> dict[tuple[int, int], dict]:
    out: dict[tuple[int, int], dict] = {}
    for row in vols:
        if not isinstance(row, dict):
            continue
        rng = row.get("chapter_range") or [0, -1]
        try:
            s, e = int(rng[0]), int(rng[1])
        except (TypeError, ValueError, IndexError):
            continue
        for ch in range(s, e + 1):
            g = parse_gist(ws, project_id, int(row.get("vol", 0)), ch)
            if g is not None:
                out[(int(row.get("vol", 0)), ch)] = g
    return out


def _protagonist(chars: list[dict]) -> dict | None:
    return next((c for c in chars
                 if c.get("role") == "protagonist" or c.get("is_protagonist")), None)


# ---- V1：双层 schema ----
def _v1_schema(ws: Workspace, project_id: str, res: ValidateResult) -> None:
    # 蓝图自身 schema（新文件格式，V1 须校验中间态）
    try:
        Blueprint.load(ws, project_id)
    except FileNotFoundError:
        res.add("V1", "block", f"无蓝图 workspace/forge/blueprint.json——先跑 `forge seed`")
        return
    except ValueError as e:
        res.add("V1", "block", f"蓝图未过自身 schema 校验: {e}")
    # bible/outline 契约（文件层 + 条目层，F0' 产物 core.bible.validate_project）
    for v in validate_bible_contract(ws, project_id):
        res.add("V1", "block", f"{v.path} 未过 schema `{v.schema}`: {'; '.join(v.errors)}")


# ---- V2：交叉引用 ----
def _v2_crossref(res: ValidateResult, bp_data: dict, vols: list[dict],
                 chars: list[dict], threads: list[dict],
                 gists: dict[tuple[int, int], dict]) -> None:
    char_ids = {c.get("id") for c in chars if c.get("id")}
    char_names = {c.get("name") for c in chars if c.get("name")}
    thread_ids = {t.get("id") for t in threads if t.get("id")}
    n_vols = len([v for v in bp_data.get("volumes") or [] if isinstance(v, dict)]) \
        or len(vols) or 1

    # relationships.target 必须存在
    for c in chars:
        for r in c.get("relationships") or []:
            if not isinstance(r, dict):
                continue
            t = r.get("target")
            if t and t not in char_ids:
                res.add("V2", "block", f"{c.get('id')} relationships.target `{t}` 不存在")

    # threads.scope / target_vol 合法
    for t in threads:
        scope = t.get("scope")
        if scope is not None and scope not in ("volume", "book"):
            res.add("V2", "block", f"{t.get('id')} scope `{scope}` 非法（volume|book）")
        tv = t.get("target_vol")
        if tv is not None and int(tv) > n_vols:
            res.add("V2", "block", f"{t.get('id')} target_vol={tv} 超出总卷数 {n_vols}")

    # volumes.chapter_range 连续无重叠无缝隙（全局章号）
    if not vols:
        res.add("V2", "block", "outline/volumes.json 缺失或为空——无卷可校验")
    expected = 1
    for row in sorted((v for v in vols if isinstance(v, dict)), key=lambda v: int(v.get("vol", 0))):
        vol = int(row.get("vol", 0))
        rng = row.get("chapter_range") or [None, None]
        try:
            s, e = int(rng[0]), int(rng[1])
        except (TypeError, ValueError, IndexError):
            res.add("V2", "block", f"卷 {vol} chapter_range 非法: {rng!r}")
            continue
        if s != expected:
            res.add("V2", "block",
                    f"卷 {vol} 章区间不连续（期望起 {expected}，实际 {s}）——存在缝隙或重叠")
        if e < s:
            res.add("V2", "block", f"卷 {vol} chapter_range 终止 {e} 小于起始 {s}")
        expected = max(expected, e + 1)

    # 细纲 characters / threads_involved 指向存在的 id
    for (vol, ch), g in sorted(gists.items()):
        for cid in g.get("characters") or []:
            cid = str(cid)
            if cid.startswith("char:"):
                if cid not in char_ids:
                    res.add("V2", "block", f"细纲 {vol}-{ch} 出场人物 `{cid}` 不存在")
            elif cid not in char_names:
                res.add("V2", "block", f"细纲 {vol}-{ch} 出场人物 `{cid}` 既非 id 也非人物名")
        for tid in g.get("threads_involved") or []:
            tid = str(tid)
            if tid and tid not in thread_ids:
                res.add("V2", "block", f"细纲 {vol}-{ch} threads_involved `{tid}` 不存在")


# ---- V3：覆盖度 ----
def _v3_coverage(res: ValidateResult, vols: list[dict], chars: list[dict],
                 gists: dict[tuple[int, int], dict]) -> None:
    # 每章存在且 ≥1 key_events
    for row in vols:
        if not isinstance(row, dict):
            continue
        vol = int(row.get("vol", 0))
        rng = row.get("chapter_range") or [None, None]
        try:
            s, e = int(rng[0]), int(rng[1])
        except (TypeError, ValueError, IndexError):
            continue
        for ch in range(s, e + 1):
            g = gists.get((vol, ch))
            if g is None:
                # 卷 1 由 build 一次性产出，缺失即阻断；卷 2+ 由 forge roll 渐进生成，
                # 未 roll 的卷缺失不算阻断（已生成章在下方独立检查内容）
                if vol == 1:
                    res.add("V3", "block", f"细纲 {vol}-{ch} 缺失（outline/chapters/{vol}-{ch}.md）")
            elif not (g.get("key_events") or []):
                res.add("V3", "block", f"细纲 {vol}-{ch} key_events 为空")

    # 主角在 vol1 ch1 出场
    proto = _protagonist(chars)
    if proto is None:
        res.add("V3", "block", "无主角（characters 中 role=protagonist / is_protagonist 缺失）")
    else:
        g = gists.get((1, 1))
        if g is not None:
            cast = [str(c) for c in (g.get("characters") or [])]
            if proto.get("id") not in cast and proto.get("name") not in cast:
                res.add("V3", "block",
                        f"主角 {proto.get('name')} 未在卷 1 第 1 章出场（出场人物: {cast}）")

    # settings ≥ N
    return  # 占位：settings 计数在 _v3_settings（需要 ws）——见下


def _v3_settings(ws: Workspace, project_id: str, bp_data: dict,
                 settings_min: int, res: ValidateResult) -> None:
    n = len(_load_list(ws, project_id, "bible/settings.json")) \
        or len([s for s in bp_data.get("settings") or [] if isinstance(s, dict)])
    if n < settings_min:
        res.add("V3", "block", f"settings 仅 {n} 条（要求 ≥ {settings_min}）——知识库检索会空转")


def _v3_volume_and_style(res: ValidateResult, vols: list[dict], chars: list[dict],
                         style: dict) -> None:
    # 每卷 threads_to_payoff 非空
    for row in vols:
        if not isinstance(row, dict):
            continue
        if not (row.get("threads_to_payoff") or []):
            res.add("V3", "block", f"卷 {row.get('vol')} threads_to_payoff 为空（卷末无回收清单）")
    # style.protagonist 与 characters 一致
    proto = _protagonist(chars)
    sp = style.get("protagonist") if isinstance(style, dict) else None
    if proto is None:
        return  # 主角缺失已在 V3 报过
    if not isinstance(sp, dict) or not sp.get("name"):
        res.add("V3", "block", "style.protagonist 缺失（应由 role=protagonist 派生）")
    elif sp.get("name") != proto.get("name"):
        res.add("V3", "block",
                f"style.protagonist.name `{sp.get('name')}` 与 characters 主角 "
                f"`{proto.get('name')}` 不一致")


# ---- V4：可写冒烟 ----
_SMOKE_REPLY = ("第一章。叶蓝睁开眼，识海里悬着半明半灭的五五开符印。他试探着握拳，"
                "符印微微一颤——修行，从今日始。")

def _v4_smoke(ws: Workspace, project_id: str, res: ValidateResult) -> None:
    res.smoke_ran = True
    try:
        from ..core.approval import ApprovalQueue
        from ..core.context import build_chapter_context
        from ..core.orchestrator import produce_chapter
        from ..core.session import SessionInfo
        from ..core.tools import PermissionGate
        from ..providers.fake import FakeProvider
        from ..tools import build_registry

        # G7 修复（2026-09-05）：冒烟会把 FakeProvider 产物写进真实项目
        # （drafts/chapters/1-1.md + 记忆回写）——跑前备份受影响文件，跑后还原，
        # 原先会静默覆盖真实草稿。备份失败则拒绝冒烟（宁缺勿错）。
        _targets = [ws.draft_path(project_id, 1, 1)]
        _backup: list[tuple[object, bytes | None]] = []
        try:
            for _p in _targets:
                _backup.append((_p, _p.read_bytes() if _p.exists() else None))
            _mem_events = ws._abs(f"{project_id}/memory/plot_events.json")  # noqa: SLF001
            _backup.append((_mem_events, _mem_events.read_bytes() if _mem_events.exists() else None))
            _wsp = ws.bible_path(project_id, "worldstate")
            _backup.append((_wsp, _wsp.read_bytes() if _wsp.exists() else None))
        except OSError as e:
            res.add("V4", "warn", f"冒烟前备份失败，跳过冒烟: {e}")
            return

        reg = build_registry(ws, gate=PermissionGate(), approvals=ApprovalQueue(),
                             decision_fn=lambda r: "allow")
        prod = produce_chapter(
            ws, project_id, 1, 1, FakeProvider(reply=_SMOKE_REPLY),
            session=SessionInfo(project_id=project_id, agent="forge-validate"),
            registry=reg, prefer_direct=True, generation_tokens=2000,
            inject_bible=True, jit_characters=False)
        if not prod.ok:
            res.add("V4", "block", f"冒烟 produce_chapter(1,1) 失败: {prod.result[:200]}")
            return
        if not prod.bible_injected:
            res.add("V4", "block", "冒烟 bible_injected=False——下一层拿不到圣经注入")
            return
        ctx = build_chapter_context(ws, project_id, 1, 1)
        if not ctx.cast:
            res.add("V4", "block", "冒烟 cast 为空——人物卡没有进入生成上下文")
            return
        res.smoke_ok = True
    except Exception as e:  # noqa: BLE001 - 冒烟任何异常都算不通过
        res.add("V4", "block", f"冒烟异常: {type(e).__name__}: {e}")
    finally:
        try:
            for _p, _blob in _backup:
                if _blob is None:
                    if _p.exists():
                        _p.unlink()
                else:
                    _p.parent.mkdir(parents=True, exist_ok=True)
                    _p.write_bytes(_blob)
        except OSError as e:  # pragma: no cover
            res.add("V4", "warn", f"冒烟后还原失败（真实文件可能被冒烟产物覆盖）: {e}")


# ---- V5：质量提示（warn）----
def _v5_quality(res: ValidateResult, vols: list[dict], chars: list[dict],
                threads: list[dict], gists: dict[tuple[int, int], dict]) -> None:
    char_ids = {c.get("id") for c in chars if c.get("id")}
    for (vol, ch), g in sorted(gists.items()):
        ke = g.get("key_events") or []
        if len(ke) > MAX_KEY_EVENTS:
            res.add("V5", "warn", f"细纲 {vol}-{ch} key_events {len(ke)} 个（>{MAX_KEY_EVENTS}）——单章过载")
        cast = [str(c) for c in (g.get("characters") or [])]
        uncard = [c for c in cast if c.startswith("char:") and c not in char_ids]
        if len(cast) > MAX_CAST:
            res.add("V5", "warn", f"细纲 {vol}-{ch} 出场人物 {len(cast)} 个（>{MAX_CAST}）——认知负担偏大")
        if len(uncard) > MAX_UNCARD_CAST:
            res.add("V5", "warn", f"细纲 {vol}-{ch} 缺卡人物 {len(uncard)} 个（运行期 JIT 补卡兜底）")
    # 卷末未回收伏笔（规划态：target_vol 卷末应回收的线）
    paid_like = {"paid_off", "returned"}
    for row in vols:
        if not isinstance(row, dict):
            continue
        vol = int(row.get("vol", 0))
        open_threads = [t.get("id") for t in threads
                        if int(t.get("target_vol", 0) or 0) == vol
                        and t.get("status") not in paid_like]
        if open_threads:
            res.add("V5", "warn",
                    f"卷 {vol} 末未回收伏笔 {len(open_threads)} 个: {open_threads[:8]}")


# ---- V6：叙事质量软检查（warn，全确定性）----
def _v6_narrative(ws: Workspace, project_id: str, res: ValidateResult,
                  vols: list[dict], chars: list[dict], threads: list[dict],
                  gists: dict[tuple[int, int], dict]) -> None:
    paid_like = {"paid_off", "returned"}
    for row in vols:
        if not isinstance(row, dict):
            continue
        vol = int(row.get("vol", 0))
        rng = row.get("chapter_range") or [None, None]
        try:
            s, e = int(rng[0]), int(rng[1])
        except (TypeError, ValueError, IndexError):
            continue
        seq = [gists.get((vol, ch)) for ch in range(s, e + 1)]

        # 1) 章间因果链：相邻章承接重合度（key_events / characters / threads 任一重合；
        #    pov 不同豁免——刻意换线不算断章）
        for a, b in zip(seq, seq[1:]):
            if a is None or b is None:
                continue
            pov_a, pov_b = str(a.get("pov") or ""), str(b.get("pov") or "")
            overlap = (set(a.get("key_events") or []) & set(b.get("key_events") or [])
                       or set(map(str, a.get("characters") or [])) & set(map(str, b.get("characters") or []))
                       or set(a.get("threads_involved") or []) & set(b.get("threads_involved") or []))
            if not overlap and pov_a == pov_b:
                res.add("V6", "warn",
                        f"卷 {vol} 第 {a.get('ch')}→{b.get('ch')} 章零承接重合——疑似断章")

        # 2) 节奏曲线：turns/key_events 关键词统计
        climax_flags: list[bool] = []
        tension_flags: list[bool] = []
        for g in seq:
            text = "；".join([*(g.get("turns") or []), *(g.get("key_events") or [])]) if g else ""
            climax_flags.append(bool(g) and bool(_CLIMAX_RE.search(text)))
            tension_flags.append(bool(g) and bool((_TENSION_RE or _CLIMAX_RE).search(text)))
        run = 0
        for i, t in enumerate(tension_flags):
            run = 0 if t else run + 1
            if run == RHYTHM_RUN:
                res.add("V6", "warn",
                        f"卷 {vol} 连续 {RHYTHM_RUN} 章无冲突/高潮（至第 {s + i} 章）——节奏平缓")
        for i in range(len(climax_flags) - 1):
            if climax_flags[i] and climax_flags[i + 1]:
                res.add("V6", "warn",
                        f"卷 {vol} 第 {s + i}、{s + i + 1} 章连续高潮——过密")

        # 3) 伏笔密度：卷内 planted:paid_off > 5:1 且零回收 → 堆积
        vol_thread_ids = set()
        for g in seq:
            if g:
                vol_thread_ids |= {str(t) for t in (g.get("threads_involved") or [])}
        for t in threads:
            planted = t.get("planted") or {}
            if int(planted.get("vol", 0) or 0) == vol:
                vol_thread_ids.add(str(t.get("id")))
        paid = [t for t in threads if str(t.get("id")) in vol_thread_ids
                and t.get("status") in paid_like]
        if vol_thread_ids and len(vol_thread_ids) > 5 * len(paid):
            res.add("V6", "warn",
                    f"卷 {vol} 伏笔堆积：planted {len(vol_thread_ids)} : paid_off {len(paid)}（>5:1 且零回收）")

        # 4) 实体密度：开篇章新人物对照 PhasePolicy.opening_quota
        _v6_entity_quota(ws, project_id, res, vol, s, e, seq, chars)


def _v6_entity_quota(ws: Workspace, project_id: str, res: ValidateResult,
                     vol: int, s: int, e: int, seq: list, chars: list[dict]) -> None:
    from ..core.phase import PhasePolicy

    pol = PhasePolicy.load(ws, project_id)
    name_of: dict[str, str] = {}
    for c in chars:
        if c.get("id"):
            name_of[str(c["id"])] = str(c.get("name") or c["id"])
    seen: set[str] = set()
    for offset, g in enumerate(seq):
        if g is None:
            continue
        cast = [name_of.get(str(c), str(c)) for c in (g.get("characters") or [])]
        new = [n for n in cast if n not in seen]
        seen |= set(cast)
        ch_in_vol = offset + 1  # 章在卷内序号（chapter_range 全局号的卷内坐标）
        if ch_in_vol <= pol.opening_chapters and len(new) > pol.opening_quota:
            res.add("V6", "warn",
                    f"卷 {vol} 开篇第 {ch_in_vol} 章新实体 {len(new)} 个"
                    f"（配额 {pol.opening_quota}）——可考虑推迟引入")
