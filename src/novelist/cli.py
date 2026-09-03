"""CLI（docs/07 §6.1）。命令骨架正在逐步真实化（M0：init）。"""

from __future__ import annotations

import json

import click

from .config import load_config
from .core.errors import NovelistError
from .core.session import SessionInfo
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

    ws, project_id = _resolve_project(ctx.obj["workspace"], directory)
    ck = Checkpoint(ws)
    try:
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
    ws, project_id = _resolve_project(ctx.obj["workspace"], directory)
    ck = Checkpoint(ws)
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
@click.option("--gen-tokens", type=int, default=None,
              help="单次生成总预算（B-05）。缺省时本地模型 400、其余 4000；"
                   "本地 9B 模型写满一章建议 1200–1500")
@click.option("--content-tokens", type=int, default=None,
              help="正文预算（第二批）：期望正文量；总预算至少覆盖它。缺省取配置或 3000")
@click.option("--length-cap", type=int, default=None,
              help="篇幅硬上限（字符，第七批）：超限截断到段落边界；缺省不截断")
@click.option("--max-events", type=int, default=None,
              help="每章事件数上限（第二批）：超限只取前 N 个；缺省不限制")
@click.option("--min-event-words", type=int, default=120,
              help="单事件最小篇幅（字符，第二批）：低于下限判失败；事件循环生效")
@click.option("--polish/--no-polish", default=False,
              help="成章后追加一次 LLM 调用优化文风（降低 AI 味），并用确定性指标复核")
@click.option("--no-bible", is_flag=True, default=False,
              help="关闭圣经注入（仅用于对照实验；默认开启，见 B-02）")
@click.option("--event-loop/--no-event-loop", default=False,
              help="按细纲 key_events 逐事件生成、逐事件回写（ADR-013 落地，第二批第 2 条）")
@click.option("--screenplay", is_flag=True, default=False,
              help="重场戏：先剧本体写对白交锋，再叙事化成小说（第三批第 2 条·档 2，两遍生成）")
@click.option("--readback", is_flag=True, default=False,
              help="回读机制：生成前注入前 1 章正文原文（第七批第 4 条）")
@click.option("--event-polish", is_flag=True, default=False,
              help="事件级润色：每事件写完即润色，源头统一风格（第七批第 2 条）")
@click.option("--supplement-settings", is_flag=True, default=False,
              help="世界观滚动补充：事件新名词补 settings 条目（第七批第 5 条·递归分层 B）")
@click.option("--no-jit", is_flag=True, default=False,
              help="关闭人物 JIT 补卡（默认开启，第七批第 5 条·递归分层 A）")
@click.pass_context
def chapter(ctx: click.Context, directory: str | None, vol: int, ch: int, provider: str, direct: bool | None,
            policy: str | None, gen_tokens: int | None, content_tokens: int | None,
            length_cap: int | None, max_events: int | None, min_event_words: int,
            polish: bool, no_bible: bool,
            event_loop: bool, screenplay: bool, readback: bool, event_polish: bool,
            supplement_settings: bool, no_jit: bool) -> None:
    """串行写一章：圣经注入 → 生成 → 完整性校验 → 文风润色 → 编纂员回写事件。

    --provider lmstudio 走本地 LM-Studio（默认直出文本，量力而为，避免多轮工具调用）。
    --policy 指定策略文件后，sensitive/danger 工具按策略处置；ask 时交互审批（grant 可见）。
    """
    from novelist.core.orchestrator import produce_chapter
    from novelist.core.approval import ApprovalQueue
    from novelist.core.tools import PermissionGate
    from novelist.tools import build_registry

    ws, project_id = _resolve_project(ctx.obj["workspace"], directory)
    ck = Checkpoint(ws)
    try:
        ck.restore(project_id)
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"chapter: {e}") from e

    prov = _make_cli_provider(provider, vol, ch)
    # 本地模型默认直出（prefer_direct）；其余遵循用户 --direct/--loop 显式选择
    prefer_direct = True if provider in ("lmstudio", "local") else (direct if direct is not None else False)
    # 生成预算（B-05）：显式 --gen-tokens 优先；否则本地模型 400、其余 4000
    if not gen_tokens:
        gen_tokens = 400 if provider in ("lmstudio", "local") else 4000

    # 门禁 + 审批：策略文件（可选）→ 默认 supervised；ask 走交互审批并持久化供 grant 查询
    # Embedding：按配置 provider.embedding 选取；无 key/不可用时自动降级为关键词索引（F9.4）
    from novelist.core.embedding import make_embedding

    emb = make_embedding(getattr(ctx.obj["config"].provider, "embedding", "keyword-fallback"))

    gate = PermissionGate.from_policy_file(policy) if policy else PermissionGate()
    approvals = ApprovalQueue(persist_dir=ws._abs(f"{project_id}/logs"))
    reg = build_registry(ws, gate=gate, approvals=approvals, decision_fn=_interactive_decision, embedding=emb)

    # 先忆结果交给编排层注入圣经上下文（B-02）；关闭注入时仍可用于旧链路
    memories = _recall_lines(ws, project_id, vol, ch, embedding=emb)
    res = produce_chapter(ws, project_id, vol, ch, prov, registry=reg, prefer_direct=prefer_direct,
                          generation_tokens=gen_tokens, content_tokens=content_tokens,
                          length_cap_chars=length_cap, max_events_per_chapter=max_events,
                          min_event_words=min_event_words,
                          embedding=emb,
                          inject_bible=not no_bible, memories=memories or None, polish=polish,
                          event_loop=event_loop, screenplay=screenplay,
                          readback=readback, event_polish=event_polish,
                          supplement_settings=supplement_settings, jit_characters=not no_jit)
    if not res.ok:
        raise click.ClickException(f"chapter production failed: {res.result}")
    click.echo(f"wrote draft: {res.chapter_path} (mode={res.mode}, bible={res.bible_injected}, "
               f"attempts={res.attempts})")
    click.echo(f"phase: {res.phase}" + (f" ({res.phase_reason})" if res.phase_reason else ""))
    click.echo(f"events committed: {res.events_committed}")
    if res.entity_new or res.entity_alerts:
        click.echo(f"entities: +{res.entity_new}"
                   + (f", alerts: {len(res.entity_alerts)}" if res.entity_alerts else ""))
        for a in res.entity_alerts:
            click.echo(f"  [entity] {a}")
    if res.completeness:
        c = res.completeness
        flag = "ok" if c.get("ends_properly") and not c.get("meta_narration") else "CHECK"
        click.echo(f"completeness: {flag} ({c.get('chars')} 字, 末字「{c.get('last_char')}」"
                   + (f", 元叙事={c['meta_narration']}" if c.get("meta_narration") else "")
                   + (f", 未解决={c['_unresolved']}" if c.get("_unresolved") else "") + ")")
    if res.length_truncated:
        click.echo(f"length: TRUNCATED (超 {res.completeness.get('chars', '?')} 字上限，已截断到段落边界)")
    if res.events_capped:
        click.echo(f"events: capped（细纲 {res.events_capped + res.events_committed} 个事件超上限，"
                   f"取前 {res.events_committed} 个）")
    if res.polish is not None:
        p = res.polish
        click.echo(f"polish: {'applied' if p.changed else 'kept original'} "
                   f"AI味 {p.before.score} -> {p.after.score} ({p.delta:+.2f}) {p.note}")


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


def _recall_lines(ws, project_id: str, vol: int, ch: int, embedding=None, top_k: int = 6) -> list[str]:
    """先忆结果（行列表），供编排层注入圣经上下文（B-02）。"""
    gist = ws.outline_chapter_path(project_id, vol, ch)
    gist_text = gist.read_text(encoding="utf-8")[:800] if gist.exists() else ""
    return _recall(ws, project_id, gist_text or f"第 {vol} 卷第 {ch} 章", embedding=embedding, top_k=top_k)


def _recall(ws, project_id: str, query: str, embedding=None, top_k: int = 6) -> list[str]:
    """记忆检索，返回「- [kind @ vol:ch] text」行列表；无记忆时为空列表。"""
    from novelist.core.memory import MemoryIndex, MemoryQuery, MemoryRetriever

    idx = MemoryIndex.load(ws, project_id)
    if not idx.fragments:
        idx.rebuild(ws, project_id, embedding)
    if not idx.fragments:
        return []
    hits = MemoryRetriever(idx, embedding=embedding).query(MemoryQuery(query=query, top_k=top_k))
    return [f"- [{h.kind} @ {h.source.get('vol')}:{h.source.get('ch')}] {h.text}"
            for h in hits if h.score > 0]


def _recall_memory(ws, project_id: str, query: str, embedding=None, top_k: int = 5) -> str:
    """写作前"先忆"（docs/07 §7.1 / F3.4）：经记忆检索召回相关历史片段。

    索引缺失时自动从 memory/ 事实源重建；完全没有记忆时退回 plot_events 末尾摘要。
    """
    lines = _recall(ws, project_id, query, embedding=embedding, top_k=top_k)
    if lines:
        return "\n".join(lines)
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

        # 思考型模型（qwen3.5-9b）+ 大 prompt（forge 构建节点）单次调用可达 4-5 分钟
        # （10 token/s × 2600 + 思考），默认 120s 会让 book 节点超时回退父层（实测）。
        # 构建/写作场景统一放宽到 600s，超时仅作兜底保护。
        return LMStudioProvider(timeout_s=600)
    # openai 等真实 provider（需 key/base_url，见 providers.openai）
    from novelist.providers.openai import OpenAICompatibleProvider

    return OpenAICompatibleProvider(model="gpt-4o-mini")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.option("--vol", type=int, default=None, help="卷号；与 --ch 配对使用")
@click.option("--ch", type=int, default=None, help="章号；与 --vol 配对使用")
@click.option("--all", "all_chapters", is_flag=True, default=False, help="转正全部草稿")
@click.option("--policy", default=None, help="权限策略文件；sensitive 设为 allow 可免交互")
@click.pass_context
def promote(ctx: click.Context, directory: str | None, vol: int | None, ch: int | None,
            all_chapters: bool, policy: str | None) -> None:
    """草稿转正为正式章节（docs/06 §4.2 reviewed_ok → published，B-06）。

    promote_draft 是 sensitive 工具：默认走 ask 门禁。不带 --policy 时会交互询问，
    也可用策略文件把 sensitive 设为 allow 批量放行。
    """
    from novelist.core.approval import ApprovalQueue
    from novelist.core.tools import PermissionGate
    from novelist.tools import build_registry

    ws, project_id = _resolve_project(ctx.obj["workspace"], directory)
    if not all_chapters and (vol is None or ch is None):
        raise click.ClickException("specify --vol/--ch, or use --all")

    gate = PermissionGate.from_policy_file(policy) if policy else PermissionGate()
    approvals = ApprovalQueue(persist_dir=ws._abs(f"{project_id}/logs"))
    reg = build_registry(ws, gate=gate, approvals=approvals, decision_fn=_interactive_decision)
    sess = SessionInfo(project_id=project_id, agent="cli")

    targets: list[tuple[int, int]] = []
    if all_chapters:
        for f in sorted((ws.project_dir(project_id) / "drafts" / "chapters").glob("*.md")):
            parts = f.stem.split("-")
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                targets.append((int(parts[0]), int(parts[1])))
    else:
        targets.append((int(vol or 0), int(ch or 0)))

    ok_n = 0
    for v, c in targets:
        r = reg.invoke(sess, "promote_draft", {"vol": v, "ch": c})
        if r.status == "ok":
            ok_n += 1
            click.echo(f"promoted {v}-{c}")
        else:
            click.echo(f"FAILED {v}-{c}: {r.code} {r.data}")
    click.echo(f"promoted {ok_n}/{len(targets)} chapter(s)")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.option("--provider", default="lmstudio", help="审校用的 LLM provider")
@click.option("--max-show", default=20, type=int, help="最多展示多少条告警")
@click.pass_context
def review(ctx: click.Context, directory: str | None, provider: str, max_show: int) -> None:
    """一致性审查：确定性规则 + 审校师语义检（docs/04 §5.4，B-08）。

    不传 provider 也能跑（只跑规则层）；传了才会追加 LLM 语义审校。
    """
    from novelist.consistency import run_consistency

    ws, project_id = _resolve_project(ctx.obj["workspace"], directory)

    llm = None
    if provider and provider != "none":
        try:
            llm = _make_cli_provider(provider)
        except Exception as e:  # noqa: BLE001 - provider 不可用时降级为纯规则层
            click.echo(f"[warn] provider unavailable, rule layer only: {e}")

    alerts = run_consistency(ws, project_id, llm=llm)
    blocks = [a for a in alerts if a.level == "block"]
    warns = [a for a in alerts if a.level == "warn"]
    click.echo(f"alerts: {len(alerts)} (block {len(blocks)} / warn {len(warns)})")
    for a in alerts[:max_show]:
        click.echo(f"  [{a.level}/{a.rule_id}] {a.object_ref}: {a.detail}")
    if len(alerts) > max_show:
        click.echo(f"  ... {len(alerts) - max_show} more")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.option("--approve", "approve_id", default=None, help="批准指定审批 id")
@click.option("--deny", "deny_id", default=None, help="拒绝指定审批 id")
@click.pass_context
def grant(ctx: click.Context, directory: str | None, approve_id: str | None, deny_id: str | None) -> None:
    """处理待决门禁审批（docs/07 §6.1，F6.1）。不传 --approve/--deny 时列出待决列表。"""
    from novelist.core.approval import ApprovalQueue

    ws, project_id = _resolve_project(ctx.obj["workspace"], directory)
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
@click.option("--format", "fmt", type=click.Choice(["markdown", "docx"]), default="markdown",
              help="发布包格式：markdown | docx（Word 成稿，需 --output）")
@click.option("--output", "out", default=None, help="输出文件路径；markdown 缺省打印到 stdout，docx 必填")
@click.option("--include-drafts", is_flag=True, default=False, help="把草稿并入发布包")
@click.pass_context
def export(ctx: click.Context, directory: str | None, fmt: str, out: str | None, include_drafts: bool) -> None:
    """导出发布包（docs/07 §6.1，F8.1）。fmt=docx 直接产出 Word 成稿（M3o）。"""
    from novelist.core.export import export_project

    ws, project_id = _resolve_project(ctx.obj["workspace"], directory)
    text = export_project(ws, project_id, include_drafts=include_drafts)
    if fmt == "docx":
        import os

        from novelist.core.docxconv import DocxConvError, markdown_to_docx

        if not out:
            raise click.ClickException("fmt=docx 需用 --output 指定 .docx 输出路径")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True) if os.path.dirname(out) else None
        try:
            path = markdown_to_docx(text, out, title=project_id)
        except DocxConvError as e:
            raise click.ClickException(str(e)) from e
        click.echo(f"exported to {path}（Word 成稿，{len(text)} 字符源文本）")
        return
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

    ws, project_id = _resolve_project(ctx.obj["workspace"], directory)
    s = collect_stats(ws, project_id)
    click.echo(f"project: {project_id}")
    click.echo(f"chapters(published): {s.chapters}")
    click.echo(f"drafts:              {s.drafts}")
    click.echo(f"total words:         {s.total_words}")
    click.echo(f"plot events:         {s.plot_events}")
    click.echo(f"characters:          {s.characters}")


@cli.command()
@click.argument("directory", required=False, default=None)
@click.pass_context
def validate(ctx: click.Context, directory: str | None) -> None:
    """校验项目事实源 ↔ schema 契约（F0'，docs/06 §5 / core/bible.py）。

    传入项目目录直接校验该项目；传入 workspace 根则遍历全部项目。
    """
    import pathlib

    from novelist.core.bible import BIBLE_CONTRACT, validate_project

    ws: Workspace = ctx.obj["workspace"]
    if directory:
        ws = Workspace(root=directory)
    root = pathlib.Path(ws._abs(""))
    if (root / "project.json").exists():
        base = root.parent
        projects = [root.name]
    else:
        base = root
        projects = sorted(d.name for d in base.iterdir() if d.is_dir() and (d / "project.json").exists())
    if not projects:
        raise click.ClickException("no project found; run `novelist init` first")
    if str(base) != str(root):
        ws = Workspace(root=str(base))
    failed = 0
    for project_id in projects:
        violations = validate_project(ws, project_id)
        if not violations:
            click.echo(f"{project_id}: 契约校验全过（{len(BIBLE_CONTRACT)} 类映射）")
            continue
        failed += 1
        click.echo(f"{project_id}: {len(violations)} 个文件未通过契约校验")
        for v in violations:
            click.echo(f"[FAIL] {v.path} (schema={v.schema})")
            for e in v.errors:
                click.echo(f"    {e}")
    if failed:
        raise click.ClickException(f"{failed} 个项目存在契约违规，见上方清单")


@cli.group()
def forge() -> None:
    """构建层 Forge（docs/10）：seed/ingest/show/resume/build/roll/rollback/validate。"""


def _resolve_forge_target(ws: Workspace, directory: str | None) -> tuple[Workspace, str]:
    """解析 forge 目标项目（D7 起委托 _resolve_project，全 CLI 单一定位语义）。"""
    return _resolve_project(ws, directory)


@forge.command("show")
@click.argument("directory", required=False, default=None)
@click.pass_context
def forge_show(ctx: click.Context, directory: str | None) -> None:
    """打印蓝图 / 缺口 / 进度 / 调用数（F0，无 LLM）。"""
    from novelist.forge import Blueprint, ForgeState, show_summary

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    try:
        bp = Blueprint.load(ws, project_id)
    except FileNotFoundError:
        raise click.ClickException(
            f"{project_id}: 尚无蓝图（workspace/forge/blueprint.json）——先跑 `forge seed`"
        ) from None
    state = ForgeState.load(ws, project_id)
    click.echo(f"== {project_id} ==")
    for line in show_summary(bp, state):
        click.echo(line)


# ---- 审核闸门命令（ADR-024：分模块权限开关）----

@forge.command("review")
@click.argument("module", required=False, default=None)
@click.option("--dir", "directory", default=None, help="目标项目目录")
@click.pass_context
def forge_review(ctx: click.Context, module: str | None, directory: str | None) -> None:
    """查看待审模块：无参列出 pending；给 MODULE 打印该模块评审稿全文。"""
    from novelist.forge.review import REVIEW_MODULES, load_review

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    cfg = load_review(ws, project_id)
    if module is None:
        if not cfg["pending"]:
            click.echo("（无待审模块）")
            return
        click.echo(f"== {project_id} 待审模块 ==")
        for m, e in sorted(cfg["pending"].items()):
            label = REVIEW_MODULES[m]["label"]
            click.echo(f"  {m}（{label}）→ {e['file']}")
        click.echo("处置：forge review <module> 看全文；forge approve/revise <module>")
        return
    if module not in REVIEW_MODULES:
        raise click.ClickException(f"未知模块 {module}；可选：{', '.join(REVIEW_MODULES)}")
    e = cfg["pending"].get(module)
    if e is None:
        raise click.ClickException(f"{module}: 无待审内容")
    path = ws._abs(f"{project_id}/{e['file']}")  # noqa: SLF001
    click.echo(path.read_text(encoding="utf-8") if path.exists()
               else f"（评审稿缺失：{e['file']}）")


@forge.command("approve")
@click.argument("module")
@click.option("--remember", is_flag=True, default=False,
              help="可行，且该模块后续不再审核（开关永久关闭）")
@click.option("--dir", "directory", default=None, help="目标项目目录")
@click.pass_context
def forge_approve(ctx: click.Context, module: str, remember: bool,
                  directory: str | None) -> None:
    """审核通过：清除 pending；--remember 同时永久关闭该模块审核开关。"""
    from novelist.forge.review import REVIEW_MODULES, resolve_pending

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    if module not in REVIEW_MODULES:
        raise click.ClickException(f"未知模块 {module}；可选：{', '.join(REVIEW_MODULES)}")
    resolve_pending(ws, project_id, module, decision="approved",
                    remember=remember)
    click.echo(f"approve {module} ✓" + ("（后续不再审核该模块）" if remember else ""))


@forge.command("revise")
@click.argument("module")
@click.argument("suggestions")
@click.option("--provider", default="fake", help="fake|lmstudio|deepseek|openai|scripted")
@click.option("--dir", "directory", default=None, help="目标项目目录")
@click.pass_context
def forge_revise(ctx: click.Context, module: str, suggestions: str,
                 provider: str, directory: str | None) -> None:
    """按修改建议重生成模块内容并再次展示（新旧差异如实列出）。"""
    from novelist.forge.engine import revise_module
    from novelist.forge.review import REVIEW_MODULES, load_review

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    if module not in REVIEW_MODULES:
        raise click.ClickException(f"未知模块 {module}；可选：{', '.join(REVIEW_MODULES)}")
    if module not in load_review(ws, project_id)["pending"]:
        raise click.ClickException(f"{module}: 无待审内容")
    diffs = revise_module(ws, project_id, module, suggestions,
                          _make_cli_provider(provider))
    click.echo(f"revise {module} 完成，仍待审核。与旧版差异：")
    if diffs:
        for ln in diffs[:40]:
            click.echo(f"  {ln}")
        if len(diffs) > 40:
            click.echo(f"  …（共 {len(diffs)} 条，详见评审稿）")
    else:
        click.echo("  （无结构差异）")
    chapters = ws._abs(f"{project_id}/chapters")  # noqa: SLF001
    if chapters.exists() and any(chapters.iterdir()):
        click.echo("  [risk] 项目已有正文——基于旧设计生成的章节可能与新设定不一致"
                   "（正文自动修订暂缓，需人工复核）")


@forge.command("switches")
@click.argument("module", required=False, default=None)
@click.argument("state", required=False, default=None,
                type=click.Choice(["on", "off"]))
@click.option("--dir", "directory", default=None, help="目标项目目录")
@click.pass_context
def forge_switches(ctx: click.Context, module: str | None, state: str | None,
                   directory: str | None) -> None:
    """查看/设置模块审核开关（on=需审核）：forge switches [MODULE on|off]。"""
    from novelist.forge.review import REVIEW_MODULES, load_review, set_switch

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    cfg = load_review(ws, project_id)
    if module is None:
        click.echo(f"== {project_id} 审核开关 ==")
        for m, spec in REVIEW_MODULES.items():
            on = cfg["switches"].get(m, True)
            pend = "（待审）" if m in cfg["pending"] else ""
            click.echo(f"  {'开' if on else '关'}  {m}（{spec['label']}）{pend}")
        return
    if module not in REVIEW_MODULES:
        raise click.ClickException(f"未知模块 {module}；可选：{', '.join(REVIEW_MODULES)}")
    if state is None:
        on = cfg["switches"].get(module, True)
        click.echo(f"{module}: {'开（需审核）' if on else '关（LLM 自由发挥）'}")
        return
    set_switch(ws, project_id, module, state == "on")
    click.echo(f"{module} → {'开（需审核）' if state == 'on' else '关（LLM 自由发挥）'}")


# forge seed 的 fake 演示回复（SeedSpec JSON；构建节点走宽容降级，链路完整可演示）
_FAKE_SEED_REPLY = json.dumps({
    "genre": "修仙", "template_suggestion": "修仙男频",
    "logline": "五五开系统，绑定他人共享修炼",
    "protagonist_hint": {"name": "叶蓝", "gender": "male", "cheat": "五五开系统"},
    "conflict": "废柴逆袭",
    "tone_hint": "热血激昂",
    "scale_hint": {"volumes": 2, "chapters_per_volume": 3},
    "time_origin": "叶蓝穿越之日",
    "unknowns": [],
}, ensure_ascii=False)


@forge.command("seed")
@click.argument("brief")
@click.option("--dir", "directory", default=None, help="目标项目目录（缺省取 workspace 根下唯一项目）")
@click.option("--mode", type=click.Choice(["auto", "interactive"]), default="auto",
              help="interactive 会先展示提炼结果并授权询问；非 TTY 自动降级 auto")
@click.option("--provider", default="fake", help="fake|lmstudio|deepseek|openai|scripted")
@click.option("--genre-pack", default=None, help="类型包 id（缺省由提炼匹配，兜底通用包）")
@click.option("--volumes", type=int, default=None, help="卷数（缺省由提炼/模板定）")
@click.option("--chapters-per-volume", type=int, default=None, help="每卷章数")
@click.option("--target-words", type=int, default=None, help="每章目标字数")
@click.option("--max-calls", type=int, default=60, help="构建分阶段配额（build 卷 1 默认 60）")
@click.option("--max-depth", type=int, default=4, help="递归最大深度")
@click.option("--max-width", type=int, default=4, help="子节点宽度上限")
@click.option("--smoke", is_flag=True, default=False, help="冒烟：只提炼 + 建蓝图，不跑构建")
@click.pass_context
def forge_seed(ctx: click.Context, brief: str, directory: str | None, mode: str,
               provider: str, genre_pack: str | None,
               volumes: int | None, chapters_per_volume: int | None,
               target_words: int | None,
               max_calls: int, max_depth: int, max_width: int, smoke: bool) -> None:
    """模式一：一句话创意 → 种子提炼 + 授权询问 → 全权构建（F1）。

    产出：workspace/forge/blueprint.json + bible/* + outline/volumes.json +
    outline/chapters/1-*.md + worldstate 确定性合成。之后可 `chapter 1 1` 直出正文。
    """
    from novelist.forge import ForgeState, run_seed

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    if provider == "fake":
        from novelist.providers.fake import FakeProvider

        prov = FakeProvider(reply=_FAKE_SEED_REPLY)  # 演示：固定 SeedSpec，链路可跑通
    else:
        prov = _make_cli_provider(provider)
    state = ForgeState.load(ws, project_id)
    if state.stage == "built" and not smoke:
        click.echo(f"{project_id}: 已构建过（stage=built）。改动蓝图后重跑用 `forge build`；"
                   f"强制重seed 请先手改 project.json。")
    res = run_seed(ws, project_id, brief, provider=prov, mode=mode,
                   genre_pack=genre_pack, volumes=volumes,
                   chapters_per_volume=chapters_per_volume, target_words=target_words,
                   max_calls=max_calls, max_depth=max_depth, max_width=max_width,
                   smoke=smoke)
    for w in res.warnings:
        click.echo(f"  [warn] {w}", err=True)
    click.echo(f"seed done: mode={res.mode_used} calls={res.build.get('calls_used', 0)}")
    if smoke:
        click.echo("smoke 模式：仅提炼 + 建蓝图，未跑构建。可 `forge show` 查看后 `forge build`。")
        return
    click.echo(
        f"build: 卷主线 {res.build.get('volumes_written', 0)} 卷 / 卷 1 细纲 "
        f"{res.build.get('chapters_written', 0)} 章 / 调用 {res.build.get('calls_used', 0)}"
        f"（配额 {res.build.get('budget_limit')}）"
    )
    if res.build.get("budget_exhausted"):
        click.echo("预算耗尽：部分节点未生成，改动蓝图后 `forge resume` 续跑。", err=True)
    if res.quit_early:
        click.echo(f"下一步：`forge resume --dir {project_id}` 继续商讨，或 `forge build --dir {project_id}` 直接构建")
        return
    if not res.ok:
        raise click.ClickException("seed 构建未完成，见上方 warnings")
    click.echo(f"下一步：`novelist chapter 1 1 --dir {project_id}` 或 `novelist forge show {project_id}`")


@forge.command("build")
@click.argument("directory", required=False, default=None)
@click.option("--force", is_flag=True, default=False, help="已有 chapters/ 时强制（docs/10 §7.6）")
@click.option("--provider", default="fake", help="fake|lmstudio|deepseek|openai|scripted")
@click.option("--max-calls", type=int, default=60, help="构建分阶段配额")
@click.option("--no-deepen", is_flag=True, default=False,
              help="退化为 F1 最小树（跳过旁支递归深化与 arc/beat 层）")
@click.option("--diff", is_flag=True, default=False,
              help="影响分析：比对最近快照，只重建被改实体引用的章/节点（docs/10 §9.6）")
@click.pass_context
def forge_build(ctx: click.Context, directory: str | None, force: bool, provider: str,
                max_calls: int, no_deepen: bool, diff: bool) -> None:
    """重跑构建引擎（蓝图已有时）：provenance 保护 + 幂等落盘。

    默认拒绝已有 chapters/ 的项目（先写正文或 ingest 的项目）；--force 放行。
    --diff 走影响分析：清掉被改实体引用的章细纲与节点后再构建。
    """
    from novelist.forge import Blueprint, build, diff_affected

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    try:
        Blueprint.load(ws, project_id)
    except FileNotFoundError:
        raise click.ClickException(f"{project_id}: 尚无蓝图——先跑 `forge seed`") from None
    chapters = list(ws._abs(f"{project_id}/chapters").glob("*.md"))  # noqa: SLF001
    if chapters and not force:
        raise click.ClickException(
            f"{project_id}: 已有 {len(chapters)} 章正文（chapters/）——Forge 拒绝覆盖已有稿子；"
            f"确认要继续请加 --force"
        )
    if diff:
        plan = diff_affected(ws, project_id)
        removed = plan.apply(ws, project_id)
        click.echo(f"diff: {plan.summary}（清理 {removed} 个产物文件）")
        if plan.rebuild_all:
            click.echo("  [info] 结构级变更（worldview/style/volumes/arcs）→ 全量重建")
    res = build(ws, project_id, provider=_make_cli_provider(provider),
                max_calls=max_calls, resume=False, deepen=not no_deepen)
    for w in res.warnings:
        click.echo(f"  [warn] {w}", err=True)
    if res.gate_halted:
        click.echo("审核闸门：以下模块待处置后再续跑 build/resume：")
        for m in res.pending_review:
            click.echo(f"  - {m}（forge review {m} → approve / revise）")
        return
    click.echo(f"build done: calls={res.calls_used} nodes={res.nodes_done} "
               f"卷={res.volumes_written} 章={res.chapters_written}")
    if not res.ok:
        raise click.ClickException("构建未完成，见上方 warnings")


@forge.command("roll")
@click.argument("vol", type=int)
@click.argument("directory", required=False, default=None)
@click.option("--provider", default="fake", help="fake|lmstudio|deepseek|openai")
@click.option("--max-calls", type=int, default=40, help="roll 每卷分阶段配额（docs/10 §7.3）")
@click.pass_context
def forge_roll(ctx: click.Context, vol: int, directory: str | None, provider: str,
               max_calls: int) -> None:
    """滚动生成第 N 卷细纲（需前卷已有正文）：注入前卷事实四块上下文（docs/10 §7.7）。"""
    from novelist.forge import Blueprint, roll

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    try:
        Blueprint.load(ws, project_id)
    except FileNotFoundError:
        raise click.ClickException(f"{project_id}: 尚无蓝图——先跑 `forge seed`") from None
    try:
        res = roll(ws, project_id, provider=_make_cli_provider(provider),
                   vol=vol, max_calls=max_calls)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    for w in res.warnings:
        click.echo(f"  [warn] {w}", err=True)
    click.echo(f"roll vol {vol} done: calls={res.calls_used} 章={res.chapters_written} "
               f"弧={res.arcs_written}")
    if not res.ok:
        raise click.ClickException("滚动生成未完成，见上方 warnings")


@forge.command("resume")
@click.argument("directory", required=False, default=None)
@click.option("--max-calls", type=int, default=60, help="构建分阶段配额")
@click.option("--provider", default="fake", help="fake|lmstudio|deepseek|openai|scripted（商讨续跑时生成候选）")
@click.option("--no-deepen", is_flag=True, default=False, help="续跑构建时跳过旁支深化与 arc/beat 层")
@click.pass_context
def forge_resume(ctx: click.Context, directory: str | None, max_calls: int, provider: str,
                 no_deepen: bool) -> None:
    """断点续跑：商讨中断 → 续问答；构建中断 → 跳过已落盘节点续构建（幂等）。"""
    from novelist.forge import Blueprint, ForgeState, build, run_consult

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    try:
        bp = Blueprint.load(ws, project_id)
    except FileNotFoundError:
        raise click.ClickException(f"{project_id}: 尚无蓝图——先跑 `forge seed`") from None
    state = ForgeState.load(ws, project_id)
    if state.stage == "consulting":
        # 商讨续问：已答槽位自动跳过（transcript 判据），q 可再次退出
        from novelist.forge import ConsoleIO

        consult = run_consult(ws, project_id, bp, provider=_make_cli_provider(provider),
                              io=ConsoleIO())
        for w in consult.warnings:
            click.echo(f"  [warn] {w}", err=True)
        click.echo(f"consult resume done: 已答 {consult.answered} 项 / 自由 {consult.free_answers} / "
                   f"完成 {consult.rounds_done} 轮")
        if consult.quit_early:
            click.echo(f"下一步：`forge resume --dir {project_id}` 继续，或 `forge build --dir {project_id}` 直接构建")
            return
        state.touch_stage(ws, project_id, "seeded")
        click.echo(f"商讨完成。下一步：`forge build --dir {project_id}` 开始构建")
        return
    res = build(ws, project_id, provider=_make_cli_provider(provider),
                max_calls=max_calls, resume=True, deepen=not no_deepen)
    for w in res.warnings:
        click.echo(f"  [warn] {w}", err=True)
    if res.gate_halted:
        click.echo("审核闸门：以下模块待处置后再续跑：")
        for m in res.pending_review:
            click.echo(f"  - {m}（forge review {m} → approve / revise）")
        return
    click.echo(f"resume done: calls={res.calls_used} nodes={res.nodes_done} "
               f"卷={res.volumes_written} 章={res.chapters_written}")
    if not res.ok and not res.interrupted:
        raise click.ClickException("续跑未完成，见上方 warnings")


@forge.command("ingest")
@click.argument("source")
@click.argument("directory", required=False, default=None)
@click.option("--provider", default="fake", help="fake|lmstudio|deepseek|openai|scripted（语义抽取）")
@click.option("--genre-pack", default=None, help="类型包 id（缺省通用包；抽取词表随包）")
@click.option("--chapters-per-volume", type=int, default=None, help="每卷章数（缺省 20）")
@click.option("--target-words", type=int, default=None, help="切章目标字数（缺省 2400）")
@click.option("--ingest-max-calls", type=int, default=30, help="抽取独立配额（超出降级纯确定性）")
@click.option("--mode", type=click.Choice(["auto", "interactive"]), default="auto",
              help="interactive 且 TTY：缺口回落商讨；非 TTY 自动降级")
@click.option("--dry-run", is_flag=True, default=False, help="只切章预览，不写库")
@click.option("--recursive", is_flag=True, default=False, help="递归扫描子目录 .md/.txt/.docx")
@click.pass_context
def forge_ingest(ctx: click.Context, source: str, directory: str | None, provider: str,
                 genre_pack: str | None, chapters_per_volume: int | None,
                 target_words: int | None, ingest_max_calls: int,
                 mode: str, dry_run: bool, recursive: bool) -> None:
    """模式二：已有稿子 → 蓝图 + 正式章节 + 记忆初始化（F3）。支持 .docx 源（自动转 md）。

    切章预览确认后：抽取（确定性优先 + LLM 补语义，独立配额降级）→ 归并消歧 →
    文风画像 → 卷章编码（chapters/<vol>-<ch>.md 已是正式章节 + 细纲 done=true）→
    chronicler 记忆初始化 + entity warm-up + worldstate 初始态。之后可 `chapter N+1` 接写。
    """
    from novelist.forge import ForgeState, run_ingest

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    # fake = 演示/测试（纯确定性抽取链路）；真实 provider 走语义抽取 + 记忆初始化
    prov = None if provider == "fake" else _make_cli_provider(provider)
    res = run_ingest(ws, project_id, source, provider=prov,
                     genre=genre_pack,
                     chapters_per_volume=chapters_per_volume or 20,
                     target_words=target_words or 2400,
                     ingest_max_calls=ingest_max_calls,
                     mode=mode, dry_run=dry_run, recursive=recursive)
    for w in res.warnings:
        click.echo(f"  [warn] {w}", err=True)
    if res.quit_early:
        click.echo("切章确认退出：未写库。重跑 `forge ingest` 重新预览。")
        return
    click.echo(f"ingest done: 章={res.chapters_ingested} 卷={res.volumes_encoded} "
               f"人物={res.characters_found} 调用={res.calls_used}")
    if res.downgraded:
        click.echo(f"  [info] 以下章节降级纯确定性抽取: {res.downgraded}", err=True)
    if dry_run:
        return
    if not res.ok:
        raise click.ClickException("ingest 未完成，见上方 warnings")
    click.echo(f"下一步：`novelist chapter {res.volumes_encoded} 1 --dir {project_id}` 接着写（N+1 章）"
               f"或 `novelist forge show {project_id}`")


@forge.command("validate")
@click.argument("directory", required=False, default=None)
@click.option("--smoke", is_flag=True, default=False,
              help="跑 V4 可写冒烟（FakeProvider 真跑 produce_chapter(1,1)，校验 bible 注入与 cast）")
@click.option("--settings-min", type=int, default=5, help="V3 settings 条数下限（缺省 5）")
@click.option("--no-report", is_flag=True, default=False, help="只打印结论，不写报告文件")
@click.pass_context
def forge_validate(ctx: click.Context, directory: str | None, smoke: bool,
                   settings_min: int, no_report: bool) -> None:
    """V1–V6 契约校验 + 报告双写（docs/10 §9，M3l F5）。

    全部确定性规则、零 LLM（--smoke 的 V4 用 FakeProvider，非真实调用）。
    通过（无 block）→ pipeline 推进到「细纲」（只前进）；不通过 → 列出可修条目。
    """
    from novelist.core.pipeline import PipelineStateError, PipelineStateMachine
    from novelist.forge import Blueprint, write_reports
    from novelist.forge.validate import validate_project_full
    from novelist.storage.checkpoint import Checkpoint

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    try:
        Blueprint.load(ws, project_id)
    except FileNotFoundError:
        raise click.ClickException(f"{project_id}: 尚无蓝图——先跑 `forge seed`") from None
    res = validate_project_full(ws, project_id, smoke=smoke, settings_min=settings_min)
    for f_ in res.blocks:
        click.echo(f"  [block] [{f_.code}] {f_.message}", err=True)
    for f_ in res.warns:
        click.echo(f"  [warn] [{f_.code}] {f_.message}", err=True)
    if res.smoke_ran:
        click.echo(f"V4 冒烟: {'通过' if res.smoke_ok else '失败'}（FakeProvider 直出章 1 首章）")
    if not res.ok:
        raise click.ClickException(
            f"校验未通过：{len(res.blocks)} 个阻断项（V2/V3 优先修引用与覆盖度），"
            f"见上方清单。修完重跑 `forge validate`"
        )
    # 通过 → pipeline 推进到「细纲」（只前进；持久化 project.json.pipeline_state）
    ck = Checkpoint(ws)
    try:
        project = ck.restore(project_id)
        st = PipelineStateMachine()
        cur = project.get("pipeline_state") or "立项"
        if cur != st.current:  # 先推进到已存阶段，再尝试推进到细纲
            _advance_to(st, cur)
        st.advance("细纲")
        project["pipeline_state"] = st.current
        ck.save(project_id, project)
    except PipelineStateError as e:
        raise click.ClickException(f"pipeline 推进失败: {e}") from None
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"pipeline 状态保存失败: {e}") from None
    click.echo(f"校验通过：block {len(res.blocks)} / warn {len(res.warns)}"
               f" → pipeline 已推进到「细纲」")
    if not no_report:
        full, stats = write_reports(ws, project_id, smoke=smoke)
        click.echo(f"报告已写：{full}（全量）/ {stats}（摘要）")


@forge.command("rollback")
@click.argument("directory", required=False, default=None)
@click.option("--to", "snap_name", default=None,
              help="回退到指定快照名（缺省最近一次；`forge snapshots` 可列）")
@click.pass_context
def forge_rollback(ctx: click.Context, directory: str | None, snap_name: str | None) -> None:
    """回退到构建前快照：恢复文件 + 删除构建新增产物（docs/10 §7.6，M3l F5）。

    快照由 build/roll 自动前置生成于 workspace/forge/snapshots/。
    """
    from novelist.forge import latest_snapshot, restore_snapshot, snapshots_dir

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    snaps = [p.name for p in sorted(snapshots_dir(ws, project_id).iterdir())] \
        if snapshots_dir(ws, project_id).exists() else []
    if not snaps:
        raise click.ClickException(f"{project_id}: 无可用快照（先跑一次 `forge build` 或 `forge roll`）")
    if snap_name:
        if snap_name not in snaps:
            raise click.ClickException(f"快照不存在：{snap_name}\n可用: {snaps}")
        import shutil
        from pathlib import Path

        snap = Path(snapshots_dir(ws, project_id)) / snap_name
    else:
        snap = latest_snapshot(ws, project_id)
    restored = restore_snapshot(ws, project_id, snap)
    click.echo(f"回滚到 {snap.name}: 恢复 {len(restored)} 个文件。"
               f"可用 `forge validate` 复核后重跑 `forge build --diff`")


@forge.command("snapshots")
@click.argument("directory", required=False, default=None)
@click.pass_context
def forge_snapshots(ctx: click.Context, directory: str | None) -> None:
    """列出快照目录（build/roll 前置自动生成）。"""
    from novelist.forge import snapshots_dir

    ws: Workspace = ctx.obj["workspace"]
    ws, project_id = _resolve_forge_target(ws, directory)
    d = snapshots_dir(ws, project_id)
    if not d.exists() or not any(d.iterdir()):
        click.echo(f"{project_id}: 无快照")
        return
    for p in sorted(d.iterdir()):
        if p.is_dir():
            n = sum(1 for f in p.rglob("*") if f.is_file())
            click.echo(f"  {p.name}（{n} 个文件）")


@cli.command()
@click.option("--host", default="127.0.0.1", help="监听地址")
@click.option("--port", default=8000, type=int, help="监听端口")
@click.pass_context
def server(ctx: click.Context, host: str, port: int) -> None:
    """启动 HTTP 服务（docs/07 §6.2）。"""
    import uvicorn

    uvicorn.run("novelist.server:app", host=host, port=port, reload=False)


@cli.command("settings-pending")
@click.argument("directory", required=False, default=None)
@click.option("--allow", default=None, help="确认入档的术语，逗号分隔（写入 settings.json）")
@click.option("--deny", default=None, help="拒绝的术语，逗号分隔（从 pending 移除）")
@click.pass_context
def settings_pending(ctx: click.Context, directory: str | None, allow: str | None,
                     deny: str | None) -> None:
    """审阅 P0-B 设定待确认队列（bible/settings_pending.json）。

    supplement_settings 闸门把未命中名册的新词拦进 pending（不入档）；
    用 --allow 确认入档 / --deny 拒绝；无参数时列出全部待确认项。
    """
    import json as _json

    ws, project_id = _resolve_project(ctx.obj["workspace"], directory)
    pending_path = ws._abs(f"{project_id}/bible/settings_pending.json")
    settings_path = ws._abs(f"{project_id}/bible/settings.json")
    pending = _json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else []
    pending = pending if isinstance(pending, list) else []

    if not allow and not deny:
        if not pending:
            click.echo("no pending settings")
            return
        for p in pending:
            click.echo(f"  {p.get('term')}  —— {p.get('text')}")
            click.echo(f"    reason: {p.get('reason')}")
        return

    def _split(s: str | None) -> set[str]:
        return {x.strip() for x in (s or "").split(",") if x.strip()}

    allow_set, deny_set = _split(allow), _split(deny)
    settings = _json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else []
    settings = settings if isinstance(settings, list) else []
    kept, moved, dropped = [], 0, 0
    for p in pending:
        term = str(p.get("term") or "")
        if term in allow_set:
            p.pop("reason", None)
            p["id"] = f"setting:jit{len(settings) + 1}"
            settings.append(p)
            moved += 1
        elif term in deny_set:
            dropped += 1
        else:
            kept.append(p)
    if moved:
        settings_path.write_text(_json.dumps(settings, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    if moved or dropped:
        pending_path.write_text(_json.dumps(kept, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    click.echo(f"allow {moved} / deny {dropped} / remaining {len(kept)}")


@cli.command("characters-enrich")
@click.argument("directory", required=False, default=None)
@click.option("--provider", default="fake",
              help="LLM provider：fake/lmstudio/deepseek/openai（默认 fake 防手滑）")
@click.option("--card", default=None, help="只提案指定角色名，逗号分隔（缺省=全部缺料卡）")
@click.pass_context
def characters_enrich(ctx: click.Context, directory: str | None, provider: str,
                      card: str | None) -> None:
    """P0-A 人物数据补喂：为缺 relationships/behavior_rules 的卡跑 LLM 提案。

    提案只进 bible/characters_enrich_pending.json（绝不自动改写 characters.json），
    人工确认走 `novelist enrich-pending --allow/--deny`（settings-pending 同款）。
    提案输入含该卡 character_histories 与涉卡事件——应然设定不与已发生事实冲突。
    """
    ws, project_id = _resolve_project(ctx.obj["workspace"], directory)
    from novelist.core.character_enrich import propose

    cards = {c.get("id"): c for c in _read_chars(ws, project_id)}
    card_ids = None
    if card:
        names = {x.strip() for x in card.split(",") if x.strip()}
        card_ids = {cid for cid, c in cards.items() if c.get("name") in names}
        unknown = names - {c.get("name") for c in cards.values()}
        if unknown:
            click.echo(f"warning: 名册中无这些角色: {sorted(unknown)}")
        if not card_ids:
            raise click.ClickException("no matching characters in roster")
    if provider == "fake":
        raise click.ClickException("--provider fake 仅为默认防手滑；请指定 deepseek/lmstudio/openai")
    llm = _make_cli_provider(provider)
    results = propose(ws, project_id, llm, card_ids=card_ids)
    moved = sum(1 for r in results if r.get("proposed"))
    bad = [r for r in results if not r.get("ok")]
    for r in results:
        tag = "✓" if r.get("ok") else "✗"
        why = f" —— {r['reasons']}" if r.get("reasons") else ""
        click.echo(f"  {tag} {r.get('name')}: {why}")
    click.echo(f"proposed {moved} / rejected {len(bad)} / total {len(results)}")
    if moved:
        click.echo("next: `novelist enrich-pending` 审阅，`--allow 名 --deny 名` 确认/拒绝")


@cli.command("enrich-pending")
@click.argument("directory", required=False, default=None)
@click.option("--allow", default=None, help="确认合并的角色名，逗号分隔（写入 characters.json）")
@click.option("--deny", default=None, help="拒绝的角色名，逗号分隔（从 pending 移除）")
@click.pass_context
def enrich_pending(ctx: click.Context, directory: str | None, allow: str | None,
                   deny: str | None) -> None:
    """审阅 P0-A 人物补喂待确认队列（bible/characters_enrich_pending.json）。

    --allow 确认合并（relationships/behavior_rules 并入卡，打 provenance=enrich），
    --deny 拒绝丢弃；无参数时列出全部待确认项。
    """
    ws, project_id = _resolve_project(ctx.obj["workspace"], directory)
    from novelist.core.character_enrich import apply_pending, list_pending

    if not allow and not deny:
        lines = list_pending(ws, project_id)
        if not lines:
            click.echo("no pending character enrichments")
            return
        for line in lines:
            click.echo(line)
        return

    def _split(s: str | None) -> set[str]:
        return {x.strip() for x in (s or "").split(",") if x.strip()}

    moved, dropped, remaining = apply_pending(
        ws, project_id, allow=_split(allow), deny=_split(deny))
    click.echo(f"allow {moved} / deny {dropped} / remaining {remaining}")


def _read_chars(ws, project_id: str) -> list[dict]:
    """读 bible/characters.json（enrich 命令用，避免在 cli 顶层重复 import json）。"""
    import json as _json

    p = ws._abs(f"{project_id}/bible/characters.json")
    if not p.exists():
        return []
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


@cli.group()
def docx() -> None:
    """Markdown ⇄ Word(.docx) 互转（M3o，零第三方依赖）。

    常用链路（无需单独转档）：
    - 输入系统：`novelist forge ingest book.docx`（自动转 md → 切章 → 建档接写）
    - 成稿交付：`novelist export --format docx --output book.docx`
    也可单独互转：`novelist docx to-md / to-docx`。
    """


@docx.command("to-md")
@click.argument("src", type=click.Path(exists=True, dir_okay=False))
@click.option("--output", "out", default=None, help="输出 .md 路径（缺省与源文件同目录同名）")
@click.option("--media-dir", default=None, help="图片导出目录（缺省 <源文件名>_media/）")
@click.option("--no-media", is_flag=True, default=False, help="不导出图片")
def docx_to_md(src: str, out: str | None, media_dir: str | None, no_media: bool) -> None:
    """Word(.docx) → Markdown：标题级别/粗斜体/表格/列表/链接均保留。"""
    import pathlib

    from novelist.core.docxconv import DocxConvError, docx_to_markdown

    p = pathlib.Path(src)
    dst = pathlib.Path(out) if out else p.with_suffix(".md")
    try:
        text = docx_to_markdown(p, media_dir=media_dir, extract_media=not no_media)
    except DocxConvError as e:
        raise click.ClickException(str(e)) from e
    if dst.parent and str(dst.parent) not in ("", "."):
        dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")
    click.echo(f"{p.name} → {dst}（{len(text)} 字符）")
    click.echo(f"接着写：`novelist forge ingest {dst.parent}` 或把该 md 拷入稿子目录")


@docx.command("to-docx")
@click.argument("src", type=click.Path(exists=True, dir_okay=False))
@click.option("--output", "out", default=None, help="输出 .docx 路径（缺省与源文件同目录同名）")
@click.option("--title", default=None, help="书名（缺省取首个 H1 作书名页）")
@click.option("--font-body", default="宋体", help="正文中文字体")
@click.option("--font-heading", default="黑体", help="标题中文字体")
@click.option("--no-indent", is_flag=True, default=False, help="关闭正文首行缩进两字符")
@click.option("--no-page-break", is_flag=True, default=False, help="章首不另起一页")
def docx_from_md(src: str, out: str | None, title: str | None, font_body: str,
                 font_heading: str, no_indent: bool, no_page_break: bool) -> None:
    """Markdown → Word(.docx)：宋体小四 / 1.5 倍行距 / 首行缩进 / 章首分页。"""
    import pathlib

    from novelist.core.docxconv import DocxConvError, markdown_to_docx

    p = pathlib.Path(src)
    dst = pathlib.Path(out) if out else p.with_suffix(".docx")
    try:
        markdown_to_docx(p.read_text(encoding="utf-8"), dst, title=title,
                         font_body=font_body, font_heading=font_heading,
                         indent=not no_indent,
                         chapter_page_break=not no_page_break)
    except DocxConvError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"{p.name} → {dst}（{dst.stat().st_size} 字节）")


def _resolve_project(ws: Workspace, directory: str | None) -> tuple[Workspace, str]:
    """统一项目定位（D7，与 forge _resolve_forge_target 同语义）。

    directory 可直指项目目录（含 project.json）或工作区根；缺省取根下唯一项目。
    返回 (可能重绑定到项目父目录的 ws, project_id)——直指项目目录时 ws 必须重绑，
    否则后续 `ws._abs(f"{project_id}/...")` 会路径翻倍。
    """
    import pathlib

    if directory:
        ws = Workspace(root=directory)
    root = pathlib.Path(ws._abs(""))
    if (root / "project.json").exists():
        return Workspace(root=str(root.parent)), root.name
    return ws, _locate_project(root)


def _locate_project(root) -> str:
    """在根下定位项目 id（首层子目录中含 project.json 的；多个/零个报错并提示直传目录）。"""
    import pathlib

    rp = pathlib.Path(root)
    found = sorted(d.name for d in rp.iterdir() if d.is_dir() and (d / "project.json").exists())
    if not found:
        raise click.ClickException("no project found; run `novelist init` first")
    if len(found) > 1:
        raise click.ClickException(
            f"multiple projects found: {found}; target one explicitly（目录参数直传项目目录即可）")
    return found[0]


def main() -> None:
    try:
        cli(obj={})
    except NovelistError as e:
        raise click.ClickException(f"{e.code}: {e.message}") from e


if __name__ == "__main__":
    main()
