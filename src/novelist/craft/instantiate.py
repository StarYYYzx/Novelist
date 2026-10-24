"""工艺卡实例化（DL-015⑦，2026-10-24 拍板：卡 = 构建期参考资料 → 本书完整规则）。

定位变更：卡**不再是运行时注入的指令**——构建期（seed/商讨拍板后）把勾选卡的
通用规范 + 本书拍板项（knobs/meta）**确定性地实例化**为 `style.craft_rules`：

- 生成期注入的是 craft_rules（本书规则），卡的 INJECT 段降级为实例化模板；
- 编译期判定的也是 craft_rules（可机检条目带 check 谓词，见 `eval_craft_checks`）；
- 每一步对着同一份实例化规则，而不是一句注入给模型的期望——
  「系统零发声」类事故（2026-09-05）从希望变成编译错误。

挂在 `style` 字典内的理由：style 段随既有 `sync_bible` 通道流到 bible/style.json，
生成侧（core/context）零新增读取路径，blueprint schema 零侵入。

联动（DL-015 拍板：槽位定"是什么"，卡定"怎么写"）：
- `meta.romance`（无CP/单女主/多女主后宫/副线淡化）→ romance 卡的 mode 参数；
- `meta.protagonist_hint.cheat` → 金手指相关卡的参数提示（无法确定性替换的
  knobs 保留在 rule 文本里，由 book/style 节点（LLM，读卡为参考）后续细化）。
"""

from __future__ import annotations

from typing import Any

# meta.romance 槽位枚举 → romance 卡 mode 取值（一一对应，DL-015 拍板联动）
ROMANCE_MODES = {"无CP": "none", "单女主": "single", "多女主后宫": "harem",
                 "副线淡化": "de-emphasized"}


def instantiate_craft_rules(style: dict, meta: dict) -> list[dict]:
    """把勾选的工艺卡实例化为本书 craft_rules（确定性，零 LLM）。

    `style`：blueprint.style（读 `craft_cards`，写回方由调用方决定）
    `meta`：blueprint.meta（romance / protagonist_hint 联动参数来源）
    返回规则列表，每条：{card, phase, rule, checks, mode?}——
    - `rule`：注入给生成期的规范文本（卡的 INJECT 段，romance 卡按 mode 取小节）；
    - `checks`：机检谓词列表（可空——语义类规范由三视角兜底）。
    """
    from .loader import load_card

    ids = [str(x).strip() for x in (style.get("craft_cards") or []) if str(x).strip()]
    rules: list[dict] = []
    romance_mode = ROMANCE_MODES.get(str(meta.get("romance") or ""), "")
    for cid in ids:
        card = load_card(cid)
        if card is None:
            continue
        entry: dict[str, Any] = {
            "card": card.id,
            "phase": card.phase,
            "rule": card.inject,
            "checks": [dict(c) for c in card.checks],
        }
        if card.id == "romance" and romance_mode:
            entry["mode"] = romance_mode
            entry["rule"] = _romance_rule(card.inject, romance_mode)
        if entry["rule"] or entry["checks"]:
            rules.append(entry)
    return rules


def _romance_rule(inject: str, mode: str) -> str:
    """romance 卡的 INJECT 按 `## mode：<取值>` 小节切分——只保留公共头 + 选中节。

    不能只做"取 marker 之后"（marker 之前还挂着其它 mode 的节）；用 re.split 按
    节标切开后重组。无任何分节标记时原样返回（卡内容升级前的兜底）。
    """
    import re

    parts = re.split(r"(?m)^## mode：(\S+)\s*$", inject)
    header = parts[0].strip()
    for i in range(1, len(parts) - 1, 2):
        if parts[i] == mode:
            section = parts[i + 1].strip()
            return (header + "\n\n" + section).strip() if header else section
    return inject


def eval_craft_checks(rules: list[dict], chapter_text: str) -> list[dict]:
    """对一章正文跑 craft_rules 的机检谓词（编译器第一道闸——零 LLM）。

    返回违例列表：{card, check_id, kind, note, quote}——quote 取首个命中上下文
    （供三视角聚合与修复循环复用）。非法 pattern/check 项静默跳过（卡是参考资料，
    坏谓词不炸编译）；`phase != compile` 的条目不检（polish 相归终润）。
    """
    import re

    out: list[dict] = []
    for r in rules or []:
        if r.get("phase") not in (None, "compile", "both"):
            continue
        for c in (r.get("checks") or []):
            kind = str(c.get("kind") or "")
            pattern = str(c.get("pattern") or "")
            cid = str(c.get("id") or f"{r.get('card')}:{kind}")
            note = str(c.get("note") or "")
            try:
                rx = re.compile(pattern) if pattern else None
            except re.error:
                continue
            if kind in ("min_count", "max_count") and rx is not None:
                n = len(rx.findall(chapter_text))
                mn = int(c.get("min") or 0)
                mx = c.get("max")
                if kind == "min_count" and n < mn:
                    m = rx.search(chapter_text)
                    out.append({"card": r.get("card"), "check_id": cid, "kind": kind,
                                "note": note or f"正则 {pattern} 命中 {n} 次 < 下限 {mn}",
                                "quote": (m.group(0) if m else pattern)})
                elif kind == "max_count" and mx is not None and n > int(mx):
                    m = rx.search(chapter_text)
                    out.append({"card": r.get("card"), "check_id": cid, "kind": kind,
                                "note": note or f"正则 {pattern} 命中 {n} 次 > 上限 {mx}",
                                "quote": (m.group(0) if m else pattern)})
            elif kind == "contains" and rx is not None and not rx.search(chapter_text):
                out.append({"card": r.get("card"), "check_id": cid, "kind": kind,
                            "note": note or f"正文缺少必须出现的 {pattern}",
                            "quote": pattern})
            elif kind == "not_contains" and rx is not None and rx.search(chapter_text):
                m = rx.search(chapter_text)
                out.append({"card": r.get("card"), "check_id": cid, "kind": kind,
                            "note": note or f"正文出现禁用内容 {pattern}",
                            "quote": m.group(0)})
    return out
