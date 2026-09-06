"""API Key 隐私工具：遮蔽 / 环境变量注入 / 错误日志脱敏。

原则（创作者历史经验）：
- **Key 永不落配置文件 / 工作区 / 蓝图**（避免被 git 跟踪或随 book 分发）。
  一律由**环境变量**或**运行期显式参数**注入。
- 任何出现在日志 / 错误体里的 key 都要被遮蔽，防止随远端上传或报错泄漏。
- 官方 `api-docs.deepseek.com` 推荐用环境变量持有 key，而非写死在脚本里。
"""

from __future__ import annotations


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
    """解析 key：显式参数优先，其次逐个查环境变量，全部缺失返回 None。

    env_names 按优先级排列（如 ["DEEPSEEK_API_KEY", "DeepSeek-API-KEY"]）。
    """
    if explicit:
        return explicit
    for name in env_names:
        if name in _os_environ():
            return _os_environ()[name]
    return None


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