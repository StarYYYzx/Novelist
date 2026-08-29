"""CLI（docs/07 §6.1）。命令骨架正在逐步真实化（M0：init）。"""

from __future__ import annotations

import click

from .config import load_config
from .core.errors import NovelistError
from .storage.checkpoint import Checkpoint
from .storage.workspace import WorkspaceError, Workspace


@click.group()
@click.option("--config", "config_path", default=None, help="全局配置文件路径")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """Novelist — 多 Agent 长篇小说撰写系统。"""
    ctx.ensure_object(dict)
    cfg = load_config(config_path)
    ctx.obj["config"] = cfg
    # workspace 根：默认当前目录；可用配置项 storage.workspace_root 覆盖
    root = cfg.storage.workspace_root or "."
    ctx.obj["workspace"] = Workspace(root=root)


def _new_project_id() -> str:
    import time

    return "proj-" + time.strftime("%Y%m%d%H%M%S")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.option("--title", default=None, help="书名")
@click.pass_context
def init(ctx: click.Context, directory: str | None, title: str | None) -> None:
    """新建项目工作区（docs/07 §6.1，UC-01）。"""
    ws: Workspace = ctx.obj["workspace"]
    project_id = _new_project_id()
    if directory:
        # 允许在指定目录下创建项目子目录
        ws = Workspace(root=directory)
    try:
        ws.create_project(project_id)
    except WorkspaceError as e:
        raise click.ClickException(str(e)) from e

    project = {
        "id": project_id,
        "title": title or project_id,
        "brief": "",
        "genre": None,
        "prefs": {},
        "budget": {},
        "pipeline_state": "立项",
        "event_seq": 0,
    }
    ck = Checkpoint(ws)
    ck.save(project_id, project)
    click.echo(f"created project {project_id} at {ws.project_dir(project_id)}")
    click.echo(f"  stage: {project['pipeline_state']}")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.option("--to", "to_stage", default="正文", help="跑流水线到指定工序")
@click.pass_context
def run(ctx: click.Context, directory: str | None, to_stage: str) -> None:
    """跑流水线到指定工序（docs/07 §6.1）。严格串行逐章；审查阶段触发一致性检查。"""
    from novelist.core.pipeline import PipelineStateMachine, PipelineStateError, PIPELINE_STAGES
    from novelist.consistency import run_consistency

    ws: Workspace = ctx.obj["workspace"]
    if directory:
        ws = Workspace(root=directory)
    ck = Checkpoint(ws)
    try:
        root = ws._abs("")
        project_id = _locate_project(root)
        project = ck.restore(project_id)
    except WorkspaceError as e:
        raise click.ClickException(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"run: {e}") from e

    try:
        st = PipelineStateMachine()
        cur = project.get("pipeline_state") or "立项"
        want = to_stage if to_stage in PIPELINE_STAGES else "正文"
        # 先推进到当前已存阶段（若存在），再从当前推进到目标
        if cur != st.current:
            _advance_to(st, cur)
        if want != st.current:
            _advance_to(st, want)
    except PipelineStateError:
        raise click.ClickException(
            f"cannot advance {project.get('pipeline_state')} -> {to_stage}（串行推进，仅允许前进）"
        ) from None

    # 审查及以上：执行一致性规则检查并报告
    alerts = []
    if _stage_ord(want) >= _stage_ord("审查"):
        alerts = run_consistency(ws, project_id)

    project["pipeline_state"] = st.current
    try:
        ck.save(project_id, project)
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"run save: {e}") from e

    click.echo(f"run {project.get('id')} -> {st.current}")
    if alerts:
        n_block = sum(1 for a in alerts if a.level == "block")
        click.echo(f"  consistency: {len(alerts)} alert(s), {n_block} block")
        for a in alerts[:5]:
            click.echo(f"    [{a.level}/{a.rule_id}] {a.object_ref}: {a.detail}")
    else:
        click.echo("  consistency: ok")


def _stage_ord(name: str) -> int:
    from novelist.core.pipeline import PIPELINE_STAGES

    if name in PIPELINE_STAGES:
        return PIPELINE_STAGES.index(name)
    return -1


def _advance_to(st, target: str) -> None:
    """逐步把状态机推进到 target（每次前进一步，直至目标）。"""
    while st.current != target:
        stages = st.stages
        i = stages.index(st.current)
        nxt = stages[i + 1]
        st.advance(nxt)


@cli.command()
@click.argument("directory", required=False, default=None)
@click.pass_context
def status(ctx: click.Context, directory: str | None) -> None:
    """查询进度与统计（docs/07 §6.1）。"""
    ws: Workspace = ctx.obj["workspace"]
    if directory:
        ws = Workspace(root=directory)
    ck = Checkpoint(ws)
    root = ws._abs("")
    project_id = _locate_project(root)
    try:
        project = ck.restore(project_id)
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"status: {e}") from e
    click.echo(f"project: {project.get('id')}")
    click.echo(f"title:   {project.get('title')}")
    click.echo(f"stage:   {project.get('pipeline_state')}")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.option("--vol", default=1, type=int, help="卷号")
@click.option("--ch", default=1, type=int, help="章节号")
@click.option("--provider", default="fake", help="LLM provider：fake/scripted/lmstudio/deepseek/openai")
@click.option("--direct/--loop", default=None, help="直出文本（本地慢模型）或走 Agent 工具循环；默认 local 模型用直出")
@click.option("--policy", default=None, help="权限策略文件（TOML，docs/07 §3.4）；缺省用 supervised 默认")
@click.pass_context
def chapter(ctx: click.Context, directory: str | None, vol: int, ch: int, provider: str, direct: bool | None,
            policy: str | None) -> None:
    """串行写一章：主编剧产出草稿 + 事件实时回写（docs/04 §4.1 / ADR-013）。

    --provider lmstudio 走本地 LM-Studio（默认直出文本，量力而为，避免多轮工具调用）。
    --policy 指定策略文件后，sensitive/danger 工具按策略处置；ask 时交互审批（grant 可见）。
    """
    from novelist.core.orchestrator import produce_chapter
    from novelist.core.approval import ApprovalQueue
    from novelist.core.tools import PermissionGate
    from novelist.tools import build_registry

    ws: Workspace = ctx.obj["workspace"]
    if directory:
        ws = Workspace(root=directory)
    ck = Checkpoint(ws)
    root = ws._abs("")
    try:
        project_id = _locate_project(root)
        ck.restore(project_id)
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"chapter: {e}") from e

    prov = _make_cli_provider(provider, vol, ch)
    # 本地模型默认直出（prefer_direct）；其余遵循用户 --direct/--loop 显式选择
    prefer_direct = True if provider in ("lmstudio", "local") else (direct if direct is not None else False)
    # 本地慢模型（约 20 token/s）用小生成预算，避免超时（量力而为）
    gen_tokens = 400 if provider in ("lmstudio", "local") else 4000

    # 门禁 + 审批：策略文件（可选）→ 默认 supervised；ask 走交互审批并持久化供 grant 查询
    # Embedding：按配置 provider.embedding 选取；无 key/不可用时自动降级为关键词索引（F9.4）
    from novelist.core.embedding import make_embedding

    emb = make_embedding(getattr(ctx.obj["config"].provider, "embedding", "keyword-fallback"))

    gate = PermissionGate.from_policy_file(policy) if policy else PermissionGate()
    approvals = ApprovalQueue(persist_dir=ws._abs(f"{project_id}/logs"))
    reg = build_registry(ws, gate=gate, approvals=approvals, decision_fn=_interactive_decision, embedding=emb)

    # 全链路：细纲读入 + 前导"先忆"（记忆检索）→ 拼进生成目标 → 正文 → 事件回写
    final_goal = _compose_goal(ws, project_id, vol, ch, embedding=emb)
    res = produce_chapter(ws, project_id, vol, ch, prov, registry=reg, prefer_direct=prefer_direct,
                          final_goal=final_goal, generation_tokens=gen_tokens, embedding=emb)
    if not res.ok:
        raise click.ClickException(f"chapter production failed: {res.result}")
    click.echo(f"wrote draft: {res.chapter_path} (mode={res.mode})")
    click.echo(f"events committed: {res.events_committed}")


def _compose_goal(ws, project_id: str, vol: int, ch: int, embedding=None) -> str:
    """组装生成目标：注入细纲要点 + 前导记忆近况（先忆，docs/04 §4.1 4a / ADR-011）。"""
    parts = [f"请撰写并输出第 {vol} 卷第 {ch} 章正文（project={project_id}）"]
    # 1) 细纲（outline/chapters/<vol>-<ch>.md），若存在
    gist_text = ""
    gist = ws.outline_chapter_path(project_id, vol, ch)
    if gist.exists():
        gist_text = gist.read_text(encoding="utf-8")[:800]
        parts.append("细纲：")
        parts.append(gist_text)
    # 2) 前导记忆（先忆）：以细纲为查询召回相关历史片段；无命中时退回近期事件摘要
    parts.append("近期记忆（前情速览）：")
    parts.append(_recall_memory(ws, project_id, gist_text or f"第 {vol} 卷第 {ch} 章", embedding=embedding))
    return "\n".join(parts)


def _recall_memory(ws, project_id: str, query: str, embedding=None, top_k: int = 5) -> str:
    """写作前"先忆"（docs/07 §7.1 / F3.4）：经记忆检索召回相关历史片段。

    索引缺失时自动从 memory/ 事实源重建；完全没有记忆时退回 plot_events 末尾摘要。
    """
    from novelist.core.memory import MemoryIndex, MemoryQuery, MemoryRetriever

    idx = MemoryIndex.load(ws, project_id)
    if not idx.fragments:
        idx.rebuild(ws, project_id, embedding)
    if idx.fragments:
        hits = MemoryRetriever(idx, embedding=embedding).query(MemoryQuery(query=query, top_k=top_k))
        if hits:
            return "\n".join(
                f"- [{h.kind} @ {h.source.get('vol')}:{h.source.get('ch')}] {h.text}" for h in hits
            )
    return _recent_memory_summary(ws, project_id)


def _recent_memory_summary(ws, project_id: str, limit: int = 3) -> str:
    """抽取 memory/plot_events.json 最近事件作前情摘要（docs/07 §7.1 先忆）。"""
    import json

    path = ws._abs(f"{project_id}/memory/plot_events.json")
    if not path.exists():
        return "（暂无）"
    try:
        events = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:  # pragma: no cover
        return "（暂无）"
    if not isinstance(events, list) or not events:
        return "（暂无）"
    return "; ".join(f"{e.get('vol')}:{e.get('ch')} {e.get('summary')}" for e in events[-limit:])


def _interactive_decision(req) -> str:
    """CLI 交互审批（docs/07 §3.3 CLI 提示）：ask 处置时向用户 y/n 询问。"""
    click.echo(f"[approval] tool={req.tool} reason={req.reason}")
    click.echo(f"  params={req.params}")
    try:
        ans = click.prompt("  allow? [y/N]", default="n")
    except click.Abort:
        return "deny"
    return "allow" if ans.strip().lower() in ("y", "yes") else "deny"


def _make_cli_provider(provider: str, vol: int = 1, ch: int = 1):
    from novelist.providers.fake import FakeProvider, ScriptedProvider

    if provider == "fake":
        # 占位演示：返回一段文本（不产生工具调用），展示循环结束
        return FakeProvider(reply="演示：fake provider 直接返回文本。")
    if provider in ("scripted", "demo"):
        # 默认/演示：脚本驱动一次 write_draft 工具调用（写入指定卷/章）+ 结束语
        return ScriptedProvider(
            [
                {"tool": "write_draft", "args": {"vol": vol, "ch": ch, "content": f"第 {ch} 章占位草稿：由 scripted provider 写入。"}},
                {"final": "done"},
            ]
        )
    if provider == "deepseek":
        from novelist.providers.deepseek import DeepSeekProvider

        return DeepSeekProvider(model="deepseek-chat")
    if provider in ("lmstudio", "local"):
        from novelist.providers.lmstudio import LMStudioProvider

        return LMStudioProvider()  # 默认 http://127.0.0.1:1234, qwen/qwen3.5-9b
    # openai 等真实 provider（需 key/base_url，见 providers.openai）
    from novelist.providers.openai import OpenAICompatibleProvider

    return OpenAICompatibleProvider(model="gpt-4o-mini")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.option("--approve", "approve_id", default=None, help="批准指定审批 id")
@click.option("--deny", "deny_id", default=None, help="拒绝指定审批 id")
@click.pass_context
def grant(ctx: click.Context, directory: str | None, approve_id: str | None, deny_id: str | None) -> None:
    """处理待决门禁审批（docs/07 §6.1，F6.1）。不传 --approve/--deny 时列出待决列表。"""
    from novelist.core.approval import ApprovalQueue

    ws: Workspace = ctx.obj["workspace"]
    if directory:
        ws = Workspace(root=directory)
    root = ws._abs("")
    project_id = _locate_project(root)
    queue = ApprovalQueue.load_persisted(persist_dir=ws._abs(f"{project_id}/logs"))

    if approve_id or deny_id:
        target = approve_id or deny_id
        ok_ = queue.decide(target, allow=bool(approve_id))
        if not ok_:
            raise click.ClickException(f"approval {target} not found (already decided?)")
        click.echo(f"{target} -> {'allow' if approve_id else 'deny'}")
        return

    pending = queue.list_pending()
    if not pending:
        click.echo("no pending approvals")
        return
    for r in pending:
        click.echo(f"{r.id}  tool={r.tool}  agent={getattr(r.session, 'agent', '?')}  {r.reason}")
        click.echo(f"    params={r.params}")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.option("--format", "fmt", default="markdown", help="发布包格式：markdown")
@click.option("--output", "out", default=None, help="输出文件路径；缺省打印到 stdout")
@click.option("--include-drafts", is_flag=True, default=False, help="把草稿并入发布包")
@click.pass_context
def export(ctx: click.Context, directory: str | None, fmt: str, out: str | None, include_drafts: bool) -> None:
    """导出发布包（docs/07 §6.1，F8.1）。"""
    from novelist.core.export import export_project

    ws: Workspace = ctx.obj["workspace"]
    if directory:
        ws = Workspace(root=directory)
    root = ws._abs("")
    project_id = _locate_project(root)
    if fmt != "markdown":
        raise click.ClickException(f"unsupported format: {fmt}（当前仅 markdown）")
    text = export_project(ws, project_id, include_drafts=include_drafts)
    if out:
        import os

        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True) if os.path.dirname(out) else None
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        click.echo(f"exported to {out} ({len(text)} chars)")
    else:
        click.echo(text)


@cli.command()
@click.argument("directory", required=False, default=None)
@click.pass_context
def stats(ctx: click.Context, directory: str | None) -> None:
    """输出项目统计（docs/07 §6.1，F8.2）。"""
    from novelist.core.export import collect_stats

    ws: Workspace = ctx.obj["workspace"]
    if directory:
        ws = Workspace(root=directory)
    root = ws._abs("")
    project_id = _locate_project(root)
    s = collect_stats(ws, project_id)
    click.echo(f"project: {project_id}")
    click.echo(f"chapters(published): {s.chapters}")
    click.echo(f"drafts:              {s.drafts}")
    click.echo(f"total words:         {s.total_words}")
    click.echo(f"plot events:         {s.plot_events}")
    click.echo(f"characters:          {s.characters}")


@cli.command()
@click.option("--host", default="127.0.0.1", help="监听地址")
@click.option("--port", default=8000, type=int, help="监听端口")
@click.pass_context
def server(ctx: click.Context, host: str, port: int) -> None:
    """启动 HTTP 服务（docs/07 §6.2）。"""
    import uvicorn

    uvicorn.run("novelist.server:app", host=host, port=port, reload=False)


def _locate_project(root) -> str:
    """在当前根下定位唯一项目 id（首层子目录中含 project.json 的）。"""
    import pathlib

    rp = pathlib.Path(root)
    found = [d.name for d in rp.iterdir() if d.is_dir() and (d / "project.json").exists()]
    if not found:
        raise click.ClickException("no project found; run `novelist init` first")
    if len(found) > 1:
        raise click.ClickException(f"multiple projects found: {found}; target one explicitly")
    return found[0]


def main() -> None:
    try:
        cli(obj={})
    except NovelistError as e:
        raise click.ClickException(f"{e.code}: {e.message}") from e


if __name__ == "__main__":
    main()
