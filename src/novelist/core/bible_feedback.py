"""设定集人工反馈通道（docs/07 §X，ADR-029 · 批次 A / M3z）。

创作方向是"系统写初稿、人工改正文"。设定圣经（bible）迄今只允许受控补喂
（character_enrich），人工想按自己口径修正设定时没有"把自由语意见精确落到对应
JSON 字段"的一等通道；直接手改 JSON 会绕过 schema 校验、provenance 与 covenant
门禁。本模块提供：

    人工自由语意见 → LLM（判断任务，开 thinking）拆成 op 集
    → 确定性校验（文件白名单 + 路径可改性 + 值类型）
    → 提交 ApprovalQueue（唯一人工确认队列）
    → feedback --apply 原子写回（临时文件+rename）+ 审计留痕

设计要点（ADR-029）：
- 一条修改请求 = `EditOp`。文件白名单来自 `bible_editable`（每类文件允许改的字段 +
  只读保护区）。
- 列表文件（characters/plot_threads/...）按 **id 或 name** 定位条目（解析用论断式
  选择器，LLM 不必背 id）；对象文件（worldview/style/worldstate）按点路径定位字段。
- 命中 covenant 承诺账本（世界铁律 / 已提交情节线 / timetable）的 op 标 `sensitive`，
  人工确认时高亮——落实"触碰 covenant 必须人工 approval"的硬约束（衔接 ADR-026）。
- 单测绝不真调 LLM（docs/09 §2.1，用 ScriptedProvider）；本模块**不引 providers 具体类**，
  只持 `LLMProvider` 协议实例，由 CLI 经 `providers.create()` 注入。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field as dfield
from typing import Any, Protocol

from .errors import NovelistError


class FeedbackError(NovelistError):
    """设定集人工反馈错误（docs/07 §9 映射）。"""


# ---------------------------------------------------------------------------
# 1. bible 可改性字段清单（单一事实源，供解析后确定性校验）
# ---------------------------------------------------------------------------
# 列表文件：container -> 可改字段 / 只读字段 / 解析可能用到的 id 字段
# 对象文件：可直接改的顶层点路径（world rules / protagonist 约束默认只读）
BIBLE_EDITABLE: dict[str, dict[str, Any]] = {
    # 2026-09-19（决策 D-2）：逐字段对齐 schemas/bible/*.schema.json。
    # 此前声明的 appearance/personality/motivation/goals/tags/notes/relationship_hooks
    # （characters）、name/description/payoff_vol/payoff_ch/tags（plot_threads）、
    # key/value/description（settings）、kind/description/owner/powers（items）等
    # **在 schema 里都不存在**，写回即违规或写进无人读的野字段。
    # 机械护栏见 scripts/check.py `_gate_feedback_editable()`（edit ⊆ schema properties）。
    "characters.json": {
        "kind": "list",
        "container": "characters",
        "edit": [
            "name", "aliases", "age", "gender", "core_traits", "background", "arc",
            "relationships", "power.level", "power.hidden_level", "power.faction",
        ],
        "readonly": ["id", "status", "first_appear", "is_protagonist", "revision"],
    },
    "plot_threads.json": {
        "kind": "list",
        "container": "plot_threads",
        "edit": ["desc", "plant_desc", "payoff_desc", "scope", "target_vol", "status",
                 "planted", "returned", "carrier"],
        "readonly": ["id", "revision", "due"],
    },
    "locations.json": {
        "kind": "list",
        "container": "locations",
        "edit": ["name", "parent", "desc", "status", "aliases"],
        "readonly": ["id", "revision"],
    },
    "items.json": {
        "kind": "list",
        "container": "items",
        "edit": ["name", "type", "aliases", "state", "desc", "note"],
        "readonly": ["id"],
    },
    "skills.json": {
        "kind": "list",
        "container": "skills",
        "edit": ["name", "type", "aliases", "state", "note"],
        "readonly": ["id"],
    },
    "settings.json": {
        "kind": "list",
        "container": "settings",
        "edit": ["keywords", "text", "revealed"],
        "readonly": ["id", "first_ch"],
    },
    "worldview.json": {
        "kind": "object",
        # world rules / power_system 允许改但命中 covenant → 标 sensitive 强制人工
        "edit": ["name", "summary", "rules", "power_system", "civilizations",
                 "systems", "factions", "phase_policy", "unavailable_states",
                 "realm_fluctuates"],
        "readonly": ["id", "revision"],
    },
    "style.json": {
        "kind": "object",
        "edit": [
            "tone", "glossary", "forbidden_words", "pov", "tense", "narration",
            "craft_cards", "target_words_per_chapter",
        ],
        "readonly": ["protagonist", "revision"],
    },
    "worldstate.json": {
        "kind": "object",
        # characters 硬状态可改；time/pending 是运行时实然，只读
        # （2026-09-19 修正：此前 readonly 写 "timeline"，该顶层键不存在，实为 "time"）
        "edit": ["characters"],
        "readonly": ["time", "pending"],
    },
}

# add 时给新条目铸造 id 用的前缀（必须匹配各 schema 的 id pattern，2026-09-19 D-2）
# 此前用 `char_pf_ab12cd34` 形态，**全部不匹配** `^char:` / `^set:` 等 pattern。
_ID_PREFIX = {"characters.json": "char:", "plot_threads.json": "pt:",
              "locations.json": "loc:", "items.json": "item:",
              "skills.json": "skill:", "settings.json": "set:"}


def _slug(name: Any) -> str:
    """名字 → id 片段：ASCII 名归一；中文名走稳定 hash（pattern 只允许 [A-Za-z0-9_-]）。"""
    import hashlib
    import re as _re

    s = _re.sub(r"[^A-Za-z0-9_-]", "", str(name or "").lower())[:24]
    if s:
        return s
    return "n" + hashlib.sha1(str(name or "").encode("utf-8")).hexdigest()[:10]


def mint_id(file: str, item: dict, existing: list[dict] | None = None) -> str:
    """按分区前缀铸 id（`char:` / `pt:` / `loc:` / `item:` / `skill:` / `set:`），并去重。"""
    prefix = _ID_PREFIX.get(file, "fb:")
    base = f"{prefix}{_slug(item.get('name') or item.get('desc') or 'item')}"
    taken = {str(x.get("id")) for x in (existing or []) if isinstance(x, dict)}
    cand, n = base, 2
    while cand in taken:
        cand, n = f"{base}-{n}", n + 1
    return cand



def _bible_base(file: str) -> str:
    """bible_path 会追加 .json；op.file 带扩展名，先剥离基名。"""
    return file[:-5] if file.endswith(".json") else file


class _LLM(Protocol):
    def complete(self, req: Any) -> Any:  # LLMResponse
        ...


# ---------------------------------------------------------------------------
# 2. 编辑操作模型
# ---------------------------------------------------------------------------
@dataclass
class EditOp:
    """一条人工修改请求。

    - 列表文件：`target` 为条目 id **或 name**（解析后由有效定位器兜底），
      `field` 为要改的字段（可带点路径如 `power.level`）；add 时 `value` 为整条
      对象，edit 时为该字段新值；delete 时 `value` 忽略。
    - 对象文件：`target` 为点路径（如 `style.tone`），`op` 仅支持 `edit`。
    """

    file: str
    op: str  # add | edit | delete
    target: str
    field: str | None = None
    value: Any = None
    reason: str = ""
    id: str = dfield(default_factory=lambda: f"fb:{uuid.uuid4().hex[:10]}")
    sensitive: bool = False
    status: str = dfield(default="draft")  # draft | pending | applied | rejected | rejected=deny
    created_at: float = dfield(default_factory=time.time)
    applied_at: float | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "file": self.file, "op": self.op, "target": self.target,
            "field": self.field, "value": self.value, "reason": self.reason,
            "sensitive": self.sensitive, "status": self.status,
            "created_at": self.created_at, "applied_at": self.applied_at, "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EditOp":
        return cls(
            id=d.get("id") or f"fb:{uuid.uuid4().hex[:10]}",
            file=d["file"], op=d["op"], target=d["target"], field=d.get("field"),
            value=d.get("value"), reason=d.get("reason", ""),
            sensitive=bool(d.get("sensitive")), status=d.get("status", "draft"),
            created_at=d.get("created_at") or time.time(),
            applied_at=d.get("applied_at"), error=d.get("error", ""),
        )


# ---------------------------------------------------------------------------
# 3. 确定性校验与定位（不依赖 LLM，可单测）
# ---------------------------------------------------------------------------
def _set_nested(obj: dict, dotted: str, value: Any) -> bool:
    parts = dotted.split(".")
    node = obj
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            node[p] = {}
            nxt = node[p]
        node = nxt
    node[parts[-1]] = value
    return True


def _find_index(items: list[dict], target: str) -> int:
    """按 id 或 name 定位列表条目下标；找不到抛 FeedbackError。"""
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        if it.get("id") == target or it.get("name") == target:
            return i
    raise FeedbackError(f"在列表里找不到条目: {target}")


def validate_op(op: EditOp, *, existing: list[dict] | None = None) -> None:
    """确定性校验：文件白名单 + 路径可改性 + 值类型。失败抛 FeedbackError。"""
    spec = BIBLE_EDITABLE.get(op.file)
    if not spec:
        raise FeedbackError(f"目标文件不在可改性白名单: {op.file}")
    if op.op not in ("add", "edit", "delete"):
        raise FeedbackError(f"不支持的修改类型: {op.op}")

    if spec["kind"] == "list":
        fields = set(spec["edit"])
        if op.op in ("edit", "delete"):
            if not op.target:
                raise FeedbackError("edit/delete 必须指定 target(id 或 name)")
            # 存在性检查（用调用方给出的现有条目，避免重复读盘）
            if existing is not None:
                try:
                    _find_index(existing, op.target)
                except FeedbackError:
                    raise
        if op.op == "edit":
            if not op.field:
                raise FeedbackError("edit 必须指定要修改的字段")
            if op.field not in fields:
                raise FeedbackError(f"字段不可人工修改: {op.field}（只读或白名单外）")
            _check_value_type(op.field, op.value)
        if op.op == "add":
            if not isinstance(op.value, dict):
                raise FeedbackError("add 的 value 必须是对象")
    else:
        # 对象文件：只支持 edit；target 点路径首段必须在可改白名单内
        if op.op != "edit":
            raise FeedbackError(f"对象文件 {op.file} 只支持 edit")
        first = op.target.split(".")[0]
        edit_set = set(spec["edit"])
        read_set = set(spec["readonly"])
        if first not in edit_set or first in read_set:
            raise FeedbackError(f"字段不可人工修改: {op.target}")


def _check_value_type(field: str, value: Any) -> None:
    # 只允许标量/字符串/列表/布尔/简单数字，禁止嵌套脏对象（防 schema 破坏）
    if isinstance(value, (str, int, float, bool)) or value is None:
        return
    if isinstance(value, list) and all(isinstance(x, (str, int, float, bool)) for x in value):
        return
    raise FeedbackError(f"字段 [{field}] 的值类型不符合要求")


def flag_sensitive(op: EditOp) -> bool:
    """判断 op 是否触碰 covenant 承诺账本（世界铁律/已提交线索/time 轴）。"""
    if op.op == "delete":
        return True
    spec = BIBLE_EDITABLE.get(op.file) or {}
    if op.file == "worldview.json" and op.target.split(".")[0] == "rules":
        return True
    # 2026-09-19：键名对齐实然（顶层键是 time，此前写 timeline——该键不存在）
    if op.file == "worldstate.json" and op.target.split(".")[0] == "time":
        return True
    if op.file == "plot_threads.json" and (op.field in ("status", "committed") or op.field == "arc"):
        return True
    _ = spec
    return False


# ---------------------------------------------------------------------------
# 4. 意见解析器（LLM 判断任务，开 thinking）
# ---------------------------------------------------------------------------
_PARSE_SYSTEM = (
    "你是小说设定集维护助手。用户会用自由语给你一条关于设定的修改意见。"
    "你的任务：把它拆成一到多条**结构化的字段级修改指令**，供系统精确落到设定 JSON。"
    "只理解改设定的诉求；写作/排版类意见直接忽略（返回空 ops）。"
)

_PARSE_PROMPT = """针对以下人工意见，输出修改指令。

规则：
- 对象文件是「编辑字段」，路径如 `style.tone`；列表文件用「编辑条目字段」：target 填该角色/条目
  的名字（系统会按 name 或 id 定位），field 填要改的字段名（如 `core_traits`、`power.level`）。
- op 取值：`edit`（改已有字段/条目）、`add`（新增条目，value 为整条对象）、`delete`（删条目）。
- 命中下列情况标 sensitive=true：改世界铁律(rules)、删条目、改已提交剧情的线索状态、改时间轴。
- 拿不准「改哪个文件/字段」就跳过该条，不要瞎猜；改不了的诉求不要硬拆。

输出 JSON：{"ops": [{"file": "characters.json", "op": "edit", "target": "苏晚",
  "field": "core_traits", "value": ["温润"], "reason": "一句话理由", "sensitive": false}]}

意见：
{opinion}
"""


def _extract_json(raw: str) -> dict:
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("未能在 LLM 输出中定位 JSON")  # 解析抖动：重试
    return json.loads(text[start:end + 1])


class FeedbackParser:
    """把人工自由语拆成 EditOp 集；确定性校验；重试 + 降级。"""

    def __init__(self, provider: _LLM, *, max_retries: int = 3,
                 max_tokens_out: int = 16000) -> None:
        self._provider = provider
        self._max_retries = max_retries
        self._max_tokens_out = max_tokens_out

    def parse(self, opinion: str, *, existing_by_file: dict[str, list[dict]] | None = None,
              max_ops: int = 20) -> list[EditOp]:
        from .llm import LLMMessage, LLMRequest

        if not opinion or not opinion.strip():
            raise FeedbackError("意见为空")
        last: Exception | None = None
        for _ in range(self._max_retries):
            try:
                res = self._provider.complete(LLMRequest(
                    messages=[LLMMessage(role="system", content=_PARSE_SYSTEM),
                              LLMMessage(role="user",
                                         content=_PARSE_PROMPT.replace("{opinion}", opinion))],
                    temperature=0.3, max_tokens_out=self._max_tokens_out,
                    response_format="json_object", thinking=True,  # 判断类：意见拆条，开思考
                ))
                if getattr(res, "blocked", False):
                    raise FeedbackError(f"意见解析被审核拦截: {res.block_reason or 'unknown'}")
                content = getattr(res, "content", "") or ""
                if not content.strip():
                    raise FeedbackError("意见解析返回空内容")
                data = _extract_json(content)
                raw_ops = data.get("ops") or []
                return self._finalize(raw_ops, existing_by_file=existing_by_file, max_ops=max_ops)
            except FeedbackError:
                raise  # 结构性/校验类错误，属可向用户说明的确定性问题
            except Exception as e:  # noqa: BLE001 —— LLM 传输/解析抖动，重试
                last = e
                continue
        raise FeedbackError(f"意见解析连续失败（{self._max_retries} 次）: {last}")

    def _finalize(self, raw_ops: list[dict], *, existing_by_file: dict[str, list[dict]] | None,
                  max_ops: int) -> list[EditOp]:
        ops: list[EditOp] = []
        for ro in raw_ops[:max_ops]:
            if not isinstance(ro, dict):
                continue
            file = ro.get("file")
            if file not in BIBLE_EDITABLE:
                continue  # 白名单外 → 丢弃（解析不做瞎猜）
            op = EditOp(
                file=file, op=ro.get("op", "edit"),
                target=str(ro.get("target", "") or "").strip(),
                field=ro.get("field") or None,
                value=ro.get("value"),
                reason=str(ro.get("reason", "") or "").strip(),
            )
            spec = BIBLE_EDITABLE[file]
            existing = (existing_by_file or {}).get(file)
            try:
                validate_op(op, existing=existing if spec["kind"] == "list" else None)
            except FeedbackError:
                continue  # 单条不值得整批失败，静默跳过并交给审批清单展示结果
            op.sensitive = flag_sensitive(op)
            ops.append(op)
        return ops


# ---------------------------------------------------------------------------
# 5. 审批队列接入 + 持久化
# ---------------------------------------------------------------------------
def feedback_persist_dir(ws, project_id: str) -> str:
    return str(ws.workspace_sub(project_id, "feedback"))  # noqa: SLF001


def load_store(ws, project_id: str) -> "FeedbackStore":
    return FeedbackStore(ws, project_id)


@dataclass
class FeedbackStore:
    """ops.json 持久化：所有 EditOp 及其状态 + 写入审计（原件写回前保留全量记录）。"""

    ws: Any
    project_id: str

    def _ops_path(self):
        return self.ws._abs(f"{self.project_id}/workspace/feedback/ops.json")  # noqa: SLF001

    def _load_all(self) -> list[dict]:
        p = self._ops_path()
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []
        items = data if isinstance(data, list) else data.get("ops", [])
        return items if isinstance(items, list) else []

    def _save(self, items: list[dict]) -> None:
        self._ops_path().parent.mkdir(parents=True, exist_ok=True)
        self.ws.write_json(self._ops_path(), {"ops": items,
                                              "audit": self._audit()})

    def _audit(self) -> list[dict]:
        return [o for o in self._load_all() if o.get("status") == "applied"]

    def add(self, ops: list[EditOp]) -> None:
        items = self._load_all()
        for op in ops:
            items.append(op.to_dict())
        self._save(items)

    def get(self, op_id: str) -> EditOp | None:
        for item in self._load_all():
            if item.get("id") == op_id:
                return EditOp.from_dict(item)
        return None

    def mark(self, op_id: str, status: str, *, applied_at: float | None = None,
             error: str = "") -> EditOp | None:
        items = self._load_all()
        for item in items:
            if item.get("id") == op_id:
                item["status"] = status
                item["error"] = error
                if applied_at is not None:
                    item["applied_at"] = applied_at
                self._save(items)
                return EditOp.from_dict(item)
        return None

    def list_ops(self) -> list[EditOp]:
        return [EditOp.from_dict(i) for i in self._load_all()]


# ---------------------------------------------------------------------------
# 6. 原子写回（apply）
# ---------------------------------------------------------------------------
def apply_op(ws, project_id: str, op: EditOp) -> None:
    """把已批 EditOp 落到 bible JSON（原子写）。

    校验分两段：写入前 validate_op 兜底（防绕过批准直接改），写入后做结构自洽检查
    （不破坏关键字段），保留 provenance 在 ops.json 审计里。
    """
    spec = BIBLE_EDITABLE.get(op.file)
    if not spec:
        raise FeedbackError(f"目标文件不在可改性白名单: {op.file}")
    path = ws.bible_path(project_id, _bible_base(op.file))
    data = ws.read_json(project_id, path, required=True)

    if spec["kind"] == "list":
        container = spec["container"]
        items = data[container] if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []
        if op.op == "add":
            new_item = dict(op.value) if isinstance(op.value, dict) else {}
            if not new_item.get("id"):
                new_item["id"] = mint_id(op.file, new_item, items)
            new_item["provenance"] = "feedback"  # 软 tracking，不侵入关键字段
            items.append(new_item)
        elif op.op in ("edit", "delete"):
            idx = _find_index(items, op.target)
            if op.op == "edit":
                _set_nested(items[idx], op.field or "", op.value)
            else:
                items.pop(idx)
        else:
            raise FeedbackError(f"不支持的修改类型: {op.op}")
    else:
        # 对象文件 edit 点路径
        if not op.target:
            raise FeedbackError("对象文件 edit 必须指定点路径")
        validate_op(op)  # 对象文件在 apply 时校验可改性
        _set_nested(data, op.target, op.value)

    ws.write_json(path, data)  # 原子写（临时文件 + rename，docs/06 §7）


def apply_feedback(ws, project_id: str, op: EditOp) -> EditOp:
    """审批通过后的写回入口：原子写 + 标记 applied + 审计留痕。"""
    if op.status == "applied":
        return op  # 幂等：已应用不再重复写
    apply_op(ws, project_id, op)
    op.status = "applied"
    op.applied_at = time.time()
    return op