"""人物调度层（Character Direction Sheet，ADR-020 决策三）。

## 它补的是什么缺口

细纲（"发生了什么"）与正文（"怎么写出来"）之间缺一层**人物表演的翻译**：
bible 的人物卡是**静态应然**（冷静、心机深、扮猪吃虎），但模型拿到"叶岚冷静"这
三个字，并不知道**在这一场戏里**冷静应该长什么样——于是 9B 模型退化成平均脸：
谁说话都是同一个腔调，配角忽然说出主角的判断，女角色冒出"本小姐"（实测 ch1/ch2）。

本模块在细纲与正文之间插入一次 LLM 调用，把静态人设**编译**成这一场的可执行指令：

    人物名 | 性格要点（≤3） | 本场如何体现（语气/动作/取舍） | 禁忌（绝不能出现的行为）

## 为什么是"离线编译"而不是运行时扮演

ADR-012 的角色演员（SceneBus 运行时多 Agent 会话）成本与不可控性都太高；
本层把扮演要做的事（把人设翻译成此刻表现）**在生成前一次性做完**，产出的是
一份**纯文本指令单**，塞进正文 prompt 的最前部。既拿到扮演的收益，又不引入
运行时会话。

## 失败策略

调度失败（无 LLM / 输出不可解析 / 调用异常）一律返回 `None`，调用方降级为
"仅注入人物卡"，**绝不阻断生成**。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .llm import LLMMessage, LLMRequest

DIRECTOR_PROMPT = """你是人物导演。下面是一场戏的细纲，以及本场出场人物的人物卡。
请为每个出场人物给出**这一场**的表演指令——把静态人设翻译成此刻的具体表现。
{worldview_block}
【本场细纲】
{ev_text}

【出场人物卡】
{cards}
{history_block}
【输出格式】每人一行，竖线分隔，共 4 段：
人物名 | 性格要点（≤3 条，用顿号分隔） | 本场如何体现（语气、动作或取舍，一句话说具体） | 禁忌（本场绝不能出现的行为或口吻）

【纪律】
- 只写上面列出的人物，每人一行，不要多写也不要漏写
- 性格要点必须来自人物卡，不要发明新特质
- "本场如何体现"要具体到可演：说什么话、做什么动作、忍什么、争什么
- 各人的指令必须**彼此不同**——同场人物的腔调与取舍要能区分开
- 卡上标注"实际战力/刻意隐藏"的人物（扮猪吃虎是有意设定）：禁忌只禁"在剧中人物面前
  暴露真实修为"，**不禁在读者视角展现深不可测的从容与压倒性底气**；被挑衅可写表面
  云淡风轻或吃小亏、私下不动声色解决，绝不要写成狼狈、恐惧或真的无力
- 不要输出标题、序号、解释或空行

输出：
"""


@dataclass
class CharacterDirection:
    """单个角色的本场表演指令。"""

    name: str
    traits: list[str] = field(default_factory=list)
    how: str = ""
    taboo: str = ""

    def line(self) -> str:
        parts = [self.name]
        if self.traits:
            parts.append("性格要点：" + "、".join(self.traits))
        if self.how:
            parts.append("本场体现：" + self.how)
        if self.taboo:
            parts.append("禁忌：" + self.taboo)
        return "；".join(parts)


@dataclass
class DirectionSheet:
    """一场戏的完整调度单。"""

    vol: int = 0
    ch: int = 0
    event_index: int = 0
    event_text: str = ""
    characters: list[CharacterDirection] = field(default_factory=list)
    raw: str = ""
    note: str = ""

    def lines(self) -> list[str]:
        """渲染成注入正文 prompt 的行（无内容时返回空列表）。"""
        if not self.characters:
            return []
        return [f"- {c.line()}" for c in self.characters]

    def to_dict(self) -> dict:
        return {
            "vol": self.vol,
            "ch": self.ch,
            "event_index": self.event_index,
            "event_text": self.event_text,
            "characters": [
                {"name": c.name, "traits": c.traits, "how": c.how, "taboo": c.taboo}
                for c in self.characters
            ],
            "raw": self.raw,
            "note": self.note,
        }


# ---------------------------------------------------------------- 人物卡装配（无条件注入）

_CARD_FIELDS_ORDER = ("gender", "power", "core_traits", "arc", "relationships",
                      "behavior_rules", "aliases", "possessions", "status")


def load_characters(ws, project_id: str) -> list[dict]:
    """读 bible/characters.json（失败返回空列表）。

    方案7 运行期兜底：读出时做 name 防污染归一（存量污染卡不再向广播池/
    匹配/实体别名泄漏长句名）。视图级处理，不回写文件（落盘修复走 sync_bible）。
    """
    p = ws._abs(f"{project_id}/bible/characters.json")
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(data, list):
        return []
    try:
        from ..forge.nodes import _coerce_character_card
    except Exception:  # noqa: BLE001 - 归一不可用时退回原样（不阻断）
        return [c for c in data if isinstance(c, dict) and c.get("id")]
    out = []
    for c in data:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        c, _w = _coerce_character_card(dict(c))
        out.append(c)
    return out


def match_cast(chars: list[dict], names: list[str]) -> list[dict]:
    """按姓名/别名把声明的出场人物解析成人物卡（保持 `names` 的顺序，去重）。

    **无条件注入**（ADR-020 决策二）的确定性集合来源：细纲 `出场人物:` 声明 ∪
    事件文本里命中圣经姓名的角色。不做相似度筛选——实测 RAG 漏召回导致人设崩塌。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for nm in names:
        key = str(nm).strip()
        if not key:
            continue
        card = next((c for c in chars if c.get("name") == key), None)
        if card is None:  # 别名回退
            card = next((c for c in chars if key in (c.get("aliases") or [])), None)
        if card is None:  # 名字是别的名字的子串/被包含（"叶师弟" 这类称呼）
            card = next((c for c in chars if key in str(c.get("name") or "")), None)
        if card is None:
            continue
        if card["id"] in seen:
            continue
        seen.add(card["id"])
        out.append(card)
    return out


def cast_from_text(chars: list[dict], text: str, limit: int = 8) -> list[dict]:
    """从事件文本里回扫圣经姓名（模型声明漏人时的兜底）。

    长名优先匹配，避免"云清"命中"云清瑶"之后又被"清瑶"重复命中。
    """
    out: list[dict] = []
    seen: set[str] = set()
    hit_names: list[str] = []  # 已命中的名字（长名优先），用于拦截子串误命中
    names: list[tuple[str, dict]] = []
    for c in chars:
        for nm in [c.get("name"), *(c.get("aliases") or [])]:
            if nm and len(str(nm)) >= 2:
                names.append((str(nm), c))
    names.sort(key=lambda x: len(x[0]), reverse=True)
    for nm, card in names:
        if card["id"] in seen:
            continue
        if any(nm in hit for hit in hit_names):
            continue  # 短名是已命中长名的子串（"清瑶"⊂"云清瑶"），视为同一人
        if nm in text:
            seen.add(card["id"])
            hit_names.append(nm)
            out.append(card)
        if len(out) >= limit:
            break
    return out


def render_card_line(c: dict, _names: dict | None = None) -> str:
    """把一张人物卡渲染成一行（10 字段，ADR-020 决策二：注入字段 6 → 9，M3r 再 +1）。

    原来只注 6 个字段，漏了 `arc`（人物弧线——"他要去哪"是表演的方向盘）、
    `aliases`（称谓——"本小姐"这类性别/身份错乱的源头）、`relationships`
    （关系——对谁什么态度，直接决定对白腔调）。P0-A 补喂（M3r）后追加
    `behavior_rules`（可执行行为规则——危机下怎么做，压制行为漂移与 AI 味）。
    `_names`：id→name 映射（render_cards 注入），把关系目标的 `char:xxx`
    回查成角色名——模型记的是"云清瑶"，不是半英文 id "yun"。
    """
    seg: list[str] = [str(c.get("name") or c.get("id"))]
    gender = c.get("gender")
    if gender in ("male", "female"):  # unknown 是 forge 占位，不是信息，不进 prompt
        seg.append({"male": "男", "female": "女"}.get(str(gender), str(gender)))
    power = c.get("power") if isinstance(c.get("power"), dict) else {}
    lvl = power.get("level")
    fac = power.get("faction")
    if lvl or fac:
        seg.append("／".join(x for x in (fac, lvl) if x))
    if power.get("hidden_level"):  # D8：扮猪吃虎——生成侧要知道越级表现是有意设定
        seg.append(f"实际战力：{power['hidden_level']}（刻意隐藏实力，必要时可越级出手）")
    traits = c.get("core_traits") or []
    if traits:
        seg.append("特质：" + "、".join(str(t) for t in traits[:5]))
    if c.get("arc"):
        arc = str(c["arc"])
        seg.append("弧线：" + (arc if len(arc) <= 60 else arc[:60] + "…"))
    # dp-intent：动机着色。intent=人物此刻最执着的欲望，plan=近段计划；只给"想干什么"，
    # 不改 key_event 目标——让人物带着自己的欲望行动、彼此动机不同，人物不变成剧情提线木偶。
    want = c.get("intent") or c.get("plan")
    if want:
        seg.append("动机：" + str(want)[:80])
    rels = c.get("relationships") or []
    if rels:
        parts = []
        for r in rels[:4] if isinstance(rels, list) else []:
            if isinstance(r, dict) and r.get("type"):
                parts.append(f"{_rel_name(r.get('target'), _names)}（{r['type']}）")
        if parts:
            seg.append("关系：" + "、".join(parts))
    rules = c.get("behavior_rules") or []
    if rules:
        seg.append("行为：" + "；".join(str(x) for x in rules[:3] if str(x).strip()))
    aliases = c.get("aliases") or []
    if aliases:
        seg.append("称谓：" + "、".join(str(a) for a in aliases[:4]))
    poss = c.get("possessions") or []
    if poss:
        seg.append("持有：" + "、".join(str(p) for p in poss[:4]))
    status = c.get("status")
    if status and status != "active":
        seg.append(f"状态：{status}")
    return "｜".join(seg)


def _rel_name(target, names: dict | None = None) -> str:
    """关系目标显示名：`char:xxx` 先回查 bible 角色名（`_names`: id→name），
    查不到才取 id 尾段；非 `char:` 前缀的原样返回。"""
    s = str(target or "")
    if ":" in s:
        if names and s in names:
            return str(names[s])
        return s.split(":")[-1]
    return s


def render_cards(cards: list[dict], all_chars: list[dict] | None = None) -> list[str]:
    """渲染人物卡清单；`all_chars` 给全量圣经卡（缺省取 cards 自身），
    用于把关系目标 id 回查成角色名。"""
    src = all_chars if all_chars else cards
    names = {str(c.get("id") or ""): str(c.get("name") or "")
             for c in src if c.get("id") and c.get("name")}
    return [f"- {render_card_line(c, names)}" for c in cards]


# ---------------------------------------------------------------- 调度单生成


def _parse_directions(content: str, cast_names: list[str]) -> list[CharacterDirection]:
    """解析导演输出；只保留出现在 cast 里的名字（防模型造人）。"""
    out: list[CharacterDirection] = []
    allow = {str(n).strip() for n in cast_names}
    for ln in (content or "").splitlines():
        s = ln.strip().lstrip("-•*").strip()
        s = re.sub(r"^\d+[.、)．]\s*", "", s)
        if "|" not in s:
            continue
        parts = [p.strip() for p in s.split("|")]
        name = parts[0]
        if allow and name not in allow:
            continue
        traits = [t.strip() for t in re.split(r"[、,，/]", parts[1]) if t.strip()] if len(parts) > 1 else []
        how = parts[2] if len(parts) > 2 else ""
        taboo = parts[3] if len(parts) > 3 else ""
        if not (traits or how or taboo):
            continue
        out.append(CharacterDirection(name=name, traits=traits[:3], how=how, taboo=taboo))
    return out


def worldview_block(ws, project_id: str) -> str:
    """S-2（2026-09-15 拍板）：导演层的【世界观基座】。

    原先导演只看到人物卡 power.level 一条间接证据，不知道境界体系与铁律 →
    会产出"越级表演"指令（下游润色不改情节、审校才报战力越级——报错点在最后，
    成本已付）。数据源与批次 2 细纲层 / S-3 forge 节点同一份
    （bible/worldview.json，渲染走 `context.worldview_base_lines`）。
    读取失败或空世界观返回空串（纯增量，绝不阻断调度）。
    """
    wv = _read_json(ws._abs(f"{project_id}/bible/worldview.json"))
    from .context import worldview_base_lines

    lines = worldview_base_lines(wv if isinstance(wv, dict) else {})
    if not lines:
        return ""
    return ("\n【世界观基座】（境界与规则是硬约束：表演指令不得让角色越级装腔、"
            "不得违背铁律）\n" + "\n".join(lines) + "\n")


def build_direction(ws, project_id: str, provider, *, vol: int, ch: int,
                    event_index: int, ev_text: str, cards: list[dict],
                    history_lines: list[str] | None = None,
                    system_prompt: str | None = None,
                    max_tokens: int = 700) -> DirectionSheet | None:
    """生成本场人物调度单（每事件 +1 次 LLM 调用）。失败返回 None（调用方降级）。"""
    sheet = DirectionSheet(vol=vol, ch=ch, event_index=event_index, event_text=ev_text)
    if provider is None or not cards:
        return None
    card_block = "\n".join(render_cards(cards))
    hist_block = ""
    if history_lines:
        hist_block = "\n【人物近况】（他们在前面章节经历了什么，本场应带着这些前提出场）\n" \
                     + "\n".join(history_lines) + "\n"
    prompt = DIRECTOR_PROMPT.format(ev_text=ev_text, cards=card_block,
                                    history_block=hist_block,
                                    worldview_block=worldview_block(ws, project_id))
    try:
        res = provider.complete(
            LLMRequest(
                messages=[LLMMessage(role="system",
                                     content=system_prompt or "你是人物导演。"),
                          LLMMessage(role="user", content=prompt)],
                max_tokens_out=max_tokens,
                temperature=0.4,
                thinking=True,  # 判断类：本场人物调度规划（出场+演绎取向），开思考提人设把关
            )
        )
    except Exception:  # noqa: BLE001 - 调度失败静默降级
        return None
    if res.blocked or not (res.content or "").strip():
        return None
    sheet.raw = res.content.strip()
    names = [str(c.get("name") or "") for c in cards]
    sheet.characters = _parse_directions(sheet.raw, names)
    if not sheet.characters:
        sheet.note = "输出无法解析，降级为仅注入人物卡"
        return None
    return sheet


# ---------------------------------------------------------------- 角色视角记忆的装配与回读（ADR-020 决策四）


def _read_json(path) -> dict | list:
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def recent_perspectives(ws, project_id: str, char_id: str, limit: int = 3) -> list[dict]:
    """读某角色最近的经历条目（含角色视角），按时间正序，取末尾 `limit` 条。"""
    data = _read_json(ws.char_history_path(project_id, char_id))
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)][-limit:]


def render_history_lines(ws, project_id: str, cards: list[dict],
                         limit: int = 3) -> list[str]:
    """**无条件**为每个出场角色渲染"近况行"（决策四·注入策略）。

    不做检索筛选——出场即注入。每条形如：
    `- 叶岚｜1:2（受挫、记恨）：被威压压制…`
    """
    lines: list[str] = []
    for c in cards or []:
        cid = str(c.get("id") or "")
        name = str(c.get("name") or cid)
        if not cid:
            continue
        for e in recent_perspectives(ws, project_id, cid, limit=limit):
            at = e.get("at") or {}
            pos = f"{at.get('vol', '?')}:{at.get('ch', '?')}"
            if at.get("t") is not None:
                pos += f"·第{at['t']}日"
            stance = e.get("stance")
            head = f"{name}｜{pos}" + (f"（{stance}）" if stance else "")
            body = str(e.get("perspective") or e.get("summary") or "").strip()
            if len(body) > 70:
                body = body[:70] + "…"
            if body:
                lines.append(f"- {head}：{body}")
    return lines


def _last_event_gap(ws, project_id: str, char_id: str) -> tuple[int, dict | None]:
    """该角色距最后一次出场隔了多少个**事件**（遍历 plot_events 倒序扫描）。

    返回 (间隔事件数, 最后一条命中事件)；从未出场返回 (大数, None)。
    """
    events = _read_json(ws._abs(f"{project_id}/memory/plot_events.json"))
    if not isinstance(events, list):
        return 999, None
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        if not isinstance(ev, dict):
            continue
        if char_id in (ev.get("participants") or []):
            return len(events) - 1 - i, ev
    return 999, None


def _last_seen_day(ws, project_id: str, char_id: str) -> int | None:
    """该角色最后一次被记经历时的故事内天数（无记录返回 None）。"""
    for e in reversed(recent_perspectives(ws, project_id, char_id, limit=6)):
        t = (e.get("at") or {}).get("t")
        if isinstance(t, int):
            return t
    return None


def needs_readback(ws, project_id: str, char_id: str, *,
                   event_gap: int = 3, day_gap: int = 30) -> tuple[bool, str]:
    """回读触发的**双阈值**判定（ADR-020 决策四）。

    - 主判据：**事件计数** —— 距上次出场 ≥ `event_gap` 个事件。与记忆写入节奏
      对齐（每个事件写一次），event 列表自带顺序，实现零成本。
    - 辅判据：**时间线天数** —— 故事内 ≥ `day_gap` 天未见。覆盖"闭关/远行"这类
      事件数少但故事内跨度大的情形（闭关三年 = 1 个事件）。
    二者**任一满足**即回读。返回 (是否回读, 原因串)。
    """
    gap, _ = _last_event_gap(ws, project_id, char_id)
    if gap >= event_gap:
        return True, f"已 {gap} 个事件未出场（≥{event_gap}）"
    last_t = _last_seen_day(ws, project_id, char_id)
    if last_t is None:
        return False, ""
    try:
        from . import worldstate as wsmod
        from .timeline import now_of

        now = now_of(wsmod.load(ws, project_id))
    except Exception:  # noqa: BLE001
        return False, ""
    if now - last_t >= day_gap:
        return True, f"故事内 {now - last_t} 天未出场（≥{day_gap}）"
    return False, ""


def readback_excerpt(ws, project_id: str, card: dict, *, max_chars: int = 1200,
                     max_blocks: int = 2) -> str:
    """回读内容：该角色最近出场**章**里提到他的片段（非全文）。

    取该章正文中人物姓名首次出现处前后各 `max_chars/2` 字；命中多处时最多取
    `max_blocks` 段。找不到即返回空串（不阻断）。
    """
    cid = str(card.get("id") or "")
    if not cid:
        return ""
    _, ev = _last_event_gap(ws, project_id, cid)
    if ev is None:
        return ""
    at = ev.get("at") or {}
    vol, ch = int(at.get("vol") or 0), int(at.get("ch") or 0)
    if not vol or not ch:
        return ""
    text = ""
    for base in (ws.chapter_path, ws.draft_path):
        try:
            p = base(project_id, vol, ch)
            if p.exists():
                text = p.read_text(encoding="utf-8")
                if text:
                    break
        except Exception:  # noqa: BLE001
            continue
    if not text:
        return ""
    name = str(card.get("name") or "")
    if not name:
        return ""
    half = max(200, max_chars // 2)
    blocks: list[str] = []
    start = 0
    while len(blocks) < max_blocks:
        pos = text.find(name, start)
        if pos < 0:
            break
        lo = max(0, pos - half)
        hi = min(len(text), pos + half)
        blocks.append(("…" if lo > 0 else "") + text[lo:hi].strip()
                      + ("…" if hi < len(text) else ""))
        start = hi
    if not blocks:
        return ""
    return f"【回读·{name}】第 {vol} 卷第 {ch} 章（他上次出场；不要重复已写内容）：\n" \
           + "\n\n".join(blocks)


def save_direction(ws, project_id: str, sheet: DirectionSheet) -> bool:
    """调度单落盘 `memory/directions/v{vol}-c{ch}-e{idx}.json`（ADR-016 文件为主，供事后审阅）。"""
    try:
        d = ws.memory_dir(project_id) / "directions"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"v{sheet.vol}-c{sheet.ch}-e{sheet.event_index}.json"
        p.write_text(json.dumps(sheet.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001 - 落盘失败不影响生成
        return False
