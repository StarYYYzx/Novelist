"""审核拦截降级与敏感词预检（docs/04 §5.10 / docs/07 §2.6，ADR-015）。

上游预检降低厂商侧拦截概率；识别拦截后走改写→切换→人工链路（编排层负责状态）。
"""

from __future__ import annotations


class ModerationPrechecker:
    """本地敏感词预检（可插拔词表）。"""

    def __init__(self, banned: list[str] | None = None) -> None:
        # 默认空表；生产环境加载配置/词表文件
        self._banned = banned or []

    def scan(self, text: str) -> list[str]:
        """返回命中的敏感词列表；空列表表示通过预检。"""
        return [w for w in self._banned if w and w in text]

    def predicate_hit(self, text: str) -> bool:
        return bool(self.scan(text))
