"""共享测试夹具（docs/11 §13 P1-5 整改：19 个测试文件各自手搓夹具的问题）。

M3m 起的新测试统一从这里取 `ws_factory` / `write_json` / `stub_llm`；
存量测试文件（test_m0/m1/m2/m3_*/m4/m5/m6/m7/m8/m9…）各自的
`_project` / `_write` / `_seed` / `_StubLLM` 是历史副本，按批迁移、不一次性重构
（渐进整改原则，docs/11 §13）。

单元测试**绝不真调 LLM**（docs/09 §2.1）：`stub_llm` 返回固定文本，
需要更完整替身（blocked/json 模式）用 `providers/fake.FakeProvider`。
"""

from __future__ import annotations

import pytest

from novelist.core.llm import LLMResult
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace

# 宿主环境可能注入的密钥变量（覆盖 providers/__init__.py 的 PRESETS key_env）。
_SECRET_ENV_VARS = (
    "DEEPSEEK_API_KEY",
    "DeepSeek-API-KEY",  # DeepSeek 旧名（仍在 key_env 优先级列表内）
    "OPENAI_API_KEY",
    "DASHSCOPE_API_KEY", "QWEN_API_KEY",
    "MOONSHOT_API_KEY", "KIMI_API_KEY",
    "ZHIPU_API_KEY", "GLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "OLLAMA_API_KEY",
    "VLLM_API_KEY",
    "LM_STUDIO_API_KEY",  # 已停用通道（LM-Studio）的宿主残留
)


@pytest.fixture(autouse=True)
def _isolate_secret_env(monkeypatch):
    """隔离宿主密钥环境变量（2026-09-12）。

    宿主 shell 若注入了 ``DEEPSEEK_API_KEY`` 等，会让「缺 key 应抛错」与「key 优先级」
    两类测试静默失真（实测 2 failed：test_m2.py::test_deepseek_requires_key /
    test_providers_deepseek.py::test_provider_key_precedence）。此处统一清除，
    使测试结果不依赖运行环境；确需 key 的测试仍可用 ``monkeypatch.setenv`` 自行注入。
    """
    for name in _SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def ws_factory(tmp_path):
    """返回 `make(pid="proj-test") -> (ws, pid)`：建好项目骨架的工作区。

    每个测试拿到独立的 tmp_path，互不污染；`Checkpoint.save` 写入 project.json
    骨架（pipeline_state=立项），与存量测试的 `_project` 行为一致。
    """

    def _make(pid: str = "proj-test"):
        ws = Workspace(root=str(tmp_path))
        ws.create_project(pid)
        Checkpoint(ws).save(pid, {
            "id": pid, "title": "测试书名", "pipeline_state": "立项", "event_seq": 0,
        })
        return ws, pid

    return _make


@pytest.fixture
def write_json():
    """返回 `write(ws, pid, rel, data)`：按项目相对路径写 json（原子写）。"""

    def _write(ws, pid: str, rel: str, data):
        ws.write_json(ws._abs(f"{pid}/{rel}"), data)  # noqa: SLF001 - 存量接口，docs/11 §13 P0-3

    return _write


class StubLLM:
    """极简同步替身：`complete` 返回固定文本（记录调用内容）。

    - `reasoning=True` 时附带 reasoning_content，模拟思考型模型；
    - `calls` 记录每次请求的最后一条 user 消息，供断言 prompt 内容。
    """

    def __init__(self, text: str = "", *, reasoning: bool = False) -> None:
        self.text = text
        self.reasoning = reasoning
        self.calls: list[str] = []

    def complete(self, req) -> LLMResult:
        content = req.messages[-1].content if req.messages else ""
        self.calls.append(content)
        return LLMResult(
            ok=True,
            content=self.text,
            finish_reason="stop",
            blocked=False,
            reasoning="（思考）" * 60 if self.reasoning else "",
            reasoning_tokens=120 if self.reasoning else None,
        )


@pytest.fixture
def stub_llm():
    return StubLLM
