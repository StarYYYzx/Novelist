"""搭建《无双开局》工作区：主角林峯·无敌平推流·人物中有系统。

规模为上一轮测试（断玉青冥：22 人 / 29 事件 / 10 章）的一半左右：
11 人物 / 15 个 key_events / 5 章。

注意：上一本书 style.json 把「系统」列为禁用词——这本书里**系统是人物**，
禁用词必须换（打卡/抽奖），否则每章都会被自己的金手指判违规。
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

PID = "proj-linfeng"
WS_ROOT = ROOT / "novel_workspace"

CHARACTERS = [
    # 主角与金手指
    {"id": "char:linfeng", "name": "林峯", "aliases": ["林师弟", "林师兄"], "gender": "male",
     "species": "human", "status": "active", "is_protagonist": True,
     "core_traits": ["从容", "腹黑", "扮猪吃虎"], "power": {"level": "金丹后期", "faction": "青云宗"},
     "arc": "无敌金手指带飞，一路装逼打脸", "first_appear": {"vol": 1, "ch": 1},
     "relationships": [{"target": "char:xitong", "type": "寄生"}, {"target": "char:shenqingli", "type": "al_ly"}]},

    {"id": "char:xitong", "name": "无敌系统", "aliases": ["系统", "老系统"], "gender": "unknown",
     "species": "aeon", "status": "active",
     "core_traits": ["冷冰冰", "毒舌", "爱记仇"], "power": {"level": "", "faction": "无敌系统"},
     "arc": "来历不明的金手指，与魔主残魂有旧账", "first_appear": {"vol": 1, "ch": 1},
     "relationships": [{"target": "char:linfeng", "type": "寄生"}]},

    # 宗门（炮灰与长辈）
    {"id": "char:zhaoqian", "name": "赵乾", "aliases": ["赵执事"], "gender": "male",
     "species": "human", "status": "active", "core_traits": ["势利", "克扣"],
     "power": {"level": "炼气九层", "faction": "青云宗"}, "arc": "克扣灵石被一掌拍飞",
     "first_appear": {"vol": 1, "ch": 1}},

    {"id": "char:wuhu", "name": "吴虎", "aliases": ["虎哥"], "gender": "male",
     "species": "human", "status": "active", "core_traits": ["霸道", "欺软怕硬"],
     "power": {"level": "炼气七层", "faction": "青云宗"}, "arc": "外门大比被碾压",
     "first_appear": {"vol": 1, "ch": 2}},

    {"id": "char:xuanyang", "name": "玄阳子", "aliases": ["掌门"], "gender": "male",
     "species": "human", "status": "active", "core_traits": ["深谋", "爱才"],
     "power": {"level": "元婴后期", "faction": "青云宗"}, "arc": "想收林峯为亲传被拒",
     "first_appear": {"vol": 1, "ch": 3}},

    {"id": "char:tieshan", "name": "铁面", "aliases": ["铁长老", "执法长老"], "gender": "male",
     "species": "human", "status": "active", "core_traits": ["严苛", "执拗"],
     "power": {"level": "金丹中期", "faction": "青云宗"}, "arc": "出手拦人被反震",
     "first_appear": {"vol": 1, "ch": 3}},

    {"id": "char:shenqingli", "name": "沈青璃", "aliases": ["青璃师姐", "青璃"], "gender": "female",
     "species": "human", "status": "active", "core_traits": ["清冷", "好奇"],
     "power": {"level": "筑基中期", "faction": "青云宗"}, "arc": "初见林峯记名，后成关键证人",
     "first_appear": {"vol": 1, "ch": 2}},

    # 反派
    {"id": "char:tujiu", "name": "屠九", "aliases": ["少门主"], "gender": "male",
     "species": "human", "status": "active", "core_traits": ["残暴", "欺软"],
     "power": {"level": "筑基大圆满", "faction": "血刀门"}, "arc": "夜袭青云宗被碾压后跪地求饶",
     "first_appear": {"vol": 1, "ch": 4}},

    {"id": "char:xueshou", "name": "血手长老", "aliases": ["血手"], "gender": "male",
     "species": "human", "status": "active", "core_traits": ["阴毒", "嗜杀"],
     "power": {"level": "金丹初期", "faction": "血刀门"}, "arc": "被林峯一指轰杀",
     "first_appear": {"vol": 1, "ch": 4}},

    # 神秘侧
    {"id": "char:laoxiazhe", "name": "老瞎子", "aliases": ["瞎前辈"], "gender": "male",
     "species": "human", "status": "active", "core_traits": ["神秘", "话少"],
     "power": {"level": "元婴后期", "faction": "散修"}, "arc": "点破系统来历",
     "first_appear": {"vol": 1, "ch": 5}},

    {"id": "char:mozhu", "name": "魔主残魂", "aliases": ["魔主"], "gender": "unknown",
     "species": "spirit", "status": "unknown", "core_traits": ["暴戾", "执念"],
     "power": {"level": "化神", "faction": "魔道"}, "arc": "与系统有旧账，残魂苏醒的预兆",
     "first_appear": {"vol": 1, "ch": 5}},
]

LOCATIONS = [
    {"id": "loc:waimen", "name": "青云宗外门", "type": "院落", "desc": "林峯修行起步处",
     "first_appear": {"vol": 1, "ch": 1}},
    {"id": "loc:yanwu", "name": "外门演武场", "type": "广场", "desc": "外门大比之地",
     "first_appear": {"vol": 1, "ch": 2}},
    {"id": "loc:neimen", "name": "内门大殿", "type": "殿堂", "desc": "长老议事、掌门召见之处",
     "first_appear": {"vol": 1, "ch": 3}},
    {"id": "loc:fangshi", "name": "落霞坊市", "type": "集市", "desc": "修士交易之地",
     "first_appear": {"vol": 1, "ch": 4}},
    {"id": "loc:houjin", "name": "后山禁地", "type": "禁地", "desc": "镇压魔主残魂之处",
     "first_appear": {"vol": 1, "ch": 5}},
]

PLOT_THREADS = [
    {"id": "pt:xitong", "desc": "无敌系统的来历，与魔主的旧账", "status": "planted",
     "planted": {"vol": 1, "ch": 1}, "report_deadline": {"vol": 2, "ch": 0}, "returned": None},
    {"id": "pt:mozhu", "desc": "魔主残魂在禁地的封印松动", "status": "planted",
     "planted": {"vol": 1, "ch": 5}, "report_deadline": {"vol": 2, "ch": 0}, "returned": None},
    {"id": "pt:laoxia", "desc": "老瞎子的真实身份", "status": "planted",
     "planted": {"vol": 1, "ch": 5}, "report_deadline": {"vol": 2, "ch": 0}, "returned": None},
    {"id": "pt:tai", "desc": "《太上忘情录》的来源", "status": "planted",
     "planted": {"vol": 1, "ch": 2}, "report_deadline": {"vol": 2, "ch": 0}, "returned": None},
    {"id": "pt:qingli", "desc": "沈青璃与林峯的线", "status": "planted",
     "planted": {"vol": 1, "ch": 2}, "report_deadline": {"vol": 2, "ch": 0}, "returned": None},
]

TIMELINE = [
    {"id": "tl:1", "at": {"era": "青云历", "year": 1, "season": "春"}, "event": "系统激活",
     "in_chapters": [{"vol": 1, "ch": 1}]},
    {"id": "tl:2", "at": {"era": "青云历", "year": 1, "season": "春"}, "event": "外门大比碾压",
     "in_chapters": [{"vol": 1, "ch": 2}]},
    {"id": "tl:3", "at": {"era": "青云历", "year": 1, "season": "春"}, "event": "打上内门",
     "in_chapters": [{"vol": 1, "ch": 3}]},
    {"id": "tl:4", "at": {"era": "青云历", "year": 1, "season": "夏"}, "event": "血刀门夜袭被碾压",
     "in_chapters": [{"vol": 1, "ch": 4}]},
    {"id": "tl:5", "at": {"era": "青云历", "year": 1, "season": "夏"}, "event": "魔主残魂苏醒预兆",
     "in_chapters": [{"vol": 1, "ch": 5}]},
]

WORLDVIEW = {
    "name": "青云界",
    "power_system": {
        "levels": ["炼气", "筑基", "金丹", "元婴", "化神"],
        "note": "林峯开局金丹后期——本书是无敌平推流，主角比当前出场敌人高一个大境界以上",
    },
    "factions": [
        {"id": "char:linfeng", "faction": "青云宗", "note": "正道宗门"},
        {"id": "char:tujiu", "faction": "血刀门", "note": "魔道宗门"},
        {"id": "char:mozhu", "faction": "魔道", "note": "上古魔主，残魂被镇于后山禁地"},
    ],
    "rules": [
        "金丹以上修士可御空飞行",
        "宗门大比不得伤及性命",
        "禁地不允擅自进入",
    ],
}

STYLE = {
    "protagonist": {"id": "char:linfeng", "name": "林峯", "gender": "male",
                    "note": "无敌平推流主角，开局金丹后期，碾压当前所有对手"},
    "pov": "第三人称，主角限知视角",
    "tense": "过去时",
    "tone": ["热血", "爽快", "装逼打脸"],
    "forbidden_words": ["打卡", "抽奖", "直播"],
    "glossary": [{"term": "无敌系统", "note": "林峯的金手指，冷冰冰的声音"},
                 {"term": "太上忘情录", "note": "系统奖励的功法"}],
    "narration": "快节奏短句，多对白，反派必被打脸",
    "target_words_per_chapter": 800,
}

VOLUMES = [
    {"id": "vol:1", "vol": 1, "title": "无双开局", "summary": "林峯携无敌系统降临青云宗，横扫外门内门，碾压血刀门，系统来历初揭",
     "chapter_range": [1, 5], "target_words": 4000},
]

CHAPTERS = {
    (1, 1): {
        "title": "开局满级", "pov": "林峯",
        "key_events": ["无敌系统激活并发放新手大礼包", "一掌拍飞克扣灵石的赵乾", "系统发布横扫外门任务"],
        "turns": ["opening-hook", "conflict", "cliffhanger"],
        "threads_involved": ["pt:xitong"],
        "body": (
            "## 细纲要点\n"
            "- 开场：外门杂役院，赵乾克扣林峯灵石，还出言侮辱。\n"
            "- 冲突升级：林峯拍飞赵乾（金丹对炼气，碾压）；围观者哗然。\n"
            "- 转折：脑海响起冷冰冰的声音——无敌系统激活，发放新手大礼包。\n"
            "- 钩子：系统发布任务「横扫外门」；林峯看向演武场方向，笑了。\n"
        ),
    },
    (1, 2): {
        "title": "横扫外门", "pov": "林峯",
        "key_events": ["外门大比碾压吴虎", "系统奖励《太上忘情录》", "沈青璃初见林峯记名"],
        "turns": ["setup", "climax", "aftermath"],
        "threads_involved": ["pt:tai", "pt:qingli"],
        "body": (
            "## 细纲要点\n"
            "- 大比：吴虎放话要废了林峯，台下起哄。\n"
            "- 碾压：林峯一招击败吴虎；吴虎的跟班们当场倒戈。\n"
            "- 奖励：系统提示任务完成，奖励《太上忘情录》残卷。\n"
            "- 余波：高台上沈青璃记下林峯的名字。\n"
        ),
    },
    (1, 3): {
        "title": "打上内门", "pov": "林峯",
        "key_events": ["当众连败内门十杰", "反震执法长老铁面", "掌门玄阳子隔空注视"],
        "turns": ["setup", "climax", "watch"],
        "threads_involved": [],
        "body": (
            "## 细纲要点\n"
            "- 入内门：林峯连胜十场，内门十杰尽数落败。\n"
            "- 拦路：铁面出手阻止，被护体气劲反震后退。\n"
            "- 注视：主峰之上，玄阳子睁开眼，目光穿过云雾落在林峯身上。\n"
        ),
    },
    (1, 4): {
        "title": "血刀来袭", "pov": "林峯", "heavyweight": True,
        "key_events": ["血刀门屠九率众夜袭宗门", "林峯一指轰杀血手长老", "屠九跪地求饶"],
        "turns": ["raid", "climax", "aftermath"],
        "threads_involved": [],
        "body": (
            "## 细纲要点\n"
            "- 夜袭：屠九血洗外门，见人就杀；血手长老压阵。\n"
            "- 碾压：林峯现身，一指轰杀血手长老（金丹对金丹，但林峯是后期）。\n"
            "- 求饶：屠九魂飞魄散，当众跪地求饶。\n"
            "- 余波：系统提示击杀奖励；林峯问系统「你到底是谁」。\n"
        ),
    },
    (1, 5): {
        "title": "宗门震动", "pov": "林峯",
        "key_events": ["拒绝掌门收徒", "老瞎子点破系统来历", "魔主残魂苏醒的预兆"],
        "turns": ["return", "reveal", "cliffhanger"],
        "threads_involved": ["pt:xitong", "pt:laoxia", "pt:mozhu"],
        "body": (
            "## 细纲要点\n"
            "- 召见：玄阳子要收林峯为亲传，林峯拒绝，只求藏经阁自由出入权。\n"
            "- 点破：后山禁地旁，老瞎子说「你身上那东西，不是你的机缘」——系统来历初揭。\n"
            "- 预兆：禁地封印裂开一线，魔主残魂的低语传出；系统罕见地沉默。\n"
        ),
    },
}


def build() -> None:
    ws = Workspace(root=str(WS_ROOT))
    if not ws.exists(PID):
        ws.create_project(PID)
    ck = Checkpoint(ws)
    ck.save(PID, {
        "id": PID,
        "title": "无双开局",
        "brief": "林峯携无敌系统降临青云宗，开局金丹后期，一路装逼打脸；系统来历与魔主残魂的旧账逐步揭开。",
        "genre": "男频修仙·无敌平推流",
        "prefs": {"pov": "第三人称限知", "target_words_per_chapter": 800, "volumes": 1, "chapters": 5},
        "budget": {"max_tokens_out_per_chapter": 1200},
        "pipeline_state": "细纲",
        "event_seq": 0,
    })

    for c in CHARACTERS:
        c.setdefault("gender", "unknown")
    w = lambda rel, data: ws.write_json(ws._abs(f"{PID}/{rel}"), data)
    w("bible/characters.json", CHARACTERS)
    w("bible/locations.json", LOCATIONS)
    w("bible/plot_threads.json", PLOT_THREADS)
    w("bible/timeline.json", TIMELINE)
    w("bible/worldview.json", WORLDVIEW)
    w("bible/style.json", STYLE)
    w("outline/volumes.json", VOLUMES)

    for (vol, ch), spec in CHAPTERS.items():
        p = ws.outline_chapter_path(PID, vol, ch)
        p.parent.mkdir(parents=True, exist_ok=True)
        extra = "heavyweight: true\n" if spec.get("heavyweight") else ""
        front = (
            "---\n"
            f"id: ch:{vol}:{ch}\nvol: {vol}\nch: {ch}\n"
            f"title: {spec['title']}\npov: {spec['pov']}\n"
            f"key_events: [" + ", ".join(spec["key_events"]) + "]\n"
            f"turns: [" + ", ".join(spec["turns"]) + "]\n"
            f"threads_involved: [" + ", ".join(spec["threads_involved"]) + "]\n"
            + extra +
            "---\n\n"
        )
        p.write_text(front + spec["body"], encoding="utf-8")

    # 世界状态初始化（B-STATE）：从人物卡 power.level 起算
    n = init_from_bible(ws, PID)
    chars_n = len(CHARACTERS)
    events_n = sum(len(c["key_events"]) for c in CHAPTERS.values())
    print(f"built {PID}: {chars_n} 人物 / {events_n} key_events / {len(CHAPTERS)} 章")
    print(f"worldstate 初始化 {len(n['characters'])} 人")


if __name__ == "__main__":
    build()
