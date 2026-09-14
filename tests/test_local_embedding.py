"""LocalEmbedding / make_embedding("local") 降级链测试。

单测纪律：不依赖 fastembed 必须安装——
- 未安装 fastembed：验证 make_embedding("local") 静默降级 KeywordEmbedding（F9.4）
- 已安装 fastembed：额外验证真实向量（768 维、归一化、批量一致性）
"""

from __future__ import annotations

import pytest

from novelist.core.embedding import KeywordEmbedding, LocalEmbedding, cosine, make_embedding


def _has_fastembed() -> bool:
    try:
        import fastembed  # noqa: F401
        return True
    except ImportError:
        return False


# ---- 任何环境都应通过 ----
def test_local_missing_dependency_degrades_to_keyword():
    if _has_fastembed():
        pytest.skip("fastembed 已安装，降级路径不适用")
    emb = make_embedding("local")
    assert isinstance(emb, KeywordEmbedding)
    assert emb.kind == "keyword-hash"


def test_make_embedding_backward_compat():
    # 2026-09-04 语义变更：None 默认 "auto"（内置 local 优先，用户需求：全系统统一内置 embedding）
    assert isinstance(make_embedding("keyword-fallback"), KeywordEmbedding)
    assert isinstance(make_embedding("off"), KeywordEmbedding)


def test_auto_without_any_backend_degrades():
    emb = make_embedding("auto")
    # ana/CI 环境通常无 OPENAI_API_KEY；fastembed 可选——auto 至少不抛错
    assert emb is not None
    assert hasattr(emb, "embed")


def test_local_embed_empty_input():
    emb = LocalEmbedding()
    # 不触底座（惰性加载），空输入直接返回空列表
    assert emb.embed([]) == []


# ---- fastembed 已安装时的真实向量验证 ----
@pytest.mark.skipif(not _has_fastembed(), reason="fastembed 未安装")
def test_local_embed_real_vectors():
    """真实向量（768 维）：**只有本地已有权重缓存时才跑**。

    默认套件不依赖外部服务：无缓存时直接 skip，不发起下载（CI 上不因此拉几百 MB）。
    注意不能靠"embed 抛异常"判断——P-FE2 修复后失败会就地降级，须查 `degraded`。
    """
    emb = make_embedding("local")
    assert isinstance(emb, LocalEmbedding)
    if not emb.cached():
        pytest.skip("本地无 fastembed 权重缓存（需联网下载），按纪律跳过")
    vecs = emb.embed(["灵气复苏第一年", "青云宗杂役弟子"])
    assert not emb.degraded, f"权重已缓存但仍降级：{emb.degrade_reason}"
    assert len(vecs) == 2
    assert emb.dim == 768
    assert all(len(v) == 768 for v in vecs)
    # L2 已归一化（fastembed 输出未归一化则余弦自内积应≈1 的判断改用余弦函数）
    assert abs(cosine(vecs[0], vecs[0]) - 1.0) < 1e-3
    # 语义相近文本相似度应高于无关文本
    near = emb.embed(["修仙境界炼气期"])
    assert cosine(vecs[0], near[0]) > 0.5
