"""API Key 隐私工具：遮蔽 / 环境变量注入 / `.env` 加载 / 错误日志脱敏。

原则（创作者历史经验，2026-09-07 修订）：
- **Key 永不落 git 跟踪的文件**（避免随 book 分发 / 提交泄漏）。一律由**环境变量**、
  **本机 `.env` 文件**（gitignored）或**运行期显式参数**注入；CLI 显式参数 > 环境变量 > `.env`。
- **`.env` 是可选的 key 存放点**（方案 A）：集中管理多个服务商 key，仍保持不入库。
  缺省搜索工作区根 `.env`（默认 `novel_workspace/.env`）与当前目录 `.env`。
- 任何出现在日志 / 错误体里的 key 都要被遮蔽，防止随远端上传或报错泄漏。
- 官方 `api-docs.deepseek.com` 推荐用环境变量持有 key，而非写死在脚本里。
"""

from __future__ import annotations

import os
from pathlib import Path


def mask_secret(secret: str) -> str:
    """遮蔽密钥：仅保留后 4 位，其余以星号替代。

    - 空串/空值 -> "(empty)"；
    - 过短（<8 位）-> "***"（大概率不是真 key，整段隐藏更安全）；
    - 其余 -> "sk-…-abcd" 风格。
    """
    if not secret:
        return "(empty)"
    if len(secret) < 8:
        return "***"
    return "…" + secret[-4:]


def resolve_api_key(explicit: str | None, env_names: list[str]) -> str | None:
    """解析 key：显式参数优先，其次逐个查环境变量/.env，全部缺失返回 None。

    env_names 按优先级排列（如 ["DEEPSEEK_API_KEY", "DeepSeek-API-KEY"]）。
    环境变量注入包括 `.env` 文件加载后的条目（见 load_env_files）。
    """
    if explicit:
        return explicit
    for name in env_names:
        if name in _os_environ():
            return _os_environ()[name]
    return None


_ENV_LOADED_STACK: list[str] = []  # 已加载过的 .env 绝对路径（幂等防重复注入）


def load_env_files(paths: list[str] | None = None) -> str | None:
    """加载 `.env` 文件，把 `KEY=VAL` 条目注入 os.environ（不覆盖已存在的变量）。

    - 缺省搜索：工作区根 `novel_workspace/.env`、当前目录 `.env`（后者更靠近进程 CWD）。
    - 幂等：同一 .env 只注入一次；已存在的环境变量优先（真实 shell 变量 > .env）。
    - 永不覆盖已存在的值（CLI 显式参数优先于 .env）。

    返回实际加载的 .env 绝对路径；一个都没找到返回 None。
    """
    candidates = paths or [
        os.path.join(os.getcwd(), "novel_workspace", ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    loaded: str | None = None
    for p in candidates:
        p = os.path.abspath(p)
        if p in _ENV_LOADED_STACK:
            continue
        _ENV_LOADED_STACK.append(p)
        path = Path(p)
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        loaded = p
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in _os_environ():
                _os_environ()[k] = v
    return loaded


def redact_message(text: str, secrets: list[str]) -> str:
    """把文本中出现的密钥子串替换为遮蔽形式，用于错误体 / 日志脱敏。

    secrets 可为空；只遮蔽长度 >= 8 的串，避免误伤 URL/命令等通用子串。
    """
    out = text
    for s in secrets or []:
        if s and len(s) >= 8 and s in out:
            out = out.replace(s, mask_secret(s))
    return out


def _os_environ() -> dict[str, str]:
    import os

    return os.environ