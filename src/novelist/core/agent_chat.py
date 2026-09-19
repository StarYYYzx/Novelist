"""常驻对话 Agent（ADR-036 / docs/08 M3ac）：主编剧的交互态。

用户在 `novelist console` 里用自然语对话，主编剧自主调工具（读设定/细纲/记忆、
写草稿）多轮循环后交付。本模块是 console 与 AgentRunner 之间的装配层：

- `SYSTEM_PROMPT`：**静态规则**（角色/边界/工具用法/输出风格）。DeepSeek 前缀缓存
  纪律（价差 50 倍）——system 里**绝不放逐轮变化的内容**；项目状态由首条 user 消息
  （`project_snapshot`）给一次，之后靠工具按需读。
- `ChatAgent`：一个项目一个实例——AgentRunner（chat 模式）+ 全工具 registry +
  三级门禁 + 会话持久化。
- 会话持久化（M3ac-3）：`<proj>/workspace/agent/session.jsonl`（ADR-016 文件即事实源，
  不入 git——工作区数据本来就不入库）。**按项目分文件**，天然隔离，无需段标记；
  重开时回放最近窗口（字符预算截断）。只持久化 user/assistant 文本轮——tool 消息
  脱离配对的 tool_calls 在 OpenAI 协议里非法，且工具探索属当轮临时物。

自主边界（拍板 B）：safe 自主 / sensitive 进审批（console 当场问）/ danger 默认拒。
创作类命令（chapter/build）**不是工具**——agent 只能建议用户执行对应 `/` 命令。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from ..storage.workspace import Workspace
from .agent_runner import AgentLoopError, AgentRunner
from .output import fmt_duration
from .session import Budget, SessionInfo
from .tools import PermissionGate

# ---------------------------------------------------------------- 主编剧 system prompt
# 静态规则层。改它 = 改行为契约：先离线 dump 审计（docs/prompt审计-2026-09-12 的手法），
# 再动；末尾带版本戳便于 dump diff。

SYSTEM_PROMPT = """你是「主编剧」——Novelist 小说项目的常驻协作者（交互态）。用户是一位网文作者，
你们在一个常驻控制台里对话（novelist console）。

# 你的能力
- 用只读工具查阅项目的任何事实：bible 设定（人物/世界观/伏笔/文风）、细纲、已写章节、
  实然状态（worldstate：人物当前境界/位置/伤势）、记忆检索、待裁决冲突、待审模块。
- 写草稿（write_draft 写入 drafts/）与项目内文件（write_file，受控路径需用户当场批准）。
- 回答创作问题、给修改建议、解释系统状态。

# 你的边界（硬约束，不可逾越）
- 正文转正、发布、删除文件**不归你直接做**：这些走流水线闸门（review → promote）。
  用户可以执行 /chapter、/build、/review 等命令；你可以建议，但不能代办。
- 待裁决的结构冲突（两条主线/重名伏笔）由用户用 /conflicts、/resolve 裁决；
  你可以分析利弊、给推荐，但不替用户裁决。
- 不确定的事先查工具再回答，**不要凭印象编造项目事实**；查不到就如实说查不到。
- 修改设定类请求（"把主角境界改成筑基"）： bible 是应然事实源，你只写草稿/给建议；
  正式改动建议用户用 /craft 或 /revise（Forge 审核闸门会保护一致性）。

# 输出风格
- 简洁中文，结论先行；引用你查到的具体文件/字段（如「bible/characters.json 里 char:linyuan
  的 power.level 是练气前期」），让用户可以核验。
- 调了工具就在回答末尾用一行说明读了什么（如「——依据：get_bible(characters)、
  list_chapters」）。不做无谓道歉，不重复用户的话。

# 版本
v1（2026-09-19，M3ac-4）"""

# 会话回放字符预算：超过即从尾部取最近窗口（首条项目快照**每次打开重生成**，不回放）
REPLAY_BUDGET_CHARS = 6000
# 对话态成本硬顶（S-5）：防自主循环烧钱；NOVELIST_AGENT_MAX_COST 覆盖
DEFAULT_MAX_COST = 0.5

SESSION_REL = "workspace/agent/session.jsonl"


# ---------------------------------------------------------------- 项目快照（首条 user 消息）

def project_snapshot(ws: Workspace, project_id: str) -> str:
    """打开项目时的一次性状态快照（进首条 user 消息，不进 system）。

    只放"定位性"信息（是哪本书、写到哪、有什么待办），细节让 agent 用工具按需读。
    """
    import json as _json

    lines = [f"【当前项目】{project_id}"]
    try:
        proj = _json.loads(ws.project_json_path(project_id).read_text(encoding="utf-8"))
        lines.append(f"书名：{proj.get('title') or '（未命名）'}；阶段：{proj.get('pipeline_state') or '—'}")
    except (OSError, ValueError):
        lines.append("（project.json 读取失败）")
    # 已写章节
    try:
        ch_dir = ws._abs(f"{project_id}/chapters")  # noqa: SLF001
        drafts_dir = ws._abs(f"{project_id}/drafts/chapters")  # noqa: SLF001
        n_pub = len(list(ch_dir.glob("*.md"))) if ch_dir.exists() else 0
        n_draft = len(list(drafts_dir.glob("*.md"))) if drafts_dir.exists() else 0
        lines.append(f"章节：已定稿 {n_pub} 章，草稿 {n_draft} 章")
    except OSError:
        pass
    # 待办：冲突 + 审核
    try:
        from ..forge.conflicts import open_conflicts
        from ..forge.review import pending_modules

        n_cf = len(open_conflicts(ws, project_id))
        pend = sorted(pending_modules(ws, project_id))
        if n_cf:
            lines.append(f"待裁决结构冲突：{n_cf} 条（/conflicts 查看）")
        if pend:
            lines.append(f"待审核模块：{'、'.join(pend)}（/fr-review 查看，/fr-approve-all 放行）")
    except Exception:  # noqa: BLE001 - 快照缺待办不阻塞
        pass
    lines.append("以上快照只在对话开始时给一次；之后的状态变化请用工具查最新值。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 会话持久化

def _session_path(ws: Workspace, project_id: str):
    return ws._abs(f"{project_id}/{SESSION_REL}")  # noqa: SLF001


def append_session(ws: Workspace, project_id: str, record: dict) -> None:
    """追加一条会话记录（jsonl）。失败静默——会话日志不是关键事实，绝不打断对话。"""
    record.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()))
    try:
        p = _session_path(ws, project_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_session(ws: Workspace, project_id: str,
                 *, budget_chars: int = REPLAY_BUDGET_CHARS) -> list[dict]:
    """回放会话历史：只取 user/assistant 文本轮，按字符预算从尾部取最近窗口。"""
    p = _session_path(ws, project_id)
    if not p.exists():
        return []
    turns: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "turn" and d.get("role") in ("user", "assistant"):
                content = str(d.get("content") or "")
                if content.strip():
                    turns.append({"role": d["role"], "content": content})
    except OSError:
        return []
    # 尾部窗口（保最新）：累计超预算即截断更早的
    out: list[dict] = []
    total = 0
    for t in reversed(turns):
        n = len(t["content"])
        if total + n > budget_chars and out:
            break
        out.append(t)
        total += n
    out.reverse()
    return out


# ---------------------------------------------------------------- ChatAgent

class ChatAgent:
    """一个项目一个对话 agent 实例（console 持有，/open 切换项目时各建各的）。"""

    def __init__(self, ws: Workspace, project_id: str, provider, *,
                 decision_fn=None, embedding=None,
                 max_cost: float | None = None, max_rounds: int = 12,
                 replay_budget_chars: int = REPLAY_BUDGET_CHARS) -> None:
        from ..tools import build_registry
        from .approval import ApprovalQueue

        self.ws = ws
        self.project_id = project_id
        self.turns = 0
        approvals = ApprovalQueue(persist_dir=str(
            ws._abs(f"{project_id}/logs")))  # noqa: SLF001
        registry = build_registry(ws, gate=PermissionGate(), approvals=approvals,
                                  decision_fn=decision_fn, embedding=embedding)
        if max_cost is None:
            try:
                max_cost = float(os.environ.get("NOVELIST_AGENT_MAX_COST",
                                                str(DEFAULT_MAX_COST)))
            except (TypeError, ValueError):
                max_cost = DEFAULT_MAX_COST
        self.runner = AgentRunner(
            provider,
            SessionInfo(project_id=project_id, agent="orchestrator",
                        permission_profile="supervised"),
            budget=Budget(max_tokens_out=4000, max_rounds=max_rounds,
                          max_cost=max_cost),
            registry=registry,
            enforce_cost=True,  # S-5：对话态成本硬顶（自主多轮 + 真实计费，必须有闸）
        )
        self.runner.system(SYSTEM_PROMPT)
        # 首条 user 消息 = 本次打开时的项目快照（缓存纪律：快照不进 system）
        history = [{"role": "user", "content": project_snapshot(ws, project_id)}]
        history += load_session(ws, project_id, budget_chars=replay_budget_chars)
        self.runner.load_messages(history)

    def ask(self, text: str) -> str:
        """用户一句话 → agent 多轮循环 → 回答文本。持久化双轮 + 用量。"""
        append_session(self.ws, self.project_id,
                       {"type": "turn", "role": "user", "content": text})
        try:
            run = self.runner.run_chat(text)
            answer = run.final
        except AgentLoopError:
            # 轮次/预算耗尽 → 抢救（AG-13 同语义）：要求不带工具直接答
            answer = self.runner.converge("对话抢救：直接回答用户最后的问题，"
                                          "答不了的部分明说。") or "（本轮达到工具调用上限，未能产出回答）"
        self.turns += 1
        append_session(self.ws, self.project_id,
                       {"type": "turn", "role": "assistant", "content": answer})
        usage = self.runner.usage
        append_session(self.ws, self.project_id, {
            "type": "usage", "turn": self.turns,
            "tokens_in": usage.get("tokens_in"), "tokens_out": usage.get("tokens_out"),
            "cost": usage.get("cost")})
        return answer

    def status(self) -> dict[str, Any]:
        """/agent status：本轮会话的轮数与累计用量。"""
        u = self.runner.usage
        return {"project_id": self.project_id, "turns": self.turns,
                "tokens_in": u.get("tokens_in"), "tokens_out": u.get("tokens_out"),
                "cost": u.get("cost"),
                "max_cost": self.runner.budget.max_cost}

    def status_line(self) -> str:
        s = self.status()
        return (f"[agent] 项目 {s['project_id']} · 本轮会话 {s['turns']} 轮 · "
                f"tokens in/out {s['tokens_in']}/{s['tokens_out']} · "
                f"成本 ¥{s['cost']:.4f}（上限 ¥{s['max_cost']}）")


__all__ = ["ChatAgent", "SYSTEM_PROMPT", "project_snapshot", "append_session",
           "load_session", "SESSION_REL", "REPLAY_BUDGET_CHARS", "DEFAULT_MAX_COST",
           "fmt_duration"]
