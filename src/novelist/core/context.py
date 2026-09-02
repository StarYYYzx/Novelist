"""生成上下文装配（B-02：把设定圣经注入 LLM）。

## 为什么需要这个模块

`produce_chapter` 原本只把「细纲 + 检索到的记忆」送进 LLM，`system_prompt` 默认只有
一句"你是主编剧，负责指挥创作。"。设定圣经（ADR-004 定义的**唯一事实源**）在生成
上下文中完全缺席。端到端实测后果（见 `novel_workspace/_harness/系统整体测试报告.md` B-02）：

- **性别漂移**：人物卡无性别信息 → 男主苏晚被写成"她""少年女子"；
- **凭空造人**：正文出现圣经中不存在的人物；
- **卷名漂移**：大纲定的卷名被模型改写。

本模块按章节装配「世界观 + 文风 + 本章出场人物卡 + 主角硬约束 + 输出纪律」，
让圣经真正约束生成。它是**纯数据装配**，不做任何 LLM 调用，便于测试与复用。

## 用法

    ctx = build_chapter_context(ws, project_id, vol, ch, memories=[...])
    produce_chapter(..., system_prompt=ctx.system_prompt, final_goal=ctx.user_goal)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

GENDER_CN = {"male": "男", "female": "女", "unknown": "未知"}
GENDER_PRONOUN = {"male": "他", "female": "她", "unknown": "他/她"}

# 输出纪律：这些是实测中真正出过问题的点，逐条对应一个已观察到的缺陷
DISCIPLINE = [
    ("元叙事", "正文里不得出现「第X章」「本章」「上一章」「细纲」「大纲」这类章节/创作术语，"
               "读者不该看到它们。章节标题只在第一行出现一次。"),
    ("完整性", "必须在结尾写一个完整的收束句，以句号、问号、感叹号或引号结尾。"
               "宁可压缩内容，也严禁写到一半停下。"),
    ("人物边界", "只能使用「人物名单」与「本事件相关人物」里给出的人物，不得引入新名字；"
                 "不得改变任何人的性别、境界、阵营。"),
    ("事实一致", "时间、地点、物品去向、人物伤势必须与前情提要保持连续；"
                 "前情里已发生的事不得重复发生或自相矛盾。"),
]


@dataclass
class ChapterContext:
    """一章的生成上下文（圣经注入的产物）。"""

    system_prompt: str
    user_goal: str
    cast: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def _read_json(ws, project_id: str, rel: str, default=None):
    p = ws._abs(f"{project_id}/{rel}")
    if not p or not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def load_bible(ws, project_id: str) -> dict:
    """装载设定圣经全部文件（缺失的给空值，初始化期允许不全）。"""
    return {
        "characters": _read_json(ws, project_id, "bible/characters.json", []) or [],
        "worldview": _read_json(ws, project_id, "bible/worldview.json", {}) or {},
        "style": _read_json(ws, project_id, "bible/style.json", {}) or {},
        "locations": _read_json(ws, project_id, "bible/locations.json", []) or [],
        "plot_threads": _read_json(ws, project_id, "bible/plot_threads.json", []) or [],
        "worldstate": _read_json(ws, project_id, "bible/worldstate.json", {}) or {"characters": {}},
        "items": _read_json(ws, project_id, "bible/items.json", []) or [],
        "skills": _read_json(ws, project_id, "bible/skills.json", []) or [],
        "settings": _read_json(ws, project_id, "bible/settings.json", []) or [],
        "volumes": _read_json(ws, project_id, "outline/volumes.json", []) or [],
        "lessons": _read_json(ws, project_id, "bible/review_lessons.json", []) or [],
    }


def parse_key_events(gist_text: str) -> list[str]:
    """从细纲 front-matter 解析 key_events（声明式事件清单，人工审查第二批第 2 条）。

    细纲已声明事件清单，生成期按它迭代即可——不需要 LLM 递归判断"这一段是不是一个事件"。
    解析容错：无 front-matter / 无该字段 / 空列表 都返回 []。
    """
    m = re.search(r"^key_events:\s*\[(.*)\]\s*$", gist_text or "", re.M)
    if not m:
        return []
    body = m.group(1)
    out = []
    for piece in body.split(","):
        piece = piece.strip().strip("'\"").strip()
        if piece:
            out.append(piece)
    return out


def chapter_cast(characters: list[dict], vol: int, ch: int, gist_text: str = "",
                 *, max_cast: int = 16) -> list[dict]:
    """本章出场人物 = 已登场且仍在场的全部人物（上限 max_cast）。

    取"已登场（first_appear ≤ 本章）且 status 非 dead/unknown"——**而不是只看细纲点没点名**。
    理由：只按细纲点名会让模型"忘记"已存在的人物（例如上一章才登场的长老），
    转而在正文里另造一个名字填坑，反而加剧凭空造人（B-02 实测问题之一）。

    超上限时按「细纲点名 / 主角 > 登场更早」优先保留。
    """
    priority: list[str] = []
    rest: list[tuple[tuple[int, int], dict]] = []
    for c in characters:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        if c.get("status") in ("dead", "unknown"):
            continue
        fa = c.get("first_appear") or {}
        fv, fc = int(fa.get("vol", 0) or 0), int(fa.get("ch", 0) or 0)
        appeared = (fv, fc) <= (vol, ch) if fv else True
        if not appeared:
            continue
        is_named = bool(gist_text and c.get("name") and c["name"] in gist_text)
        if is_named or c.get("is_protagonist"):
            priority.append(c["id"])
        rest.append(((fv, fc), c))

    by_id = {c["id"]: c for _, c in rest}
    ordered = [by_id[i] for i in priority if i in by_id]
    ordered += [c for _, c in sorted(rest, key=lambda x: x[0]) if c["id"] not in priority]
    return ordered[:max_cast]


def _mark_protagonist(bible: dict) -> None:
    """把 style.protagonist.id 标记到人物卡上（bible 本身不改）。"""
    proto = (bible.get("style") or {}).get("protagonist") or {}
    pid = proto.get("id")
    if not pid:
        return
    for c in bible.get("characters") or []:
        if isinstance(c, dict) and c.get("id") == pid:
            c["is_protagonist"] = True


def build_system_prompt(bible: dict, cast: list[dict], vol: int, ch: int,
                        *, genre: str | None = None) -> str:
    """装配 system prompt：世界观 + 文风 + 人物卡 + 输出纪律。

    历史教训（review_lessons.json）不在此注入：第 8 轮已 RAG 化改由知识层检索
    （knowledge.py），system prompt 级无条件注入会诱发"细纲未覆盖误报→提前补写
    后续事件"（prompt 作用审计 §2.1，lessons 死参数已删）。
    """
    wv = bible.get("worldview") or {}
    st = bible.get("style") or {}
    levels = ((wv.get("power_system") or {}).get("levels")) or []
    ps_note = ((wv.get("power_system") or {}).get("note")) or ""
    rules = wv.get("rules") or []
    banned = st.get("forbidden_words") or []
    modern = wv.get("modern_words") or []  # 世界观层现代词禁令（forge 产出，装配补读）
    glossary = st.get("glossary") or []
    threads = bible.get("plot_threads") or []
    volumes = bible.get("volumes") or []

    L: list[str] = []
    L.append(f"你是一部{genre or ''}长篇小说的主编剧，正在写第 {vol} 卷第 {ch} 章。")

    # 明线（讨论第 6 轮）：卷主线注入——每章须服务本卷主线，而不是只有细纲要点
    cur_vol = next((v for v in volumes if isinstance(v, dict) and int(v.get("vol", 0) or 0) == vol), None)
    if cur_vol and cur_vol.get("summary"):
        L.append("")
        L.append(f"【本卷主线】（每章都要服务于它）：{cur_vol['summary']}")

    if wv:
        L.append("")
        L.append("【世界设定】")
        if wv.get("name"):
            L.append(f"- 世界：{wv['name']}")
        if wv.get("summary"):
            L.append(f"- 概要：{wv['summary']}")
        if levels:
            L.append(f"- 境界体系：{'、'.join(levels)}（表述必须统一，不得混用「层」「重」等不同划分）")
            if ps_note:
                L.append(f"  说明：{ps_note}")
        for r in rules:
            L.append(f"- 铁律：{r}")
        civs = wv.get("civilizations") or []
        if civs:
            L.append(f"- 文明：{'、'.join(str(x) for x in civs)}")
        systems = wv.get("systems") or []
        if systems:
            L.append(f"- 体系：{'、'.join(str(x) for x in systems)}")
        # 势力不在此全量注入（讨论第 8 轮 RAG）：按事件相关性由知识层检索，事件级注入
        fluctuates = wv.get("realm_fluctuates") or []
        if fluctuates:
            L.append("- 境界波动：下列角色/对象的境界随系统绑定/解除而变动，"
                     "写作时不得写死为固定境界——" + "、".join(str(x) for x in fluctuates))

    if st:
        L.append("")
        L.append("【文风约束】")
        for key, label in (("pov", "视角"), ("tense", "时态"), ("narration", "叙述")):
            if st.get(key):
                L.append(f"- {label}：{st[key]}")
        if st.get("tone"):
            L.append(f"- 笔调：{'、'.join(st['tone'])}")
        if st.get("target_words_per_chapter"):
            L.append(f"- 目标篇幅：约 {st['target_words_per_chapter']} 字")
        if banned:
            L.append(f"- 禁用词，出现即失败：{'、'.join(banned)}")
        if modern:
            L.append(f"- 禁用现代词/现代概念（出现即失败）：{'、'.join(modern)}")
        if glossary:
            L.append("- 专有名词（写法必须固定）：" + "、".join(
                f"{g.get('term')}（{g.get('note', '')}）" for g in glossary if isinstance(g, dict)))

    proto = st.get("protagonist") or {}
    if proto.get("name"):
        pronoun = GENDER_PRONOUN.get(proto.get("gender"), "他/她")
        L.append("")
        L.append(f"【主角硬约束】主角是{proto['name']}，性别"
                 f"{GENDER_CN.get(proto.get('gender'), '未知')}，"
                 f"人称代词一律用「{pronoun}」，绝不可混用。")

    if cast:
        L.append("")
    # 全员名单（防造人）：只给名字+境界一行；人物细节由知识层按事件相关性检索，
    # 事件级注入相关人物卡（讨论第 8 轮 RAG——system 不再全量塞人物卡）
    if cast:
        names = "、".join(
            f"{c.get('name', '?')}（{((c.get('power') or {}).get('level') or '?')}）"
            for c in cast)
        L.append("")
        L.append(f"【人物名单】（只能使用下列人物，不得新造名字；本事件相关人物的"
                 f"性格/弧线卡片见本事件提示）：{names}")

    # 伏笔与历史教训不在此注入（讨论第 8 轮 RAG）：由知识层按事件相关性检索，
    # 事件级注入【相关知识·伏笔/教训】段（related.thread / related.lesson）

    L.append("")
    L.append("【输出纪律】")
    for name, rule in DISCIPLINE:
        L.append(f"- {name}：{rule}")
    L.append("")
    L.append("【输出格式】直接输出正文。第一行是章节标题，形如：## 第X章 标题。"
             "不要写卷名，不要写前言、后记、注释或任何说明文字。")
    return "\n".join(L)


def build_chapter_context(
    ws,
    project_id: str,
    vol: int,
    ch: int,
    *,
    gist_text: str | None = None,
    memories: list[str] | None = None,
    genre: str | None = None,
    gist_max_chars: int = 1200,
    max_cast: int = 16,
) -> ChapterContext:
    """装配一章的完整生成上下文（圣经注入的入口）。"""
    bible = load_bible(ws, project_id)
    _mark_protagonist(bible)

    if gist_text is None:
        gist = ws.outline_chapter_path(project_id, vol, ch)
        gist_text = gist.read_text(encoding="utf-8") if gist.exists() else ""

    cast = chapter_cast(bible.get("characters") or [], vol, ch, gist_text, max_cast=max_cast)
    # 主角必须在场：限知视角的载体缺席会导致视角崩坏
    proto_id = (bible.get("style") or {}).get("protagonist", {}).get("id")
    if proto_id and not any(c.get("id") == proto_id for c in cast):
        hit = next((c for c in (bible.get("characters") or [])
                    if isinstance(c, dict) and c.get("id") == proto_id), None)
        if hit:
            cast.append(hit)
    if not cast and bible.get("characters"):
        cast = [c for c in bible["characters"] if isinstance(c, dict)][:1]

    system_prompt = build_system_prompt(bible, cast, vol, ch, genre=genre)

    goal: list[str] = [f"请撰写第 {vol} 卷第 {ch} 章正文。"]
    if gist_text:
        # ADR-020 决策一（延迟拟题）：细纲 md 首行的 `# 第 X 章 <标题>` 必须剥掉再进
        # 生成 prompt——qwen3.6 真机实测：标题留在上下文里模型会概率性在事件边界
        # 复述成「## 第X章 <细纲标题>」卡进正文（元叙事泄漏，ch2/ch5 均中招）。
        # 标题只在章末由 _title_chapter 依据正文拟写（forge 的 render_gist_md 已剥，
        # 但 produce_chapter 真实链路读 outline/*.md 原文，此前从未剥过）。
        _g = gist_text
        if _g.lstrip().startswith("# "):
            _g = _g.lstrip()
            _nl = _g.find("\n")
            _g = _g[_nl + 1:] if _nl >= 0 else ""
        goal += ["", "【细纲】（必须逐条落实，不得遗漏要点）", _g.strip()[:gist_max_chars]]
    if memories:
        goal += ["", "【前情提要】（先忆：必须与以下已发生的事实保持连续）", *memories]
    goal += ["", "要求：严格按细纲推进，写完本章全部要点，结尾必须是一个完整的收束句。"]

    return ChapterContext(
        system_prompt=system_prompt,
        user_goal="\n".join(goal),
        cast=cast,
        meta={"vol": vol, "ch": ch, "cast_ids": [c.get("id") for c in cast]},
    )
