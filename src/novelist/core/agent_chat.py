"""常驻对话 Agent（ADR-036 / docs/08 M3ac）：主编剧的交互态。

用户在 `novelist console` 里用自然语对话，主编剧自主调工具（读设定/细纲/记忆、
写草稿）多轮循环后交付。本模块是 console 与 AgentRunner 之间的装配层：

- `SYSTEM_PROMPT`：**静态规则**（角色/边界/工具用法/输出风格）。DeepSeek 前缀缓存
  纪律（价差 50 倍）——system 里**绝不放逐轮变化的内容**；项目状态由首条 user 消息
  （`project_snapshot`）给一次，之后靠工具按需读。
- `ChatAgent`：一个项目一个实例——AgentRunner（chat 模式）+ 全工具 registry +
  三级门禁 + 会话持久化。`hub=True` 为枢纽态（G3）：未打开项目时的自然语对话，
  配合 pipeline 工具的显式 project_id 参数完成「对话开书」。
- 会话持久化（M3ac-3）：`<proj>/workspace/agent/session.jsonl`（ADR-016 文件即事实源，
  不入 git——工作区数据本来就不入库）。**按项目分文件**，天然隔离，无需段标记；
  重开时回放最近窗口（字符预算截断）。只持久化 user/assistant 文本轮——tool 消息
  脱离配对的 tool_calls 在 OpenAI 协议里非法，且工具探索属当轮临时物。

自主边界（拍板 B，2026-10-24 DL-014 修订）：safe 自主 / sensitive 首次问（会话内
可授权记住，G4）/ danger 默认拒且**永不入会话授权**。开书/构建/写章/修订已工具化
（`tools/pipeline.py`，M3ac 拍板 C 反转）；正文转正（publish）与删除仍非 agent 职责。
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
- 用只读工具查阅项目的任何事实：bible 设定（人物/世界观/伏笔/文风）、蓝图
  （get_bible 的 blueprint / blueprint:<子段>）、细纲、已写章节、实然状态
  （worldstate：人物当前境界/位置/伤势）、记忆检索、待裁决冲突、待审模块。
- **驱动流水线**（DL-014，2026-10-24 拍板）：build_blueprint 构建/续跑蓝图、
  approve_review 放行待审模块、write_chapter 写正文、revise_module 按意见重生成
  模块。这些是敏感操作——首次调用会请求用户批准，批准过本会话不再重复问。
- **改设定用 `update_blueprint`**（dict 段按键合并、支持点路径如 power_system.levels；
  list 段按 id 合并条目字段；保存后自动同步 bible）。**不要**用 write_file 整份
  重写 JSON（又长又容易写崩）。
- 写草稿（write_draft 写入 drafts/）与项目内文件（write_file，受控路径当场审批）。

# 工作流（对话式，不是表单）
- **补设定/开书**：get_gaps 查缺口 → 只把**真正需要用户拍板的**问题拿出来问
  （一次不超过 3 个，自然语言，禁止 Q1/Q2 编号格式）→ 用 update_blueprint 写入 →
  有合理默认的推荐项，用户不反对就取默认并在回答里报备一声。
- **构建循环**：build_blueprint → 若停在审核闸门，把蓝图关键内容念给用户听
  （书名/主角/金手指/世界观要点/卷纲，用 get_bible blueprint:<段> 取）→
  用户认可后 approve_review → build_blueprint(resume=true) 续跑，直到完成。
- **写正文**：用户点头后 write_chapter（不给 vol/ch 自动写下一章）。
  动手前用一句话报备规模（「构建全书约 X 次模型调用」），让用户有成本预期。
- 长任务期间系统会打印进度心跳，你不要在等待期间重复解释。

# 你的边界（硬约束，不可逾越）
- **正文转正（publish）、删除文件、checkpoint 不归你做**：danger 级，须用户显式执行。
- 待裁决的结构冲突（两条主线/重名伏笔）由用户裁决；你可以分析利弊、给推荐，
  但不替用户裁决。
- 不确定的事先查工具再回答，**不要凭印象编造项目事实**；查不到就如实说查不到。
- 用户表述含糊时先问清再动手（写错重来的代价比多问一句高）；但能用工具查到的
  事实不要拿去烦用户。

# 输出风格
- 简洁中文，结论先行；引用你查到的具体文件/字段（如「bible/characters.json 里
  char:linyuan 的 power.level 是练气前期」），让用户可以核验。
- 调了工具就在回答末尾用一行说明做了什么（如「——依据：get_bible(characters)；
  已执行：write_chapter(1,1)」）。不做无谓道歉，不重复用户的话。

# 工具使用纪律（真机教训）
- **先想清楚要读什么再动手**：优先用结构化查询工具（get_bible/get_outline/
  list_chapters/get_worldstate/get_gaps），不要拿 grep_text/read_file 在大文件里盲找。
- 修改请求**一次说清多处改动就合并成尽量少的 update_blueprint 调用**（set 里可放
  多个字段），别一个字段一次调用——轮次有限，读两次就该动笔。
- 读是为了写：确认现状后立刻写，不要反复重读同一文件。

# 版本
v2（2026-10-24，DL-014：流水线工具化 + 对话式填设定）"""

# 枢纽态（未打开项目时的自然语对话，G3）：工作台层面，只管开新书/选书。
HUB_SYSTEM_PROMPT = """你是「Novelist 工作台助手」——用户还没有打开任何小说项目，
你们在 novelist console 里对话。

# 你的职责
- 用户想开新书：create_project(书名) → start_book(project_id, 一句话创意)。
  创意里信息不足时，先问 1-2 个最关键的（类型/主角/卖点），不要连环追问；
  其余交给构建引擎的默认值，事后可用 get_gaps 补。
- 想先看立项要素再决定：start_book(smoke=true) 只提炼不构建（便宜）。
- 想继续写已有的书：列出项目（用 create_project 返回的信息或让用户 /open），
  打开后由主编剧接管。
- start_book 会触发真实构建（多次模型调用）——动手前一句话报备规模。

# 边界
- 你不直接写正文、不改设定（那是打开项目后主编剧的职责）。
- 不确定就问，不编造项目状态。

# 输出风格
简洁中文，结论先行。开书成功后明确告诉用户：已切换到新书，接下来的对话直接
针对这本书（设定/构建/写章都可以继续说）。

# 版本
v1（2026-10-24，DL-014 G3 枢纽层）"""

# 会话回放字符预算：超过即从尾部取最近窗口（首条项目快照**每次打开重生成**，不回放）
REPLAY_BUDGET_CHARS = 6000
# 对话态成本硬顶（S-5）：防自主循环烧钱；NOVELIST_AGENT_MAX_COST 覆盖
DEFAULT_MAX_COST = 0.5
# 长对话压缩阈值（D-13）：消息史超此字符数即把中段折成摘要
DEFAULT_COMPACT_CHARS = 24_000

SESSION_REL = "workspace/agent/session.jsonl"
# 枢纽态（G3，DL-014）：无项目时的会话落工作区根（不入任何项目目录）
HUB_SESSION_REL = "agent_hub_session.jsonl"


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


def hub_snapshot(ws: Workspace) -> str:
    """枢纽态首条 user 消息：工作台概览（有哪些书、开新书的路怎么走）。"""
    lines = ["【工作台】当前未打开任何项目。"]
    try:
        ids = ws.list_projects()
    except Exception:  # noqa: BLE001 - 快照失败不阻塞对话
        ids = []
    if ids:
        shown = ids[-10:]
        lines.append(f"已有 {len(ids)} 个项目（最近 {len(shown)} 个）：{'、'.join(shown)}")
        lines.append("用户想继续写其中一本：提示其执行 /open <id> 切入后由主编剧接管。")
    else:
        lines.append("工作台还没有项目。")
    lines.append("用户想开新书：create_project(书名) → start_book(project_id, 一句话创意)。")
    lines.append("以上快照只在对话开始时给一次。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 会话持久化

def _session_path(ws: Workspace, project_id: str | None):
    """项目态 → `<proj>/workspace/agent/session.jsonl`；枢纽态（None）→ 工作区根。"""
    rel = f"{project_id}/{SESSION_REL}" if project_id else HUB_SESSION_REL
    return ws._abs(rel)  # noqa: SLF001


def append_session(ws: Workspace, project_id: str | None, record: dict) -> None:
    """追加一条会话记录（jsonl；project_id=None 为枢纽态）。失败静默——会话日志
    不是关键事实，绝不打断对话。"""
    record.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()))
    try:
        p = _session_path(ws, project_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_session(ws: Workspace, project_id: str | None,
                 *, budget_chars: int = REPLAY_BUDGET_CHARS) -> list[dict]:
    """回放会话历史（project_id=None 为枢纽态）：只取 user/assistant 文本轮，
    按字符预算从尾部取最近窗口。"""
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
        # 2026-09-19 审计修复：单轮超预算时截断该轮内容而非整条照收
        # （此前 converge 吐出的整章会撑爆回放窗口，REPLAY_BUDGET 形同虚设）
        if n > budget_chars:
            t = {**t, "content": t["content"][-budget_chars:]
                 + "\n…[回放截断：该轮原文超长，仅保留尾部]"}
            n = budget_chars
        if total + n > budget_chars and out:
            break
        out.append(t)
        total += n
    out.reverse()
    # 2026-09-19 审计修复：窗口首条必须对齐到 user——从 assistant 开场
    # （配对的 user 被预算切掉）会让模型看到无主语的回答，部分后端直接拒收
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


# ---------------------------------------------------------------- ChatAgent

class ChatAgent:
    """一个项目一个对话 agent 实例（console 持有，/open 切换项目时各建各的）。

    `hub=True`（G3，DL-014）：枢纽态——未打开项目时的自然语对话，session.project_id
    为 None，流水线工具须经显式 project_id 参数定位目标；会话落工作区根。
    `pipeline_ctx`：流水线工具共享上下文（console 据此做开书后的项目交接，
    并把审批回调透传给章节生成内部）。
    """

    def __init__(self, ws: Workspace, project_id: str | None, provider, *,
                 decision_fn=None, embedding=None,
                 max_cost: float | None = None, max_rounds: int = 12,
                 replay_budget_chars: int = REPLAY_BUDGET_CHARS,
                 hub: bool = False,
                 pipeline_ctx=None) -> None:
        from ..tools import build_registry
        from ..tools.pipeline import PipelineContext
        from .approval import ApprovalQueue

        self.ws = ws
        self.project_id = project_id
        self.hub = hub
        self.turns = 0
        if hub and project_id:
            raise ValueError("hub 态不能绑定 project_id")
        approvals = ApprovalQueue(persist_dir=str(
            ws._abs(f"{project_id}/logs")) if project_id else None)
        pctx: PipelineContext = pipeline_ctx or PipelineContext()
        registry = build_registry(ws, gate=PermissionGate(), approvals=approvals,
                                  decision_fn=decision_fn, embedding=embedding,
                                  provider=provider, pipeline_ctx=pctx)
        self.pipeline_ctx = pctx
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
            budget=Budget(max_tokens_out=8000, max_rounds=max_rounds,
                          max_cost=max_cost),
            registry=registry,
            enforce_cost=True,  # S-5：对话态成本硬顶（自主多轮 + 真实计费，必须有闸）
            obs_total_budget_chars=48_000,  # 对话态放宽整轮观测预算（读设定是常态）
        )
        self.runner.trace_tools = True  # 对话态可见性：每次工具调用打一行
        # D-13：长对话压缩（默认 24000 字符；NOVELIST_CHAT_COMPACT_CHARS 覆盖，0 = 关闭）
        try:
            self.runner.compact_chars = int(
                os.environ.get("NOVELIST_CHAT_COMPACT_CHARS") or DEFAULT_COMPACT_CHARS)
        except (TypeError, ValueError):
            self.runner.compact_chars = DEFAULT_COMPACT_CHARS
        self.runner.system(HUB_SYSTEM_PROMPT if hub else SYSTEM_PROMPT)
        # 首条 user 消息 = 本次打开时的快照（缓存纪律：快照不进 system）
        snap = hub_snapshot(ws) if hub else project_snapshot(ws, project_id)
        history = [{"role": "user", "content": snap}]
        history += load_session(ws, project_id, budget_chars=replay_budget_chars)
        self.runner.load_messages(history)

    def ask(self, text: str) -> str:
        """用户一句话 → agent 多轮循环 → 回答文本。持久化双轮 + 用量。

        可见性（2026-09-19 真机 UX）：轮开始即报「思考中」+ 超阈值心跳
        （此前整轮 44s 零输出）；calllog 挂 `chat:<pid>` 锚点（溯源归属）。
        """
        from .calllog import call_context
        from .output import emit, heartbeat

        append_session(self.ws, self.project_id,
                       {"type": "turn", "role": "user", "content": text})
        _think = "工作台助手思考中" if self.hub else "主编剧思考中（可调工具查证）"
        emit(f"… {_think}")
        try:
            with call_context(f"chat:{self.project_id or 'hub'}"), heartbeat(_think):
                run = self.runner.run_chat(text)
            answer = run.final
        except KeyboardInterrupt:
            # 审批/生成中 Ctrl+C：补记中断标记，保持 session 双轮配对，再上抛
            append_session(self.ws, self.project_id,
                           {"type": "turn", "role": "assistant",
                            "content": "（本轮被用户中断）"})
            raise
        except AgentLoopError:
            # 轮次/预算耗尽 → 抢救（AG-13 同语义）：要求不带工具直接答
            with call_context(f"chat:{self.project_id}"):
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
