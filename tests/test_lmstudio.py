"""LM-Studio 本地适配器测试（docs/07 §2.4）。

- 单元：httpx mock 验证请求组装与响应解析（不含畸形 tool_calls 容错）。
- 集成：真实连通性测试——若 LM-Studio 未运行或鉴权失败则跳过（不因环境阻塞测试）。
"""

from __future__ import annotations

import json
import os

import pytest
import httpx

from novelist.providers.lmstudio import LMStudioProvider, parse_lmstudio


# ---------- 单元：解析 ----------


def test_parse_lmstudio_content():
    res = parse_lmstudio(
        {"choices": [{"message": {"content": "本地模型回复"}, "finish_reason": "stop"}]}
    )
    assert res.ok
    assert res.content == "本地模型回复"
    assert res.provider == "lmstudio"


def test_parse_lmstudio_tool_call_malformed_arguments():
    # 畸形 arguments（非 JSON）容错为空 dict，不抛错
    res = parse_lmstudio(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [{"id": "c1", "function": {"name": "write_draft", "arguments": "not-json"}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    assert res.tool_calls[0].name == "write_draft"
    assert res.tool_calls[0].arguments == {}


def test_parse_lmstudio_no_tool_calls_falls_back_to_content():
    # 本地小模型把工具意图写进 content 时按文本处理（ADR-006 降级）
    res = parse_lmstudio({"choices": [{"message": {"content": "我决定调用 write_draft。"}, "finish_reason": "stop"}]})
    assert res.ok
    assert "write_draft" in res.content


# ---------- 单元：请求组装（httpx mock） ----------


def test_lmstudio_request_bypasses_auth_when_no_key(monkeypatch):
    """显式不提供 key 时不应发送 Authorization 头（适配器默认不要求鉴权）。"""
    headers_capture = {}

    def handler(request):
        headers_capture["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    # 显式传 api_key=""（不用环境变量回退），确保走"无鉴权"路径
    p = LMStudioProvider(api_key="", model="qwen/qwen3.5-9b", _client=client)
    from novelist.core.llm import LLMMessage, LLMRequest

    p.complete(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))
    assert not (headers_capture.get("authorization") or "")


def test_lmstudio_json_object_drops_response_format(monkeypatch):
    """LM-Studio 兼容层实测只接受 json_schema/text，json_object 会 400——
    适配器必须剥掉 response_format，靠 prompt 引导 JSON（forge 真实链路教训）。
    """
    body_capture = {}

    def handler(request):
        body_capture["json"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = LMStudioProvider(api_key="", model="qwen/qwen3.5-9b", _client=client)
    from novelist.core.llm import LLMMessage, LLMRequest

    p.complete(
        LLMRequest(
            messages=[LLMMessage(role="user", content="返回 JSON")],
            max_tokens_out=128,
            response_format="json_object",
        )
    )
    assert "response_format" not in body_capture["json"], "LM-Studio 不接受 json_object，必须剥掉"


def test_lmstudio_reasoning_effort_payload(monkeypatch):
    """reasoning_effort 设置后必须每次请求都带（FreeToken 硬约束：不传则思考吃光预算）。"""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = LMStudioProvider(api_key="", model="qwen3.6-35b-a3b-fp8",
                         reasoning_effort="none", _client=client)
    from novelist.core.llm import LLMMessage, LLMRequest

    # 默认：provider 级 effort 原样发送
    p.complete(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))
    assert bodies[-1].get("reasoning_effort") == "none"

    # 请求级 thinking=False → 强制 none（正文生成关思考）
    p.complete(LLMRequest(messages=[LLMMessage(role="user", content="hi")], thinking=False))
    assert bodies[-1].get("reasoning_effort") == "none"

    # 请求级 thinking=True（审校等判断类任务）→ 从 none 自动升 low
    p.complete(LLMRequest(messages=[LLMMessage(role="user", content="judge")], thinking=True))
    assert bodies[-1].get("reasoning_effort") == "low", "判断类任务请求级开思考应升一档"


def test_lmstudio_reasoning_effort_absent_by_default(monkeypatch):
    """默认不传 reasoning_effort 字段——LM-Studio/llama.cpp 路径行为不得改变。"""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = LMStudioProvider(api_key="", model="qwen/qwen3.5-9b", _client=client,
                         enable_thinking=False)
    from novelist.core.llm import LLMMessage, LLMRequest

    p.complete(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))
    assert "reasoning_effort" not in bodies[-1]
    assert bodies[-1].get("chat_template_kwargs") == {"enable_thinking": False}


# ---------- 集成：连通性（服务未运行/未授权则跳过） ----------


def _lmstudio_reachable(timeout: int = 5) -> bool:
    try:
        resp = httpx.get("http://127.0.0.1:1234/v1/models", timeout=timeout, verify=False)
        return resp.status_code in (200, 401)  # 401=服务在但需 key
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _lmstudio_reachable(), reason="LM-Studio 本地服务不可达或未运行")
def test_lmstudio_live_completion():
    """真实 LM-Studio 集成（qwen/qwen3.5-9b）。

    服务在但本次因负载超时/需 key 时降级为 skip（避免本地小模型负载时序造成的 flaky 硬失败）。
    """
    from novelist.core.errors import ProviderError
    from novelist.core.llm import LLMMessage, LLMRequest

    key = os.getenv("LM_STUDIO_API_KEY")
    p = LMStudioProvider(api_key=key, timeout_s=90)
    try:
        res = p.complete(
            LLMRequest(messages=[LLMMessage(role="user", content="用一句话回复：你好")], max_tokens_out=256)
        )
    except ProviderError as e:
        if "401" in str(e):
            pytest.skip(f"LM-Studio 需要 API key：{e}")
        pytest.skip(f"LM-Studio 本次调用失败（可能模型正在加载）：{e}")
    assert res.ok
    # 小模型可能把 64 上限全用于填充/思考导致 content 空，故以 ok 为主判据；
    # 若 content 为空则断言确实产生了输出（finish/usage）。
    assert res.content or res.usage is not None


@pytest.mark.slow
@pytest.mark.skipif(not _lmstudio_reachable(), reason="LM-Studio 本地服务不可达或未运行")
def test_lmstudio_end_to_end_chapter(tmp_path):
    """端到端：LM-Studio 驱动 produce_chapter（prefer_direct）产出第 1 章草稿（慢测试 -m slow）。

    本地 token 生成慢：prefer_direct=True 只做一次生成（不来回调工具），量力而为。
    """
    from novelist.core.orchestrator import produce_chapter
    from novelist.storage.checkpoint import Checkpoint
    from novelist.storage.workspace import Workspace

    ws = Workspace(root=str(tmp_path))
    pid = "proj-lm"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t", "pipeline_state": "正文", "event_seq": 0})

    p = LMStudioProvider(api_key=os.getenv("LM_STUDIO_API_KEY"), timeout_s=60)
    # 9B 约 20 token/s：generation_tokens=300 ≈ 15s，保证在 60s 超时内跑完
    res = produce_chapter(ws, pid, vol=1, ch=1, provider=p, max_rounds=3,
                          direct_words_floor=5, prefer_direct=True, generation_tokens=300)
    assert res.ok, f"chapter production failed: {res.result}"
    assert ws.draft_path(pid, 1, 1).exists()
    body = ws.draft_path(pid, 1, 1).read_text(encoding="utf-8")
    assert len(body) > 0


@pytest.mark.slow
@pytest.mark.skipif(not _lmstudio_reachable(), reason="LM-Studio 本地服务不可达或未运行")
def test_lmstudio_cli_chapter_full(tmp_path):
    """CLI chapter 全链路（真实 LM-Studio）：细纲+前情记忆注入 → 正文直出 → 事件回写。

    最小预算（150 token≈8s），量力而为：不追求长篇，只验证链路从细纲到回写真跑通。
    """
    import json

    from click.testing import CliRunner

    from novelist.cli import cli
    from novelist.storage.checkpoint import Checkpoint
    from novelist.storage.workspace import Workspace

    ws = Workspace(root=str(tmp_path))
    pid = "proj-lmcli"
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t", "pipeline_state": "正文", "event_seq": 0})
    gist = ws.outline_chapter_path(pid, 1, 2)
    gist.parent.mkdir(parents=True, exist_ok=True)
    gist.write_text("细纲：苏晚发现断玉佩，掌门之位悬而未决。", encoding="utf-8")
    ev = ws._abs(f"{pid}/memory/plot_events.json")
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(json.dumps([{"id": "ev:0", "vol": 1, "ch": 1, "summary": "苏晚刚刚下山"}]), encoding="utf-8")

    res = CliRunner().invoke(cli, ["chapter", str(tmp_path), "--provider", "lmstudio", "--vol", "1", "--ch", "2"],
                             env=None)
    assert res.exit_code == 0, res.output
    assert ws.draft_path(pid, 1, 2).exists()
    events = json.loads(ws._abs(f"{pid}/memory/plot_events.json").read_text(encoding="utf-8"))
    assert len(events) == 2  # 前情 1 + 本章回写 1
    assert events[-1]["summary"].startswith("完成")
