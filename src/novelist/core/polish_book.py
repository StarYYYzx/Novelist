"""全书终润层（DL-015 / ADR-038 批次 3，2026-10-24 拍板）。

两阶段管线的第二阶段：生成期只保正确性（毛坯正文），**人类味道在全书完成后
统一润色**。拍板口径：

- **手动触发**（`novelist polish <dir>`，终润是"交稿"动作，不自动跑）；
- **句级风格改写权限**：只改句子层（句式/节奏/去重/用词），不动情节/事实/段序/
  对白语义；
- **指纹锚定的分窗润色**：全书 10 万+ 字塞不进窗口，跨卷一致性靠
  `style_fingerprint`（全局风格 + 每人物口吻卡）+ 前窗润色尾部锚定达成；
- **润色后事实轻量复检**：工艺规则机检（craft_rules checks）+ 人物名覆盖率 +
  篇幅比——改坏事实即回滚该章（润色权限是句级，复检证明它没越权）；
- **不动原文**：产物落 `chapters_polished/`，报告落 `reports/polish_report.json`，
  用户验收后自行替换。

对白区分度（挂账项）在此落地为口吻卡：指纹按人物聚合"说话方式"特征，
润色指令据此刻画各人语气——每个人说话不再像同一个人。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..core.llm import LLMMessage, LLMRequest
from ..core.output import emit
from ..storage.workspace import Workspace

FINGERPRINT_REL = "bible/style_fingerprint.json"
POLISHED_DIR = "chapters_polished"
REPORT_REL = "reports/polish_report.json"
WINDOW_CHAPTERS = 4          # 每窗章数（拍板 3-5 章一窗，取 4）
PREV_TAIL_CHARS = 800        # 前窗润色尾部锚（接缝语气一致）

_FINGERPRINT_PROTOCOL = """只输出 JSON（不要解释）：
{"global": {"pov_tense": "人称与时态（如：第三人称限知/过去时）",
            "sentence_rhythm": "句长与节奏特征一句话",
            "taboos": ["全书应避免的表达（陈词滥调/AI味句式），最多 8 条"],
            "techniques": ["本书已形成的好手法（应保持），最多 6 条"]},
 "voices": {"<人物名>": {"traits": "此人说话方式一句话（用词/语气/口头禅）",
                          "sample": "最能代表其口吻的一句原文（逐字摘录）"}}}
voices 收本窗出场的主要人物（≤6 人）；没有把握就少收，宁缺勿编。"""


def _chapter_files(ws: Workspace, pid: str, rel: str) -> dict[tuple[int, int], Path]:
    d = ws._abs(f"{pid}/{rel}")  # noqa: SLF001
    out: dict[tuple[int, int], Path] = {}
    if d.exists():
        for f in sorted(d.glob("*.md")):
            v, _, c = f.stem.partition("-")
            try:
                out[(int(v), int(c))] = f
            except ValueError:
                continue
    return out


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------- 风格指纹

def build_fingerprint(ws: Workspace, project_id: str, provider,
                      *, window: int = 8) -> dict:
    """分窗通读全书 → 聚合风格指纹（全局特征 + 人物口吻卡），落 bible/。

    指纹聚合是确定性的（列表去重截断、口吻按人物取首个非空 traits），
    窗口分析各出各的 JSON——无跨窗 LLM 状态，可并行/可重跑。
    """
    files = _chapter_files(ws, project_id, "chapters")
    if not files:
        files = _chapter_files(ws, project_id, "drafts/chapters")
    if not files:
        raise ValueError("全书没有任何章节（chapters/ 或 drafts/chapters/）——无物可润")
    keys = sorted(files)
    merged: dict = {"global": {"taboos": [], "techniques": []}, "voices": {}}
    for i in range(0, len(keys), window):
        chunk = keys[i:i + window]
        corpus = "\n\n".join(f"=== 第{v}卷第{c}章 ===\n{_read(files[(v, c)])[:4000]}"
                             for (v, c) in chunk)
        emit(f"[polish] 指纹窗口 {i // window + 1}/{(len(keys) + window - 1) // window}"
             f"（{chunk[0][0]}-{chunk[0][1]} … {chunk[-1][0]}-{chunk[-1][1]}）")
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user",
                                 content=f"分析这一批章节的风格与人物口吻。\n\n{corpus}\n\n"
                                         + _FINGERPRINT_PROTOCOL)],
            temperature=0.2, max_tokens_out=2000, response_format="json_object",
            thinking=True))
        data = _loads(res.content)
        g = (data or {}).get("global") or {}
        if not merged["global"].get("pov_tense") and g.get("pov_tense"):
            merged["global"]["pov_tense"] = str(g["pov_tense"])[:60]
        if not merged["global"].get("sentence_rhythm") and g.get("sentence_rhythm"):
            merged["global"]["sentence_rhythm"] = str(g["sentence_rhythm"])[:120]
        for key in ("taboos", "techniques"):
            for x in (g.get(key) or [])[:8]:
                s = str(x).strip()
                if s and s not in merged["global"][key] and len(merged["global"][key]) < 8:
                    merged["global"][key].append(s)
        for name, v in ((data or {}).get("voices") or {}).items():
            if isinstance(v, dict) and v.get("traits") \
                    and str(name) not in merged["voices"] and len(merged["voices"]) < 16:
                merged["voices"][str(name)] = {
                    "traits": str(v.get("traits"))[:120],
                    "sample": str(v.get("sample") or "")[:120]}
    merged["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    ws.write_json(ws.bible_path(project_id, "style_fingerprint"), merged)
    return merged


def load_fingerprint(ws: Workspace, project_id: str) -> dict | None:
    p = ws.bible_path(project_id, "style_fingerprint")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _loads(raw: str) -> dict | None:
    text = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        return json.loads(text[s:e + 1])
    except ValueError:
        return None


# ---------------------------------------------------------------- 分窗润色

def _fingerprint_block(fp: dict) -> str:
    g = fp.get("global") or {}
    lines = ["【全书风格指纹（润色必须贴合，不得漂移）】"]
    if g.get("pov_tense"):
        lines.append(f"- 人称/时态：{g['pov_tense']}")
    if g.get("sentence_rhythm"):
        lines.append(f"- 节奏特征：{g['sentence_rhythm']}")
    if g.get("taboos"):
        lines.append("- 避免表达：" + "；".join(g["taboos"][:8]))
    if g.get("techniques"):
        lines.append("- 保持手法：" + "；".join(g["techniques"][:6]))
    voices = fp.get("voices") or {}
    if voices:
        lines.append("- 人物口吻（对白务必按此刻画，不得千人一面）：")
        for name, v in list(voices.items())[:10]:
            lines.append(f"  · {name}：{v.get('traits')}")
    return "\n".join(lines)


def _light_fact_check(original: str, polished: str,
                      craft_rules: list[dict], names: list[str]) -> list[str]:
    """润色后轻量事实复检（句级权限的越权检测）。返回问题列表（空=通过）。

    三项：工艺规则机检（craft_rules checks 原文过的润色稿也必须过）、
    人物名覆盖率（原文出场者润色稿不得消失）、篇幅比（句级改写不增删内容）。
    """
    from ..craft.instantiate import eval_craft_checks

    problems: list[str] = []
    for v in eval_craft_checks(craft_rules, polished):
        problems.append(f"工艺规则违反（{v.get('check_id')}）：{v.get('note')}")
    o_names = {n for n in names if n in original}
    missing = [n for n in sorted(o_names) if n not in polished]
    if missing:
        problems.append(f"人物消失（句级改写不得删人）：{('、'.join(missing))[:120]}")
    if original and polished:
        ratio = len(polished) / max(1, len(original))
        if not 0.7 <= ratio <= 1.4:
            problems.append(f"篇幅异常（润色/原文 = {ratio:.2f}，句级改写应在 0.7~1.4）")
    return problems


def polish_book(ws: Workspace, project_id: str, provider, *,
                window: int = WINDOW_CHAPTERS) -> dict:
    """全书终润主入口：指纹 → 分窗句级润色 → 轻量事实复检 → 产物+报告。"""
    fp = load_fingerprint(ws, project_id) \
        or build_fingerprint(ws, project_id, provider)
    src_files = _chapter_files(ws, project_id, "chapters") \
        or _chapter_files(ws, project_id, "drafts/chapters")
    if not src_files:
        raise ValueError("全书没有任何章节——无物可润")
    # 人物名清单（覆盖率复检用）：bible 人物卡 + worldstate 实然
    names: list[str] = []
    for section in ("characters", "worldstate"):
        p = ws.bible_path(project_id, section)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows = data if isinstance(data, list) else \
                list((data.get("characters") or {}).values())
            for r in rows:
                if isinstance(r, dict) and r.get("name"):
                    names.append(str(r["name"]))
    # 本书实例化工艺规则（机检复检用）——路径走 bible_path 访问器（D-8 棘轮纪律）
    stp = ws.bible_path(project_id, "style")
    craft_rules = []
    if stp.exists():
        try:
            craft_rules = [r for r in (json.loads(stp.read_text(encoding="utf-8"))
                                       .get("craft_rules") or []) if isinstance(r, dict)]
        except (OSError, ValueError):
            craft_rules = []

    keys = sorted(src_files)
    out_dir = ws._abs(f"{project_id}/{POLISHED_DIR}")  # noqa: SLF001
    out_dir.mkdir(parents=True, exist_ok=True)
    prev_tail = ""
    report_rows: list[dict] = []
    n_polished = n_rolled = 0
    for i in range(0, len(keys), window):
        chunk = keys[i:i + window]
        originals = {k: _read(src_files[k]) for k in chunk}
        corpus = "\n\n".join(
            f"=== 第{v}卷第{c}章 ===\n{text}" for (v, c), text in originals.items())
        prompt = (
            "你是全书统稿润色师。对下面各章做**句级风格润色**，全书统一交付质感。\n"
            + _fingerprint_block(fp)
            + (f"\n\n【上一窗润色稿结尾（保持接缝语气一致）】\n{prev_tail}"
               if prev_tail else "")
            + "\n\n【硬边界（越权即废稿）】\n"
              "1. 只改句子层：句式/节奏/去重/用词；**不得**增删情节、改动任何事实、\n"
              "  调整段落顺序、改写对白的语义（对白只按口吻卡调整语气措辞）。\n"
              "2. 每一章单独输出，格式严格为：=== 第X卷第Y章 === 换行后接该章润色全文。\n"
              "3. 章与章之间用一个空行分隔；不得输出任何解释。\n\n【原文】\n" + corpus)
        emit(f"[polish] 润色窗口 {i // window + 1}/{(len(keys) + window - 1) // window}"
             f"：{chunk[0][0]}-{chunk[0][1]} … {chunk[-1][0]}-{chunk[-1][1]}")
        res = provider.complete(LLMRequest(
            messages=[LLMMessage(role="user", content=prompt)],
            temperature=0.3, max_tokens_out=8000, thinking=False))
        body = (res.content or "").strip()
        polished = _split_window_output(body, chunk)
        for k in chunk:
            v, c = k
            original, new = originals[k], polished.get(k, "")
            problems = (["润色稿缺失/过短（保留原文）"]
                        if len(new) < max(300, int(len(original) * 0.6))
                        else _light_fact_check(original, new, craft_rules, names))
            if problems:
                out = original
                n_rolled += 1
            else:
                out = new
                n_polished += 1
                prev_tail = out[-PREV_TAIL_CHARS:]
            ws.write_text(out_dir / f"{v}-{c}.md", out + "\n")
            report_rows.append({"vol": v, "ch": c, "rolled_back": bool(problems),
                                "chars": len(out), "problems": problems[:4]})
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
              "windows": (len(keys) + window - 1) // window,
              "polished": n_polished, "rolled_back": n_rolled,
              "fingerprint": f"bible/style_fingerprint.json（{len(fp.get('voices') or {})} 张口吻卡）",
              "chapters": report_rows}
    ws.write_json(ws._abs(f"{project_id}/{REPORT_REL}"), report)  # noqa: SLF001
    return report


def _split_window_output(body: str, keys: list[tuple[int, int]]) -> dict[tuple[int, int], str]:
    """按 `=== 第X卷第Y章 ===` 分隔符拆窗口输出；缺章/坏格式由调用方回退原文。"""
    out: dict[tuple[int, int], str] = {}
    marker = re.compile(r"===\s*第(\d+)卷第(\d+)章\s*===")
    matches = list(marker.finditer(body))
    for i, m in enumerate(matches):
        key = (int(m.group(1)), int(m.group(2)))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out[key] = body[m.end():end].strip()
    # 模型偶尔丢分隔符但只润一章的窗：整段文本若明显是正文，按唯一键收下
    if not out and len(keys) == 1 and len(body) > 300:
        out[keys[0]] = body
    return out
