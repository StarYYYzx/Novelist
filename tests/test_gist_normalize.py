"""细纲 frontmatter 规范化（F2/P0-1 收尾）：多段收敛 + 单段原样 + 正文保真。"""

from __future__ import annotations

from novelist.core.bible import (normalize_all_gists,
                                 normalize_gist_frontmatter)
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


def _project(tmp_path, pid="proj-gnorm"):
    ws = Workspace(root=str(tmp_path))
    ws.create_project(pid)
    Checkpoint(ws).save(pid, {"id": pid, "title": "t", "pipeline_state": "Forge",
                              "event_seq": 0})
    return ws, pid


DOUBLE = ("---\n{\"id\": \"ch:1:1\", \"title\": \"新题\", \"lines_present\": "
          "[{\"id\": \"ln:a\", \"action\": \"open\"}]}\n---\n"
          "---\n{\"id\": \"ch:1:1\", \"title\": \"旧题\", \"lines_present\": "
          "[{\"id\": \"ln:old\", \"action\": \"open\"}]}\n---\n"
          "# 第 1 章\n\n正文内容。")


def test_normalize_collapses_double_frontmatter():
    out = normalize_gist_frontmatter(DOUBLE)
    assert out.count('{"id"') == 0 or out.count('"旧题"') == 0
    assert out.startswith("---\n")
    assert out.count("---\n") == 2  # 头尾各一条分隔线
    assert "旧题" not in out          # 旧版丢弃
    assert "新题" in out              # 第一份（回填目标）保留
    assert "ln:a" in out and "ln:old" not in out
    assert "正文内容。" in out         # 正文保真


def test_normalize_bare_json_segment():
    """裸 JSON 段形态（fame5 实测）：`}⏎⏎---⏎{json2}⏎⏎---⏎⏎正文`，第二段无前导 ---。"""
    bare = ("---\n{\"id\": \"ch:1:1\", \"title\": \"新题\"}\n---\n"
            "{\"id\": \"ch:1:1\", \"title\": \"旧题\"}\n\n---\n\n"
            "# 第 1 章\n\n正文内容。")
    out = normalize_gist_frontmatter(bare)
    assert "旧题" not in out and "新题" in out
    assert "正文内容。" in out
    assert out.startswith("---\n") and out.count("---\n") == 2


def test_normalize_single_frontmatter_untouched():
    single = "---\n{\"id\": \"ch:1:1\", \"title\": \"t\"}\n---\n# 第 1 章\n正文"
    assert normalize_gist_frontmatter(single) == single


def test_normalize_no_frontmatter_untouched():
    plain = "# 第 1 章 标题\nkey_events: [a, b]"
    assert normalize_gist_frontmatter(plain) == plain


def test_normalize_all_gists_writes_files(tmp_path):
    ws, pid = _project(tmp_path)
    d = ws._abs(f"{pid}/outline/chapters")
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-1.md").write_text(DOUBLE, encoding="utf-8")
    (d / "1-2.md").write_text("---\n{\"id\": \"ch:1:2\"}\n---\n正文", encoding="utf-8")
    fixed = normalize_all_gists(ws, pid)
    assert fixed == 1
    out = (d / "1-1.md").read_text(encoding="utf-8")
    assert "旧题" not in out and "正文内容。" in out
