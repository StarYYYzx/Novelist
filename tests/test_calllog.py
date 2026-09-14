"""原始 LLM 调用日志（core/calllog）+ openai.complete 拦截（2026-09-14）测试。

文档：docs/ (calllog) ；约定：单测绝不真调 LLM（docs/09 §2.1）——httpx 用桩替身。
覆盖：
- calllog：未启用时 record no-op；enable/disable；call_context 嵌套 / 清栈
- 记录内容：完整 payload / 原始 raw_text / 解析 result / status / 耗时
- 异常路径：HTTP 非 200 也记录 exception
- 拦截点：provider.complete 统一记录；call_context 标签进记录 ctx
- 批次 3（2026-09-15）加固：脱敏双保险接线 / 去重复字段 / 关闭开关 / 容量上限 /
  ts 与文件名同用时区 / 目录自愈 / `sys.exc_info()` 污染
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from novelist.core import calllog
from novelist.core.llm import LLMMessage, LLMRequest
from novelist.providers import openai as oa


@pytest.fixture
def calldir(tmp_path):
    calllog.disable_calllog()
    calllog.enable_calllog(tmp_path / "raw-calls")
    yield tmp_path / "raw-calls"
    calllog.disable_calllog()


def _read(calldir):
    files = list(calldir.glob("*.jsonl"))
    assert len(files) == 1, f"expected 1 file, got {[f.name for f in files]}"
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines() if line]


def _req(content: str = "写一章"):
    return LLMRequest(
        messages=[LLMMessage(role="system", content="你是编辑"),
                  LLMMessage(role="user", content=content)],
        temperature=0.5,
        max_tokens_out=400,
        response_format="json_object",
        thinking=False,
    )


def _stub_httpx(monkeypatch, status=200, body=None):
    body = body or {
        "choices": [{"message": {"content": "正文内容", "reasoning_content": "思考过程"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 4,
                  "prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 7},
    }

    class _Resp:
        status_code = status
        text = "" if status == 451 else "🔥not-in-json"  # 451 无 body
        _json = body

        def json(self):
            return self._json

    monkeypatch.setattr(oa, "httpx", SimpleNamespace(post=lambda *a, **k: _Resp()))
    return _Resp()


# ---------- calllog 基础 ----------

def test_record_is_noop_when_disabled():
    calllog.disable_calllog()
    assert calllog.record({"a": 1}) is None
    assert calllog.is_enabled() is False


def test_enable_create_dir_and_ctx_empty(calldir):
    assert calldir.exists()
    assert calllog.is_enabled()
    assert calllog.current_ctx() == ""


def test_call_context_nesting(calldir):
    assert calllog.current_ctx() == ""
    with calllog.call_context("chapter v1-ch1"):
        assert calllog.current_ctx() == "chapter v1-ch1"
        with calllog.call_context("forge:character"):
            assert calllog.current_ctx() == "chapter v1-ch1 / forge:character"
    assert calllog.current_ctx() == ""


def test_record_writes_jsonl_with_ctx(calldir):
    with calllog.call_context("a"):
        with calllog.call_context("b"):
            p = calllog.record({"x": 1})
    assert p is not None and p.exists()
    (file,) = list(calldir.glob("*.jsonl"))
    rec = json.loads(file.read_text(encoding="utf-8"))
    assert rec["x"] == 1
    assert rec["ctx"] == "a / b"
    assert "ts" in rec


def test_redact_secrets():
    secret = "sk-foobar1234567"
    out = calllog.redact_secrets(f"key={secret}")
    assert secret not in out
    assert "<redacted>" in out
    assert calllog.redact_secrets("明文") == "明文"


# ---------- 批次 3 加固（D4/D5/D7/U8） ----------

def test_record_redacts_bottom_layer(calldir):
    """D4#2：`redact_secrets` 必须真的接在生产路径上（原先 src 下零调用）。"""
    calllog.record({"text": "key=sk-abcdefgh1234567 尾巴"})
    raw = list(calldir.glob("*.jsonl"))[0].read_text(encoding="utf-8")
    assert "sk-abcdefgh1234567" not in raw
    assert "<redacted>" in raw


def test_record_recreates_missing_dir(calldir):
    """D4 细节：运行中目录被移走 → 自愈重建，而不是静默停止记录。"""
    moved = calldir.parent / "raw-calls-moved"
    calldir.rename(moved)
    assert not calldir.exists()
    assert calllog.record({"x": 1}) is not None
    assert calldir.exists()


def test_ts_matches_filename_date(calldir):
    """D4 细节：`ts` 与文件名同用本地时区（原 ts 用 UTC——北京时间 00:00–08:00 会差一天）。"""
    calllog.record({"x": 1})
    p = next(calldir.glob("*.jsonl"))
    rec = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert rec["ts"][:10] == p.stem, f"ts={rec['ts']} 与文件名 {p.stem} 日期不一致"


def test_quota_stops_recording_with_marker(calldir, monkeypatch):
    """D5：超 `NOVELIST_CALLLOG_MAX_MB` 后停止记录，并留一条终态标记行（不静默）。"""
    monkeypatch.setenv("NOVELIST_CALLLOG_MAX_MB", "1")
    assert calllog.max_bytes() == 1024 * 1024
    today = calldir / f"{time.strftime('%Y-%m-%d')}.jsonl"
    today.write_text("x" * (1024 * 1024), encoding="utf-8")

    assert calllog.record({"x": 1}) is None
    lines = [ln for ln in today.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert json.loads(lines[-1])["calllog"] == "quota_exceeded"

    # 标记只写一次（不刷屏）
    assert calllog.record({"x": 2}) is None
    lines = [ln for ln in today.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert sum(1 for ln in lines if '"quota_exceeded"' in ln) == 1


def test_append_heals_truncated_tail(calldir):
    """健壮性：上一次写入被中断（文件无结尾换行）时，新记录须另起一行，不粘在残行上。

    否则整行 JSON 解析失败、该日日志后续全部不可读（手工 kill / 磁盘满都会留下这种尾行）。
    """
    today = calldir / f"{time.strftime('%Y-%m-%d')}.jsonl"
    today.write_text('{"ts": "半行被中断', encoding="utf-8")  # 无结尾换行
    calllog.record({"hello": "world"})
    lines = [ln for ln in today.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    assert json.loads(lines[1])["hello"] == "world"  # 第二行仍是完整可解析的 JSON


def test_env_off_blocks_auto_enable(calldir, monkeypatch):
    """U8/D4#1：`NOVELIST_CALLLOG=0` 禁止**自动**开启（显式 enable 仍优先，同 key 口径）。"""
    monkeypatch.setenv("NOVELIST_CALLLOG", "0")
    # 已开启态下也不"续开"（返回 False 表示不允许自动开启）
    assert calllog.ensure_enabled() is False
    # 模拟未开启（绕过 _USER_DISABLED，隔离环境变量的作用）
    monkeypatch.setattr(calllog, "_DIR", None)
    assert calllog.ensure_enabled() is False
    assert calllog.is_enabled() is False


def test_disable_calllog_is_sticky(calldir, monkeypatch):
    """U8/D4#1：`disable_calllog()` 之后**不得**被下一次真实调用自动开启（原先关不掉）。"""
    calllog.disable_calllog()
    _stub_httpx(monkeypatch)
    p = oa.OpenAICompatibleProvider(api_key="k-test-12345678", base_url="https://x/v1")
    p.complete(_req())
    assert calllog.is_enabled() is False
    assert not list(calldir.glob("*.jsonl"))


# ---------- openai.complete 拦截 ----------

def test_complete_records_full_snapshot(calldir, monkeypatch):
    _stub_httpx(monkeypatch)
    p = oa.OpenAICompatibleProvider(api_key="k-test-12345678", base_url="https://x/v1", model="m1")
    with calllog.call_context("chapter v1-ch1"):
        res = p.complete(_req())

    rec = _read(calldir)[0]
    assert res.content == "正文内容"
    # ctx：外层调用栈标签
    assert rec["ctx"] == "chapter v1-ch1"
    # 完整 prompt：以 payload.messages（实际请求体）为**唯一**存放处（D4#4 去重复）
    assert rec["payload"]["messages"][0] == {"role": "system", "content": "你是编辑"}
    assert rec["payload"]["messages"][1]["content"] == "写一章"
    # request 只留结构摘要（同一份 prompt 不再存两遍）
    assert rec["request"]["messages_digest"] == [{"role": "system", "chars": 4},
                                                 {"role": "user", "chars": 3}]
    assert "messages" not in rec["request"]
    # 实际请求体
    assert rec["payload"]["temperature"] == 0.5
    assert rec["payload"]["response_format"] == {"type": "json_object"}
    # 原始响应文本 + 解析结果
    assert rec["response"]["raw_text"] == "🔥not-in-json"
    assert rec["response"]["result"]["content"] == "正文内容"
    assert rec["response"]["result"]["reasoning"] == "思考过程"
    # 元数据
    assert rec["status_code"] == 200
    assert rec["provider"] == "OpenAICompatibleProvider"
    assert rec["model"] == "m1"
    assert rec["elapsed_ms"] >= 0
    assert rec["exception"] is None


def test_payload_redacted_with_configured_key(calldir, monkeypatch):
    """D4#3：payload 也必须脱敏（ADR-035 §D）——原先只有 raw_text / exception 走了脱敏。

    用**不含 `sk-` 前缀**的 key，确保只有"配置值脱敏"这一层能拦住（正则兜底拦不到）。
    """
    secret = "k-test-12345678"
    _stub_httpx(monkeypatch)
    p = oa.OpenAICompatibleProvider(api_key=secret, base_url="https://x/v1", model="m1")
    p.complete(_req(content=f"我的凭证是 {secret}，继续写"))

    raw = next(calldir.glob("*.jsonl")).read_text(encoding="utf-8")
    assert secret not in raw, "payload 里的密钥未脱敏"
    assert "…5678" in raw  # mask_secret 形态


def test_exception_not_contaminated_by_outer_handler(calldir, monkeypatch):
    """D7：在 `except` 块里调用（成功）不得被记上外层那条假异常。

    `sys.exc_info()` 的语义是"当前正在处理的异常"——F14.1 的"改写重试 / 切换 Provider"
    正是"在 except 里再调 complete"的形态，届时成功的调用会被记上假 exception。
    """
    _stub_httpx(monkeypatch)
    p = oa.OpenAICompatibleProvider(api_key="k-test-12345678", base_url="https://x/v1")
    try:
        raise ValueError("outer")
    except ValueError:
        res = p.complete(_req())  # 这里成功

    assert res.content == "正文内容"
    assert _read(calldir)[0]["exception"] is None


def test_complete_records_error_path(calldir, monkeypatch):
    _stub_httpx(monkeypatch, status=500, body={})
    p = oa.OpenAICompatibleProvider(api_key="k-test-12345678", base_url="https://x/v1")
    from novelist.core.errors import ProviderError

    with pytest.raises(ProviderError):
        p.complete(_req())

    rec = _read(calldir)[0]
    assert rec["status_code"] == 500
    assert rec["response"]["result"] is None
    assert rec["exception"]["type"] == "ProviderError"
    assert "openai http 500" in rec["exception"]["message"]


def test_complete_blocked_records_status_451(calldir, monkeypatch):
    _stub_httpx(monkeypatch, status=451)
    p = oa.OpenAICompatibleProvider(api_key="k-test-12345678", base_url="https://x/v1")
    res = p.complete(_req())
    assert res.blocked
    rec = _read(calldir)[0]
    assert rec["status_code"] == 451
    assert rec["response"]["result"]["blocked"] is True
