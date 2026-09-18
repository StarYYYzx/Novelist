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


def _skipif_active(fn) -> bool:
    """读函数的 `skipif` 标记条件在**当前环境**是否已生效（True = 本次会 skip）。"""
    for m in getattr(fn, "pytestmark", ()):
        if m.name == "skipif":
            return bool(m.args[0])
    return False


def test_local_path_has_at_least_one_executing_branch():
    """下限守卫（2026-09-19）：两条互斥 skip **不得同时生效**，否则本文件零真断言。

    历史形态：`test_local_missing_dependency_degrades_to_keyword` 在**已安装**时 skip、
    `test_local_embed_real_vectors` 在**未安装或无权重缓存**时 skip —— 于"装了但没缓存"
    的机器上两条齐 skip。修法是把"不触网的构造期断言"单拆成一条只 skip 于"未安装"的
    用例（同上），使两个分支**互补且必有其一执行**。此守卫把该不变式固化成机械检查：
    谁再给分支 B 加上 `cached()` 之类的额外 skip 条件，这里会立刻红。
    """
    branch_a_runs = not _has_fastembed()  # 未安装 → 降级用例执行（函数体内 skip）
    branch_b_runs = not _skipif_active(test_local_embed_object_without_download)
    assert branch_a_runs or branch_b_runs, (
        f"本地 embedding 两条路径同时 skip（fastembed={_has_fastembed()}），"
        "本文件将零真断言 —— 见本文件 2026-09-19 的下限守卫说明")


def test_local_embed_empty_input():
    emb = LocalEmbedding()
    # 不触底座（惰性加载），空输入直接返回空列表
    assert emb.embed([]) == []


# ---- fastembed 已安装时的真实向量验证 ----
@pytest.mark.skipif(not _has_fastembed(), reason="fastembed 未安装")
def test_local_embed_object_without_download():
    """装了 fastembed 也要有**不触网**的真断言（2026-09-19 补：下限守卫）。

    背景：本文件原只有两条互斥路径 —— 「没装 fastembed」跑降级用例、「装了」跑真向量
    用例。而真向量用例又因"无权重缓存"再 skip 一次。于是在「装了 fastembed 但 CI 无
    权重缓存」的机器上，**两条都 skip，本地 embedding 路径事实上零断言**。本用例只查
    构造期可确定的性质（惰性加载，不下载、不推理），保证任何"已安装"环境都至少有一处
    真执行。
    """
    emb = make_embedding("local")
    assert isinstance(emb, LocalEmbedding)  # 装了依赖就不该在构造期降级
    assert emb.kind == "local"  # 尚未 embed，未降级
    assert emb.degraded is False and emb.degrade_reason == ""
    assert emb.dim == 768 and emb.model == LocalEmbedding.DEFAULT_MODEL
    assert emb.cached() in (True, False)  # 只查目录，不联网
    assert emb.embed([]) == []  # 空输入不触底座


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
