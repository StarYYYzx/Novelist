"""搭建《诡夜大学》工作区：都市高武 + 诡异复苏 + 主角是强大的诡异（扮猪吃老虎）。

规模：8 人物 / 3 章 / 9 个 key_events（用户拍板：普通大学生身份 + 8人3章9事件）。
类型切换验证（讨论第 4 条）：都市高武没有"修为"，用 实力/战力/等级 → realm；
境界体系由 worldview.power_system.levels 配置驱动，系统代码零改动。
新机制验证（M3g）：items/skills 注册表 + settings 设定条目库（首次交代状态机）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from novelist.core.worldstate import init_from_bible  # noqa: E402
from novelist.storage.checkpoint import Checkpoint  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-guiwu"
WS_ROOT = ROOT / "novel_workspace"

TITLE = "诡夜大学"

CHARACTERS = [
    # 主角：强大诡异 · 扮猪吃老虎
    {"id": "char:chen", "name": "陈默", "aliases": ["陈同学", "陈默同学"], "gender": "male",
     "species": "ghost", "status": "active", "is_protagonist": True,
     "core_traits": ["温和", "低调", "演技惊人"], "power": {"level": "诡王（武圣之上）", "faction": "无"},
     "arc": "至强的诡王缚苍伪装成普通大学生，想安安静静读完大学，却被缚灵司一步步盯上",
     "first_appear": {"vol": 1, "ch": 1},
     "possessions": ["残玉"],   # 固有物品（非"获得"，worldstate 初始化时登记）
     "relationships": [{"target": "char:xu", "type": "被调查"}, {"target": "char:zhao", "type": "室友"},
                       {"target": "char:xie", "type": "敌对"}]},

    # 缚灵司（调查与试探）
    {"id": "char:xu", "name": "许晴", "aliases": ["许调查员", "许小姐"], "gender": "female",
     "species": "human", "status": "active",
     "core_traits": ["飒爽", "敏锐", "直觉强"], "power": {"level": "内劲中期", "faction": "缚灵司"},
     "arc": "调查校园诡异案，直觉陈默不对劲，追查中反而越陷越深", "first_appear": {"vol": 1, "ch": 1},
     "relationships": [{"target": "char:chen", "type": "调查对象"}, {"target": "char:li", "type": "前辈"}]},

    {"id": "char:li", "name": "李望山", "aliases": ["李教授", "老李"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["深藏不露", "谨慎", "惜才"], "power": {"level": "化神期", "faction": "缚灵司"},
     "arc": "历史系副教授兼缚灵司顾问，课堂上试探陈默，试探未果反被将一军",
     "first_appear": {"vol": 1, "ch": 2}},

    {"id": "char:lin", "name": "林晚晴", "aliases": ["林组长"], "gender": "female",
     "species": "human", "status": "active",
     "core_traits": ["雷厉风行", "铁腕", "护短"], "power": {"level": "宗师期", "faction": "特别行动组"},
     "arc": "宗师级战力，坐镇行动组，谢百川设局时到场镇场", "first_appear": {"vol": 1, "ch": 3}},

    {"id": "char:xie", "name": "谢百川", "aliases": ["谢执事"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["城府深", "傲慢", "不择手段"], "power": {"level": "宗师期", "faction": "缚灵司"},
     "arc": "想收编疑似诡异的陈默为工具，设局试探，在陈默面前吃瘪", "first_appear": {"vol": 1, "ch": 3}},

    # 校园（日常与线索）
    {"id": "char:zhao", "name": "赵铁山", "aliases": ["铁山", "老赵"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["憨直", "热血", "讲义气"], "power": {"level": "淬体境", "faction": "无"},
     "arc": "陈默室友，体育生，无意间卷入诡异事件，是陈默的'普通人'锚点",
     "first_appear": {"vol": 1, "ch": 1}},

    {"id": "char:hei", "name": "老黑", "aliases": ["黑老板"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["油滑", "识时务", "消息灵"], "power": {"level": "内劲中期", "faction": "黑市"},
     "arc": "校门口烟酒店老板，黑市掮客，兜售各方情报", "first_appear": {"vol": 1, "ch": 2}},

    # 诡异（对手与背景）
    {"id": "char:gui", "name": "无面", "aliases": ["无面客", "无面诡"], "gender": "unknown",
     "species": "ghost", "status": "active",
     "core_traits": ["暴戾", "嗜食精气"], "power": {"level": "化神期", "faction": "诡异"},
     "arc": "猎食落单学生的低阶诡异，被陈默随手镇杀", "first_appear": {"vol": 1, "ch": 1}},
]

LOCATIONS = [
    {"id": "loc:xiao", "name": "临江市大学城", "note": "主角就读的大学，三起失踪案发生地"},
    {"id": "loc:shi", "name": "实验楼", "note": "无面客猎食学生的地点"},
    {"id": "loc:hei", "name": "校门口烟酒店", "note": "老黑的地盘，黑市消息集散地"},
    {"id": "loc:zhu", "name": "宿舍天台", "note": "陈默卸下伪装的地方"},
]

WORLDVIEW = {
    "name": "临江市·诡异复苏纪元",
    "power_system": {
        "levels": ["淬体", "内劲", "化神", "宗师", "武圣"],
        "note": "人类武者五境：淬体淬筋骨、内劲运内力、化神生神识、宗师开领域、武圣近天人",
    },
    "factions": [
        {"faction": "缚灵司", "note": "官方诡异事务机构，巡查员以内劲/化神为主，编制隐秘"},
        {"faction": "特别行动组", "note": "缚灵司直属武力，组长级即宗师"},
        {"faction": "黑市", "note": "武者与诡异的消息、符箓、兵刃流通地"},
    ],
    "rules": [
        "武者与诡异不得在凡人面前展露超凡，违者由缚灵司追责",
        "诡异以凡人精气为食，食之必留痕迹",
    ],
    # 都市高武：默认现代词表是修仙导向（电梯/手机…在此都正常），覆盖为空仅留默认西方典故
    "modern_words": [],
}

STYLE = {
    "pov": "第三人称，主角限知视角",
    "tense": "过去时",
    "narration": "都市悬疑感，克制，靠细节而非形容词堆叠",
    "tone": ["冷峻", "悬疑", "克制"],
    "forbidden_words": ["金丹", "筑基", "法宝", "掌门", "师兄", "灵气灌体", "炼丹"],
    "glossary": [
        {"term": "诡息", "note": "诡异特有的气息，人类难以察觉，缚灵司有专门的侦测符"},
        {"term": "缚苍", "note": "七年前镇杀七路诡异的诡王之名，在诡异圈是禁忌"},
    ],
    "protagonist": {"name": "陈默", "gender": "male"},
    "target_words_per_chapter": 2400,
}

PLOT_THREADS = [
    {"id": "thread:yubi", "status": "planted", "desc": "陈默贴身残玉，缚苍真身的锚点，许晴见过一次"},
    {"id": "thread:san", "status": "planted", "desc": "近月三起失踪案的时间线与陈默经过校园的时间高度重合"},
    {"id": "thread:xie", "status": "planted", "desc": "谢百川想收编疑似诡异的陈默，设局未成必留后手"},
]

TIMELINE = [
    {"id": "tl:1", "at": {"era": "诡异复苏纪元", "year": 3, "season": "秋"},
     "event": "实验楼无面客猎食学生，被陈默暗中镇杀",
     "in_chapters": [{"vol": 1, "ch": 1}]},
    {"id": "tl:2", "at": {"era": "诡异复苏纪元", "year": 3, "season": "秋"},
     "event": "许晴调档案发现失踪案时间线异常",
     "in_chapters": [{"vol": 1, "ch": 2}]},
    {"id": "tl:3", "at": {"era": "诡异复苏纪元", "year": 3, "season": "秋"},
     "event": "谢百川设局试探陈默未成，林晚晴到场",
     "in_chapters": [{"vol": 1, "ch": 3}]},
]

# 注册表：物品（可持有实体）
ITEMS = [
    {"id": "item:zhenxie", "name": "镇邪符", "type": "consumable",
     "aliases": ["镇邪符纸", "符纸"], "note": "缚灵司制式，驱散低阶诡异"},
    {"id": "item:heipi", "name": "黑皮档案", "type": "equipment",
     "aliases": ["档案", "黑皮本"], "note": "缚灵司档案册，记载诡异与觉醒者"},
    {"id": "item:yubi", "name": "残玉", "type": "artifact",
     "aliases": ["旧玉", "玉佩"], "note": "陈默贴身旧玉，缚苍真身的锚点"},
    {"id": "item:yan", "name": "老黑的特供烟", "type": "other",
     "aliases": ["特供烟", "黑烟"], "note": "烟丝里掺了符灰，能短暂压制诡息"},
]

# 注册表：功法/技能（可学习实体）
SKILLS = [
    {"id": "skill:bingxin", "name": "冰心诀", "type": "cultivation",
     "aliases": ["冰心"], "state": "大成", "note": "许晴的功法，可冻结诡息"},
    {"id": "skill:guiqi", "name": "诡息", "type": "secret",
     "aliases": ["缚苍诡息"], "state": "至暗", "note": "陈默的诡息，人类无法察觉的至暗气息"},
    {"id": "skill:lingxi", "name": "灵犀步", "type": "technique",
     "aliases": ["灵犀身法"], "state": "入门", "note": "缚灵司身法，许晴在练"},
]

# 设定条目库（首次交代状态机）：关键词命中 → 注入未交代条目 → 章末验证置位
SETTINGS = [
    {"id": "set:jingjie", "keywords": ["淬体", "内劲", "化神", "宗师", "武圣", "境界"],
     "text": "人类武者五境：淬体淬筋骨、内劲运内力、化神生神识、宗师开领域、武圣近天人。",
     "revealed": False, "first_ch": 1},
    {"id": "set:fushi", "keywords": ["诡异复苏", "灵气潮汐", "诡异"],
     "text": "三年前灵气潮汐重启，人间开始出现诡异；常人不知，缚灵司暗中镇守。",
     "revealed": False, "first_ch": 1},
    {"id": "set:fusi", "keywords": ["缚灵司"],
     "text": "缚灵司是官方诡异事务机构，编制隐秘，巡查员以内劲/化神为主。",
     "revealed": False, "first_ch": 1},
    {"id": "set:guixing", "keywords": ["诡王", "缚苍", "七路"],
     "text": "诡王是诡异中的至强者称号；缚苍曾以一己之力压服七路诡异，其名在诡异圈是禁忌。",
     "revealed": False, "first_ch": 2},
    {"id": "set:yinbi", "keywords": ["隐匿", "凡人", "不得"],
     "text": "武者与诡异不得在凡人面前展露超凡，违者由缚灵司追责。",
     "revealed": False, "first_ch": 2},
]

# 细纲：每章 3 个 key_events（声明式事件清单，事件循环驱动）
OUTLINES = {
    "1-1": """# 第 1 章 脚步声在午夜响起

key_events: [陈默深夜回宿舍，身后传来不属于人的脚步声, 实验楼下无面客猎食学生，陈默装作吓坏实则暗中镇杀, 许晴到场封锁现场，与受惊的陈默照面直觉生疑]

细纲要点：
- 开学周，大二学生陈默在图书馆待到深夜，回宿舍的路上听见身后有"多余的脚步声"——他不动声色，按普通人的反应加快脚步。
- 实验楼下，无面客猎食落单学生，陈默"碰巧"路过，惊恐地喊叫引来保安，暗中一缕诡息镇杀无面客。
- 缚灵司调查员许晴到场封锁现场，安抚"受惊学生"陈默，直觉此人不对劲，记下他的名字。
- 结尾钩子：陈默回到宿舍，赵铁山鼾声如雷，他把残玉握在掌心，残玉微微发烫。
""",
    "1-2": """# 第 2 章 档案第 41 页

key_events: [许晴调取近月档案，发现三起失踪案时间线与陈默高度重合, 李望山课堂试探陈默，暗递镇邪符，陈默以凡人反应化解, 老黑向许晴透露诡王缚苍的传闻与禁忌]

细纲要点：
- 许晴回缚灵司调档案，发现近月三起失踪案都发生在陈默经过校园的时间段，她开始盯上这个学生。
- 历史课上下课，李望山"顺口"留下陈默谈话，暗递镇邪符试探，陈默以"受惊+疑惑"的凡人反应化解，符纸毫无反应。
- 校门口烟酒店，许晴向老黑打听消息，老黑收了钱，压低声音讲起诡王缚苍：七年前镇杀七路诡异，没人见过他的脸。
- 结尾钩子：李望山回到办公室，望着窗外，拨通一个号码："那个学生，我试过了，干干净净——正因为太干净了，才不对劲。"
""",
    "1-3": """# 第 3 章 执事的棋

key_events: [谢百川以面试为名试探陈默，化神境手下逼其出手, 陈默用恐惧演完全场，试探落空，林晚晴携行动组到场, 天台之上陈默卸下伪装，诡息笼罩半城，决定陪缚灵司玩玩]

细纲要点：
- 谢百川以"勤工俭学面试"为名约见陈默，带化神境手下在场，言语逼问、气场压制，逼他露出破绽。
- 陈默用"普通学生的恐惧"演完全场——发抖、语无伦次、汗湿后背；电梯故障、路灯爆裂等"巧合"让试探落空。
- 林晚晴携特别行动组到场，宗师气场镇场，谢百川收手，皮笑肉不笑地放陈默离开。
- 深夜，陈默站在宿舍天台，缚苍的诡息无声笼罩半城，他望着缚灵司的方向，喃喃自语：谢百川，你想抓我？那便陪你玩玩。
""",
}


def _reset_memory(ws) -> None:
    """清空记忆层与产物（覆盖写；沙箱禁用 rm，一律写空文件）。"""
    mem = ws.memory_dir(PID)
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "plot_events.json").write_text("[]", encoding="utf-8")
    (mem / "fragment_index.json").write_text(
        json.dumps({"revision": 0, "kind": "", "dim": None, "fragments": []},
                   ensure_ascii=False), encoding="utf-8")
    rag = ws.rag_dir(PID)
    rag.mkdir(parents=True, exist_ok=True)
    (rag / "vectors.json").write_text("{}", encoding="utf-8")
    hist = mem / "character_histories"
    if hist.is_dir():
        for f in hist.glob("*.json"):
            f.write_text(json.dumps({"char_id": f.stem, "revision": 0, "entries": []},
                                    ensure_ascii=False), encoding="utf-8")
    for base in ("drafts/chapters", "chapters"):
        d = ws._abs(f"{PID}/{base}")
        if d.is_dir():
            for f in d.glob("*.md"):
                f.write_text("", encoding="utf-8")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆盖重建（不删除目录）")
    args = ap.parse_args()
    ws = Workspace(root=str(WS_ROOT))
    if (WS_ROOT / PID).exists() and not args.force:
        raise SystemExit(f"{PID} 已存在，用 --force 覆盖重建")
    ws.create_project(PID)
    if args.force:
        _reset_memory(ws)
    ws.write_json(WS_ROOT / PID / "project.json",
                  {"id": PID, "title": TITLE, "genre": "都市高武·诡异复苏",
                   "target": "3 章小样，验证跨类型适配与新机制"})

    def w(rel, data):
        ws.write_json(ws._abs(f"{PID}/{rel}"), data)

    w("bible/characters.json", CHARACTERS)
    w("bible/locations.json", LOCATIONS)
    w("bible/worldview.json", WORLDVIEW)
    w("bible/style.json", STYLE)
    w("bible/plot_threads.json", PLOT_THREADS)
    w("bible/timeline.json", TIMELINE)
    w("bible/items.json", ITEMS)
    w("bible/skills.json", SKILLS)
    w("bible/settings.json", SETTINGS)
    for name, text in OUTLINES.items():
        p = ws.outline_chapter_path(PID, *[int(x) for x in name.split("-")])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    init_from_bible(ws, PID)
    Checkpoint(ws).save(PID, {"id": PID, "pipeline_state": "正文"})
    n_ev = sum(len([l for l in t.splitlines() if l.startswith("key_events:")]) * 3 for t in OUTLINES.values())
    print(f"工作区就绪：{PID}（{TITLE}）· {len(CHARACTERS)} 人 / {len(OUTLINES)} 章 / {n_ev} key_events")
    print("  items:", len(ITEMS), "| skills:", len(SKILLS), "| settings:", len(SETTINGS))


if __name__ == "__main__":
    main()
