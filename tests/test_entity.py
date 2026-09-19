"""统一实体追踪层测试（第八批：四阶段 + 别名共指消歧 + 松预算）。

覆盖：bible 构建与别名归并、阶段确定性推进、核心人物起步、
needs_expansion 差异化注入判定、松档预算告警、实然进度持久化合并。
"""

from novelist.core.entity import EntityTracker
from novelist.storage.workspace import Workspace


def _project(tmp_path) -> tuple[Workspace, str]:
    ws = Workspace(root=str(tmp_path))
    pid = "proj-t"
    ws.create_project(pid)
    return ws, pid


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def _seed(ws, pid):
    _write(ws, pid, "bible/characters.json", [
        {"id": "char:yelan", "name": "叶岚", "aliases": ["叶师弟", "叶兄"],
         "gender": "male", "is_protagonist": True,
         "core_traits": ["稳健"], "power": {"level": "炼气三层"}},
        {"id": "char:yun", "name": "云清瑶", "aliases": ["云师姐"],
         "gender": "female", "core_traits": ["冰山美人"],
         "power": {"level": "筑基中期"}},
        {"id": "char:passerby", "name": "路人甲", "gender": "male",
         "core_traits": []},
    ])
    _write(ws, pid, "bible/settings.json", [
        {"id": "setting:goufa", "term": "五行功", "keywords": ["引气入体", "五灵根"]},
        {"id": "setting:zongmen", "term": "青云宗", "keywords": ["外门", "藏经阁"]},
    ])
    _write(ws, pid, "bible/items.json", [
        {"id": "item:yubi", "name": "断玉", "aliases": ["祖传玉佩"]},
    ])
    _write(ws, pid, "bible/skills.json", [
        {"id": "skill:jianjue", "name": "青莲剑诀", "aliases": []},
    ])
    _write(ws, pid, "bible/locations.json", [
        {"id": "loc:chaifang", "name": "柴房", "aliases": ["杂物间"]},
    ])
    _write(ws, pid, "bible/worldview.json",
           {"name": "青云界", "entity_budget": {"default": 3, "ensemble": 5, "climax": 7}})


def _load(ws, pid) -> EntityTracker:
    return EntityTracker.load(ws, pid)


# ---------------------------------------------------------------- 构建 + 别名共指消歧

def test_load_registers_all_entity_types(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    kinds = {e.type for e in t.entities.values()}
    assert kinds == {"character", "setting", "item", "skill", "location"}
    assert len(t.entities) == 8


def test_alias_map_resolves_coreference(tmp_path):
    """别名共指消歧：'叶师弟' 与 '叶岚' 归并到同一实体（身份合并是阶段推进正确的前提）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    hits = t.resolve("叶师弟走过柴房门口")
    assert "char:yelan" in [e.key for e in hits]   # 别名命中同一实体
    assert "loc:chaifang" in [e.key for e in hits]  # 地点也命中（文本确实含）
    assert t.resolve("云师姐")[0].key == "char:yun"
    assert t.resolve("祖传玉佩")[0].key == "item:yubi"


def test_resolve_dedupes_multiple_aliases(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    hits = t.resolve("叶岚与叶师弟并肩而行，叶兄笑道")
    assert len(hits) == 1  # 三个称呼归并为同一实体


# ---------------------------------------------------------------- 阶段确定性推进

def test_stage_progression_by_mentions(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    assert t.entities["char:passerby"].stage == "unseen"
    t.update_from_chapter("路人甲路过。", 1, 1)
    assert t.entities["char:passerby"].stage == "mentioned"   # 1 次
    t.update_from_chapter("路人甲又来了，路人甲站定。", 1, 2)  # +2 → 累计 3
    assert t.entities["char:passerby"].stage == "described"   # 3–5 次
    t.update_from_chapter("路人甲在。路人甲在。", 1, 3)        # +2 → 累计 5
    assert t.entities["char:passerby"].stage == "described"
    t.update_from_chapter("路人甲。" * 3, 1, 4)                # +3 → 累计 8
    assert t.entities["char:passerby"].stage == "established"  # ≥6 次


def test_core_character_starts_described(tmp_path):
    """主角/核心人物（有完整卡）首次出场即带完整描写 → described 起步。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    upd = t.update_from_chapter("叶岚睁眼，叶师弟握紧了拳。", 1, 1)
    assert t.entities["char:yelan"].stage == "described"
    assert "char:yelan" in upd["new"]


def test_first_last_ch_and_new_keys(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    upd = t.update_from_chapter("路人甲在练拳。", 1, 3)
    e = t.entities["char:passerby"]
    assert e.first_ch == 3 and e.last_ch == 3
    assert upd["new"] == ["char:passerby"]
    t.update_from_chapter("路人甲又来了。", 1, 5)
    assert t.entities["char:passerby"].last_ch == 5
    assert t.entities["char:passerby"].first_ch == 3  # 首现章不后移


# ---------------------------------------------------------------- 差异化注入判定

def test_needs_expansion_threshold(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    # 未出现过的路人甲：命中 → 需要展开
    assert [e.name for e in t.needs_expansion("路人甲走来")] == ["路人甲"]
    # 已 established 的实体：命中也不再提示展开
    for i in range(4):
        t.update_from_chapter("路人甲。" * 8, 1, i + 1)
    assert t.entities["char:passerby"].stage == "established"
    assert t.needs_expansion("路人甲走来") == []


def test_needs_expansion_respects_first_ch(tmp_path):
    """未到出场章的实体不提示（ch < first_ch 跳过）。用非核心物品实体（无 core 提升）。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    # 断玉已在第 5 章首次出现（物品无 core 提升，stage=mentioned）
    t.update_from_chapter("断玉现身。", 1, 5)
    # 第 3 章提到它（回溯）→ 不应提示展开
    assert t.needs_expansion("断玉", ch=3) == []
    # 第 5 章及之后 → 应提示（别名同样触发）
    assert [e.name for e in t.needs_expansion("断玉", ch=5)] == ["断玉"]
    assert [e.name for e in t.needs_expansion("祖传玉佩", ch=5)] == ["断玉"]


def test_established_names_only_established(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    for i in range(4):
        t.update_from_chapter("路人甲。" * 8, 1, i + 1)
    assert t.established_names("路人甲来了") == ["路人甲"]
    assert t.established_names("叶岚来了") == []  # 未达标不给名


# ---------------------------------------------------------------- 松档预算（用户拍板：从松）

def test_budget_default_3(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    assert t.budget_check(["a", "b"]) == []               # 2 ≤ 3 不告警
    assert t.budget_check(["a", "b", "c", "d"])           # 4 > 3 告警
    assert any("超预算" in x for x in t.budget_check(["a", "b", "c", "d"]))


def test_budget_ensemble_climax_slots(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    assert t.budget_check(["a"] * 5, "ensemble") == []    # 5 ≤ 5
    assert t.budget_check(["a"] * 6, "ensemble")          # 6 > 5
    assert t.budget_check(["a"] * 7, "climax") == []      # 7 ≤ 7
    assert t.budget_check(["a"] * 8, "climax")            # 8 > 7


def test_budget_unknown_chapter_type_falls_back(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    assert t.budget_check(["a"] * 4, "不存在的类型")  # 回退 default 3


# ---------------------------------------------------------------- 实然持久化（ADR-011 分离）

def test_save_load_merges_progress(tmp_path):
    """entity_progress.json 是实然缓存（D-6 起落 memory/）：stage/mentions 以缓存为准，bible 不动。"""
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    t.update_from_chapter("断玉现身，祖传玉佩发光。", 1, 2)
    t.save()
    p = ws.memory_path(pid, "entity_progress")  # D-6：实然缓存迁 memory/
    assert p.exists()
    # bible 原文件未被改动
    chars = ws._abs(f"{pid}/bible/characters.json")
    assert "mentions" not in chars.read_text(encoding="utf-8")

    t2 = _load(ws, pid)
    e = t2.entities["item:yubi"]
    assert e.first_ch == 2 and e.mentions == 2 and e.stage == "mentioned"


def test_save_load_no_progress_file_starts_clean(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    assert all(e.stage == "unseen" for e in t.entities.values())


def test_dormant_since(tmp_path):
    ws, pid = _project(tmp_path)
    _seed(ws, pid)
    t = _load(ws, pid)
    for i in range(4):
        t.update_from_chapter("路人甲。" * 8, 1, i + 1)  # described，末现第 4 章
    t.update_from_chapter("云清瑶。", 1, 9)
    dormant = t.dormant_since(ch=10, min_gap=5)
    assert any("路人甲" in x for x in dormant)      # 4 → 10 超 5 章
    assert not any("云清瑶" in x for x in dormant)  # 9 → 10 未超
