"""任务板 TaskStore（ADR-028 · CC s12 借鉴与适配 · 2026-09-06）。

Novelist 是**中央编排 + 线性时序**，因此**不平/封** CC 的"多 Agent 自看板认领"——
而只借鉴其最有价值的一点：**细粒度任务持久化**，让崩溃/中断后能做单任务恢复，而非整卷回退。

有别于 `pipeline_state`（阶段级**单个字符串**），本模块把卷→章→事件落成独立任务文件，
每个任务记录：状态 / owner（编排器指派，防重入）/ 依赖（can_start 显式就绪检查）/ 产物指针。

- 存储：`{project_id}/tasks/{task_id}.json`（原子写，一部一文件便于单任务恢复）。
- 语义：`pending → in_progress(owner) → done`；`blocked`（被承诺门/依赖挂起）、`failed`。
- 崩溃恢复：编排器启动时 `recover()` 扫出 `in_progress` 未 `done` 任务，决定重做或续写。

与承诺账本（covenant）的衔接：任务启动前是否应检查"本次修订是否触碰承诺"由**编排器**在
`start(..., precheck=None)` 传入的可选回调决定（如 `touched_entries` 判断）；本模块不内置
LLM / 不依赖 covenant，保持确定性。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from ..storage.workspace import Workspace

TASK_STATUSES = ("pending", "in_progress", "done", "blocked", "failed")
TASK_KINDS = ("volume", "chapter", "event")


class TaskError(Exception):
    pass


class TaskBusyError(TaskError):
    """任务已被他方持锁（owner 冲突）：编排器不得重复指派不同 owner 到同进程内进行中的任务。"""


@dataclass
class Task:
    """一个可恢复的细粒度任务（卷/章/事件）。"""

    id: str
    kind: str  # volume | chapter | event
    ref: dict = field(default_factory=dict)      # {vol:int, ch:int, idx?:int}
    title: str = ""
    status: str = "pending"
    owner: str | None = None                      # 编排器指派的工作者标识
    dependencies: list[str] = field(default_factory=list)
    output: dict = field(default_factory=dict)    # 产物指针（细纲 md / 章正文…）
    meta: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    done_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            id=str(d.get("id", "")),
            kind=str(d.get("kind", "chapter")),
            ref=dict(d.get("ref") or {}),
            title=str(d.get("title") or ""),
            status=str(d.get("status", "pending")),
            owner=d.get("owner"),
            dependencies=list(d.get("dependencies") or []),
            output=dict(d.get("output") or {}),
            meta=dict(d.get("meta") or {}),
            created_at=float(d.get("created_at", 0) or 0),
            started_at=d.get("started_at"),
            done_at=d.get("done_at"),
        )


# 常用任务 id：章为 "ch:{vol}-{ch}"；卷为 "vol:{n}"；事件仅当需要事件级追踪时为 "ev:{vol}-{ch}-{idx}"
def chapter_task_id(vol: int, ch: int) -> str:
    return f"ch:{vol}-{ch}"


def volume_task_id(vol: int) -> str:
    return f"vol:{vol}"


class TaskStore:
    """任务板的读写门面（Workspace 支撑下的原子落盘）。

    设计为**确定性、不感知 LLM/业务**，只有"任务图"的本职；编排器负责在什么时机
    create / start / complete 哪些任务，并注入 covenant 等 precheck。
    """

    def __init__(self, ws: Workspace, project_id: str) -> None:
        self.ws = ws
        self.pid = project_id

    # ---- 存储路径 ----
    def _dir(self) -> Path:
        return self.ws._abs(f"{self.pid}/tasks/")  # noqa: SLF001

    def _path(self, task_id: str) -> Path:
        # Windows 文件名不允许 ':' ——逻辑 id 保留原样，仅存储名做安全替换
        return self._dir() / f"{task_id.replace(':', '_')}.json"

    # ---- 读写 ----
    def create(self, task: Task) -> Task:
        if task.status not in TASK_STATUSES:
            raise TaskError(f"invalid status {task.status!r}")
        if task.kind not in TASK_KINDS:
            raise TaskError(f"invalid kind {task.kind!r}")
        if self.get(task.id) is not None:
            raise TaskError(f"task {task.id} already exists")
        self.save(task)
        return task

    def save(self, task: Task) -> Task:
        self._dir().mkdir(parents=True, exist_ok=True)
        self.ws.write_json(self._path(task.id), task.to_dict())
        return task

    def get(self, task_id: str) -> Task | None:
        p = self._path(task_id)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        return Task.from_dict(d) if isinstance(d, dict) else None

    def list(self, *, status: str | None = None, kind: str | None = None) -> list[Task]:
        d = self._dir()
        out: list[Task] = []
        if not d.is_dir():
            return out
        for p in sorted(d.glob("*.json")):
            t = self.get(p.stem)
            if t is None:
                continue
            if status and t.status != status:
                continue
            if kind and t.kind != kind:
                continue
            out.append(t)
        return out

    def delete(self, task_id: str) -> bool:
        t = self.get(task_id)
        if t is None:
            return False
        if t.status == "in_progress":
            raise TaskError(f"cannot delete in-progress task {task_id}")
        p = self._path(task_id)
        if p.exists():
            p.unlink()
        return True

    # ---- 图操作 ----
    def dependencies_of(self, task_id: str) -> list[str]:
        t = self.get(task_id)
        return list(t.dependencies) if t else []

    def can_start(self, task_id: str) -> tuple[bool, list[str]]:
        """显式前置就绪检查：所有依赖必须已 done。

        返回 (ok, blockers)；blockers 列出仍未 done 的依赖 id。`covenant` 等
        语义性阻断由编排器在 `start(..., precheck=...)` 注入，不在此处内置。
        """
        t = self.get(task_id)
        if t is None:
            return False, [f"unknown task {task_id}"]
        blockers = [d for d in t.dependencies if (self.get(d) or Task(id=d)).status != "done"]
        return (not blockers), blockers

    def start(self, task_id: str, owner: str, *, precheck: Callable[[Task], tuple[bool, str]] | None = None) -> Task:
        """编排器指派 owner 并置 in_progress。

        防重入：任务已在途且被他人持锁（owner 不一致）→ 抛 `TaskBusyError`；
        同一 owner 重复 start 幂等。
        `precheck`：可选，返回 (ok, 阻断原因)；不通过 → 置 blocked 并抛 `TaskError`。
        """
        t = self.get(task_id)
        if t is None:
            raise TaskError(f"unknown task {task_id}")
        if t.status == "done":
            raise TaskError(f"task {task_id} already done")
        if t.status == "in_progress" and t.owner is not None and t.owner != owner:
            raise TaskBusyError(f"task {task_id} held by {t.owner!r}, cannot assign to {owner!r}")
        if precheck is not None:
            ok, reason = precheck(t)
            if not ok:
                t.status = "blocked"
                t.owner = owner
                t.meta["block_reason"] = reason
                self.save(t)
                raise TaskError(f"task {task_id} blocked: {reason}")
        t.status = "in_progress"
        t.owner = owner
        t.started_at = time.time()
        return self.save(t)

    def complete(self, task_id: str, output: dict | None = None) -> Task:
        t = self.get(task_id)
        if t is None:
            raise TaskError(f"unknown task {task_id}")
        t.status = "done"
        if output:
            t.output = {**t.output, **output}
        t.done_at = time.time()
        return self.save(t)

    def fail(self, task_id: str, reason: str) -> Task:
        t = self.get(task_id)
        if t is None:
            raise TaskError(f"unknown task {task_id}")
        t.status = "failed"
        t.meta["fail_reason"] = reason
        return self.save(t)

    def block(self, task_id: str, reason: str) -> Task:
        t = self.get(task_id)
        if t is None:
            raise TaskError(f"unknown task {task_id}")
        t.status = "blocked"
        t.meta["block_reason"] = reason
        return self.save(t)

    # ---- 崩溃恢复 ----
    def in_progress(self) -> list[Task]:
        return self.list(status="in_progress")

    def recover(self, *, policy: str = "list") -> list[Task]:
        """扫描崩溃遗留（in_progress 且未 done）的任务。

        policy：
        - "list"（默认）：只列出，交由编排器决定重做或续写，不改状态。
        - "reqdo"：把遗留任务连同依赖链上游一起回 `pending`（重做），供整段重建。
        返回受影响任务（被回滚的也在内）。"""
        stalled = self.in_progress()
        if policy == "list":
            return stalled
        if policy != "redo":
            raise TaskError(f"unknown recover policy {policy!r}")
        ids = [t.id for t in stalled]
        dependents = [t for t in self.list() if t.id not in ids and any(d in ids for d in t.dependencies)]
        affected = stalled + dependents
        for t in affected:
            t.status = "pending" if t.status in ("in_progress", "blocked") else t.status
            t.owner = None
            t.meta["recovered_at"] = time.time()
            self.save(t)
        return affected