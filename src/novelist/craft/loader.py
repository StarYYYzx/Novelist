"""工艺卡加载器：列出可用卡、按 id 读取、抽取注入片段。

卡片是带 YAML frontmatter 的 Markdown。为免引入 pyyaml 依赖，frontmatter
用极简手写解析（只支持 `key: value` 与 `key:` + 缩进列表两层的 `key:`/`- item`，
够本目录用；解析失败不致命，退化为无 frontmatter）。

注入片段用 HTML 注释 `<!-- INJECT:BEGIN -->` / `<!-- INJECT:END -->` 包裹，
该段内容原样进 LLM prompt。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

CARDS_DIR = Path(__file__).parent / "cards"

_BEGIN = "<!-- INJECT:BEGIN -->"
_END = "<!-- INJECT:END -->"

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass
class CraftCard:
    id: str
    name: str
    summary: str = ""
    version: int = 1
    scope: list[str] = field(default_factory=list)
    knobs: dict = field(default_factory=dict)
    body: str = ""          # frontmatter 之后的全文（给人看）
    inject: str = ""        # INJECT 段（给 LLM 看）
    path: Path | None = None

    @property
    def title(self) -> str:
        return f"{self.name}（{self.id}）"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    raw, rest = m.group(1), text[m.end():]
    data: dict = {}
    cur_key: str | None = None          # 正在收集子项的父键
    cur_container: dict | list | None = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            item = line.strip()
            if cur_key is None or cur_container is None:
                continue
            # 缩进项：'- x' 视为列表元素，'k: v' 视为字典条目
            if item.startswith("- "):
                if not isinstance(cur_container, list):
                    cur_container = []
                    data[cur_key] = cur_container
                cur_container.append(_scalar(item[2:].strip()))
            elif ":" in item:
                if not isinstance(cur_container, dict):
                    cur_container = {}
                    data[cur_key] = cur_container
                k, _, v = item.partition(":")
                cur_container[k.strip()] = _scalar(v.strip())
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if v == "":
            # 父键：子项类型由首个缩进项决定，先占位 dict
            data[k] = {}
            cur_key, cur_container = k, data[k]
        else:
            data[k] = _scalar(v)
            cur_key, cur_container = None, None
    # 空容器字段退化为默认值（避免 schema/展示噪声）
    return {k: v for k, v in data.items() if v != {} or k == "knobs"}, rest


def _scalar(v: str):
    v = v.strip().strip('"').strip("'")
    if "#" in v:
        # 剥掉行尾注释（只在 # 前有空白时视为注释，避免误伤含 # 的内容）
        v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()] or []
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _split_inject(body: str) -> tuple[str, str]:
    """返回 (去掉 INJECT 段后的正文, 注入段)。"""
    if _BEGIN in body and _END in body:
        pre, _, tail = body.partition(_BEGIN)
        inj, _, post = tail.partition(_END)
        return (pre + post).strip(), inj.strip()
    return body.strip(), ""


def load_card(card_id: str) -> CraftCard | None:
    p = CARDS_DIR / f"{card_id}.md"
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    fm, rest = _parse_frontmatter(text)
    body, inject = _split_inject(rest)
    return CraftCard(
        id=str(fm.get("id") or card_id),
        name=str(fm.get("name") or card_id),
        summary=str(fm.get("summary") or ""),
        version=int(fm.get("version") or 1) if str(fm.get("version") or "1").isdigit() else 1,
        scope=list(fm.get("scope") or []),
        knobs=fm.get("knobs") if isinstance(fm.get("knobs"), dict) else {},
        body=body,
        inject=inject,
        path=p,
    )


def list_cards() -> list[CraftCard]:
    out: list[CraftCard] = []
    if not CARDS_DIR.is_dir():
        return out
    for p in sorted(CARDS_DIR.glob("*.md")):
        c = load_card(p.stem)
        if c:
            out.append(c)
    return out


def valid_ids() -> list[str]:
    return [c.id for c in list_cards()]


def inject_block(ids: list[str] | tuple[str, ...] | None) -> str:
    """把若干卡的注入段拼成一块可直接塞进 prompt 的文本。未知 id 静默跳过。"""
    if not ids:
        return ""
    blocks: list[str] = []
    for cid in ids:
        c = load_card(str(cid))
        if c and c.inject:
            blocks.append(c.inject)
    return "\n\n".join(blocks)


def brief_lines() -> list[str]:
    """供设定阶段展示用的一行摘要列表。"""
    return [f"- {c.id}: {c.name} — {c.summary}" for c in list_cards()]
