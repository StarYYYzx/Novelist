"""搭建《五五开》工作区：玄幻修仙 · 20 章长卷 · 主角叶岚（穿越者·原身大学生·五五开系统）。

五五开系统机制：绑定任意对象（接触即可），绑定期间修为与此人一致；同一时刻只能绑定
一人；解绑后修为回落到自身真实修为（自身修炼境界独立累积）。扮猪吃老虎流变体——
绑定对象决定战力上限，对手越强自己越强，靠"挑对手"完成越级。

规模：26 人物 / 20 章 / 60 key_events（长卷密度，验证 M3h/M3i 全部机制）。
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

PID = "proj-yelan"
WS_ROOT = ROOT / "novel_workspace"
TITLE = "五五开"

CHARACTERS = [
    # ============ 主角与金手指 ============
    {"id": "char:yelan", "name": "叶岚", "aliases": ["叶师弟", "叶哥"], "gender": "male",
     "species": "human", "status": "active", "is_protagonist": True,
     "core_traits": ["冷静", "心机深", "扮猪吃虎"], "power": {"level": "炼气三层", "faction": "青云宗"},
     "arc": "大学生魂穿青云宗杂役，觉醒五五开系统，靠绑定强者一路越级，最终揭开系统与魔尊的渊源",
     "first_appear": {"vol": 1, "ch": 1},
     "possessions": ["残玉"],
     "relationships": [{"target": "char:xitong", "type": "寄生"}, {"target": "char:yun", "type": "绑定对象"}]},

    {"id": "char:xitong", "name": "五五开系统", "aliases": ["系统", "老五"], "gender": "unknown",
     "species": "aeon", "status": "active",
     "core_traits": ["毒舌", "功利", "疑心重"], "power": {"level": "", "faction": "系统"},
     "arc": "来历不明的上古遗物，绑定叶岚是处心积虑，与魔尊残魂有旧账",
     "first_appear": {"vol": 1, "ch": 1},
     "relationships": [{"target": "char:yelan", "type": "寄生"}]},

    # ============ 青云宗（同门与长辈） ============
    {"id": "char:yun", "name": "云清瑶", "aliases": ["云师姐", "大师姐"], "gender": "female",
     "species": "human", "status": "active",
     "core_traits": ["飒爽", "护短", "直觉准"], "power": {"level": "筑基中期", "faction": "青云宗"},
     "arc": "真传大师姐，叶岚第一个绑定对象，从怀疑到并肩，暗生情愫",
     "first_appear": {"vol": 1, "ch": 1},
     "relationships": [{"target": "char:yelan", "type": "被绑定"}]},

    {"id": "char:chen", "name": "陈松", "aliases": ["陈长老", "陈执事"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["势利", "贪财"], "power": {"level": "筑基后期", "faction": "青云宗"},
     "arc": "外门执事，克扣杂役，被叶岚借势打脸", "first_appear": {"vol": 1, "ch": 1}},

    {"id": "char:he", "name": "何远", "aliases": ["何师兄", "老何"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["憨厚", "重义"], "power": {"level": "炼气二层", "faction": "青云宗"},
     "arc": "杂役院同伴，叶岚的普通人锚点", "first_appear": {"vol": 1, "ch": 1}},

    {"id": "char:zhou", "name": "周泰", "aliases": ["周师兄"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["欺软怕硬", "记仇"], "power": {"level": "炼气七层", "faction": "青云宗"},
     "arc": "外门弟子，找叶岚麻烦反被碾压", "first_appear": {"vol": 1, "ch": 1}},

    {"id": "char:li", "name": "李慕白", "aliases": ["李师兄", "首席"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["骄傲", "磊落"], "power": {"level": "金丹初期", "faction": "青云宗"},
     "arc": "青云宗首席，云清瑶的追求者，视叶岚为威胁，后被其气度折服",
     "first_appear": {"vol": 1, "ch": 2}},

    {"id": "char:su", "name": "苏婉", "aliases": ["苏师妹"], "gender": "female",
     "species": "human", "status": "active",
     "core_traits": ["温柔", "细心"], "power": {"level": "炼气六层", "faction": "青云宗"},
     "arc": "药堂弟子，暗线人物，察觉叶岚修为忽高忽低的秘密", "first_appear": {"vol": 1, "ch": 2}},

    {"id": "char:mo", "name": "墨无极", "aliases": ["墨老", "老祖"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["深藏不露", "通透"], "power": {"level": "化神后期", "faction": "青云宗"},
     "arc": "青云宗老祖，隐约察觉五五开的来历，对叶岚另眼相看", "first_appear": {"vol": 1, "ch": 6}},

    {"id": "char:qin", "name": "秦叔", "aliases": ["秦师叔"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["古板", "尽责"], "power": {"level": "金丹中期", "faction": "青云宗"},
     "arc": "藏经阁管事，叶岚绑定对象之一", "first_appear": {"vol": 1, "ch": 3}},

    {"id": "char:duan", "name": "段无涯", "aliases": ["段长老"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["严苛", "公正"], "power": {"level": "元婴初期", "faction": "青云宗"},
     "arc": "执法长老，宗门大比仲裁", "first_appear": {"vol": 1, "ch": 6}},

    # ============ 血刀门（中期敌人） ============
    {"id": "char:zhu", "name": "朱烈", "aliases": ["朱长老", "血手"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["暴戾", "嗜杀"], "power": {"level": "元婴中期", "faction": "血刀门"},
     "arc": "血刀门入侵先锋，被叶岚绑定化神老祖后击退", "first_appear": {"vol": 1, "ch": 5}},

    {"id": "char:yan", "name": "燕十三", "aliases": ["十三"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["阴冷", "话少"], "power": {"level": "金丹后期", "faction": "血刀门"},
     "arc": "血刀门暗桩，混入青云宗大比刺探", "first_appear": {"vol": 1, "ch": 8}},

    {"id": "char:sha", "name": "沙无咎", "aliases": ["沙门主"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["枭雄", "精于算计"], "power": {"level": "化神初期", "faction": "血刀门"},
     "arc": "血刀门门主，入侵青云宗的总策划，觊觎五五开系统", "first_appear": {"vol": 1, "ch": 12}},

    # ============ 散修与秘境 ============
    {"id": "char:gui", "name": "鬼面", "aliases": ["鬼面散人"], "gender": "unknown",
     "species": "human", "status": "active",
     "core_traits": ["疯癫", "嗜杀"], "power": {"level": "化神初期", "faction": "散修"},
     "arc": "秘境中的疯魔散修，被叶岚绑定墨老后镇杀", "first_appear": {"vol": 1, "ch": 3}},

    {"id": "char:lang", "name": "狼妖白", "aliases": ["白狼"], "gender": "male",
     "species": "beast", "status": "active",
     "core_traits": ["狡诈", "护犊"], "power": {"level": "金丹初期", "faction": "妖族"},
     "arc": "秘境守护兽，叶岚秘境中的对手与机缘", "first_appear": {"vol": 1, "ch": 4}},

    {"id": "char:hun", "name": "魔尊残魂", "aliases": ["血月魔尊"], "gender": "male",
     "species": "ghost", "status": "active",
     "core_traits": ["狂傲", "深谋"], "power": {"level": "化神大圆满", "faction": "上古魔道"},
     "arc": "系统来历的核心——上古魔尊残魂，布局万年的重生计划",
     "first_appear": {"vol": 1, "ch": 17}},

    # ============ 其他势力与配角 ============
    {"id": "char:qing", "name": "青云子", "aliases": ["掌门"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["稳重", "爱才"], "power": {"level": "元婴后期", "faction": "青云宗"},
     "arc": "青云宗掌门，血刀门入侵时的决策者", "first_appear": {"vol": 1, "ch": 12}},

    {"id": "char:ling", "name": "林晚", "aliases": ["林师姐"], "gender": "female",
     "species": "human", "status": "active",
     "core_traits": ["爽利", "好胜"], "power": {"level": "筑基后期", "faction": "青云宗"},
     "arc": "内门弟子，宗门大比与叶岚同台", "first_appear": {"vol": 1, "ch": 7}},

    {"id": "char:tie", "name": "铁牛", "aliases": ["牛哥"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["莽撞", "实诚"], "power": {"level": "炼气五层", "faction": "青云宗"},
     "arc": "杂役院出身的内门弟子，叶岚的拥趸", "first_appear": {"vol": 1, "ch": 7}},

    {"id": "char:luo", "name": "洛天行", "aliases": ["洛阁主"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["商人气", "圆滑"], "power": {"level": "金丹中期", "faction": "万宝阁"},
     "arc": "万宝阁阁主，灵脉拍卖与情报的中间人", "first_appear": {"vol": 1, "ch": 9}},

    {"id": "char:bai", "name": "白眉", "aliases": ["白眉道人"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["仙风道骨", "神秘"], "power": {"level": "化神中期", "faction": "天机阁"},
     "arc": "天机阁主，推演到五五开现世，来青云宗确认", "first_appear": {"vol": 1, "ch": 10}},

    {"id": "char:xue", "name": "雪见", "aliases": ["雪女"], "gender": "female",
     "species": "human", "status": "active",
     "core_traits": ["冷", "纯粹"], "power": {"level": "金丹后期", "faction": "万剑宗"},
     "arc": "万剑宗天才，秘境中与叶岚结缘", "first_appear": {"vol": 1, "ch": 4}},

    {"id": "char:wang", "name": "王福", "aliases": ["王管事"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["精明", "怕事"], "power": {"level": "炼气九层", "faction": "青云宗"},
     "arc": "杂役院管事，墙头草", "first_appear": {"vol": 1, "ch": 1}},

    {"id": "char:yunfei", "name": "云飞扬", "aliases": ["云师弟"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["热血", "单纯"], "power": {"level": "炼气八层", "faction": "青云宗"},
     "arc": "云清瑶之弟，叶岚的绑定对象之一", "first_appear": {"vol": 1, "ch": 5}},

    {"id": "char:baishi", "name": "白石", "aliases": ["白石真人"], "gender": "male",
     "species": "human", "status": "active",
     "core_traits": ["严肃", "务实"], "power": {"level": "元婴后期", "faction": "万剑宗"},
     "arc": "万剑宗掌门，血刀门入侵时与青云宗结盟", "first_appear": {"vol": 1, "ch": 14}},
]

LOCATIONS = [
    {"id": "loc:zawu", "name": "杂役院", "note": "叶岚穿越初期的落脚地"},
    {"id": "loc:waishan", "name": "青云外山", "note": "外门弟子与试炼之地"},
    {"id": "loc:neishan", "name": "青云主峰", "note": "内门与真传所在"},
    {"id": "loc:cangjing", "name": "藏经阁", "note": "秦叔管事，叶岚常去"},
    {"id": "loc:mijing", "name": "上古秘境", "note": "灵脉与上古遗迹，大比后开放"},
    {"id": "loc:xuedao", "name": "血刀门", "note": "入侵青云宗的敌对势力"},
]

WORLDVIEW = {
    "name": "青云界",
    "power_system": {
        "levels": ["炼气", "筑基", "金丹", "元婴", "化神"],
        "note": "修仙五境：炼气纳灵气、筑基筑道基、金丹凝丹、元婴出窍、化神近道",
    },
    "factions": [
        {"faction": "青云宗", "note": "正道大宗，叶岚所在宗门，老祖墨无极坐镇"},
        {"faction": "血刀门", "note": "邪道宗门，觊觎青云宗灵脉与五五开系统"},
        {"faction": "万剑宗", "note": "剑修宗门，血刀门入侵时与青云宗结盟"},
        {"faction": "万宝阁", "note": "修真界商盟，灵脉拍卖与情报"},
        {"faction": "天机阁", "note": "推演天机的神秘组织，注意到五五开现世"},
    ],
    "rules": [
        "同门不得相残，违者逐出宗门",
        "五五开绑定期间修为与对象一致，解绑后回落——此乃系统铁则",
    ],
    "modern_words": [],
}

STYLE = {
    "pov": "第三人称，主角限知视角",
    "tense": "过去时",
    "narration": "男频爽文节奏：冲突密集、打脸干脆、升级快",
    "tone": ["爽", "快节奏", "稍带黑色幽默"],
    "forbidden_words": ["打卡", "签到", "直播", "弹幕", "程序员"],
    "glossary": [
        {"term": "五五开", "note": "绑定对象后修为与对象一致，解绑回落"},
        {"term": "绑定", "note": "系统能力：接触目标后建立绑定，同时只能一人"},
    ],
    "protagonist": {"id": "char:yelan", "name": "叶岚", "gender": "male"},
    "target_words_per_chapter": 2400,
}

PLOT_THREADS = [
    {"id": "thread:qiyuan", "status": "planted",
     "desc": "五五开系统的来历——与上古魔尊血月残魂的渊源"},
    {"id": "thread:yupi", "status": "planted",
     "desc": "叶岚随身残玉，穿越前就在他口袋里，与系统共鸣"},
    {"id": "thread:xuemeng", "status": "planted",
     "desc": "血刀门对青云宗灵脉的谋划与入侵"},
    {"id": "thread:shenfen", "status": "planted",
     "desc": "叶岚修为忽高忽低，云清瑶与苏婉先后起疑"},
    {"id": "thread:mozong", "status": "planted",
     "desc": "上古魔宗遗迹中血月魔尊的布局"},
]

TIMELINE = [
    {"id": "tl:1", "at": {"era": "青云历", "year": 784, "season": "春"},
     "event": "叶岚魂穿青云宗杂役，觉醒五五开系统", "in_chapters": [{"vol": 1, "ch": 1}]},
    {"id": "tl:2", "at": {"era": "青云历", "year": 784, "season": "春"},
     "event": "宗门大比，叶岚绑定云清瑶一鸣惊人", "in_chapters": [{"vol": 1, "ch": 7}]},
    {"id": "tl:3", "at": {"era": "青云历", "year": 784, "season": "夏"},
     "event": "血刀门入侵青云宗，叶岚绑定墨无极迎战", "in_chapters": [{"vol": 1, "ch": 16}]},
]

ITEMS = [
    {"id": "item:xi", "name": "洗髓丹", "type": "consumable", "aliases": ["洗髓丹药"],
     "note": "筑基以下洗髓伐脉，叶岚卖丹换资源"},
    {"id": "item:ju", "name": "聚灵丹", "type": "consumable", "aliases": ["聚气丹"],
     "note": "修炼辅助，杂役院配发"},
    {"id": "item:yupi", "name": "残玉", "type": "artifact", "aliases": ["旧玉", "玉佩"],
     "note": "叶岚穿越前就有的残玉，与系统共鸣"},
    {"id": "item:qing", "name": "青云令", "type": "equipment", "aliases": ["宗门令"],
     "note": "青云宗弟子身份令牌"},
    {"id": "item:lingmai", "name": "灵脉石", "type": "material", "aliases": ["灵脉"],
     "note": "秘境灵脉出产，血刀门觊觎的资源"},
]

SKILLS = [
    {"id": "skill:wuwu", "name": "五五开", "type": "secret", "aliases": ["五五开系统"],
     "state": "核心", "note": "绑定对象修为与宿主一致，解绑回落，同时只能绑定一人"},
    {"id": "skill:qingjian", "name": "青云剑诀", "type": "combat", "aliases": ["青云剑法"],
     "state": "入门", "note": "青云宗基础剑法，叶岚的看家本事"},
    {"id": "skill:yufeng", "name": "御风步", "type": "technique", "aliases": ["御风身法"],
     "state": "小成", "note": "身法，逃命与追敌两用"},
    {"id": "skill:xuedao", "name": "血刀诀", "type": "combat", "aliases": ["血刀"],
     "state": "残卷", "note": "血刀门功法，叶岚秘境战利品"},
]

SETTINGS = [
    {"id": "set:jingjie", "keywords": ["炼气", "筑基", "金丹", "元婴", "化神", "境界"],
     "text": "修仙五境：炼气纳灵气、筑基筑道基、金丹凝丹、元婴出窍、化神近道。",
     "revealed": False, "first_ch": 1},
    {"id": "set:chuan", "keywords": ["穿越", "大学生", "前世", "魂穿"],
     "text": "叶岚前世是现代社会的大学生，魂穿到青云宗杂役弟子身上，保留前世记忆。",
     "revealed": False, "first_ch": 1},
    {"id": "set:wuwu", "keywords": ["五五开", "绑定", "修为一致", "解绑"],
     "text": "五五开系统：接触即可绑定对象，绑定期间修为与对象一致；同时只能绑定一人；"
            "解绑后修为回落，自身修炼境界独立累积。",
     "revealed": False, "first_ch": 1},
    {"id": "set:qingyun", "keywords": ["青云宗", "杂役院", "外门", "内门"],
     "text": "青云宗是正道大宗，弟子分杂役/外门/内门/真传四等，老祖墨无极化神后期坐镇。",
     "revealed": False, "first_ch": 1},
    {"id": "set:xuedao", "keywords": ["血刀门", "血刀", "邪道"],
     "text": "血刀门是邪道宗门，门主沙无咎化神初期，以血炼刀，觊觎青云宗灵脉。",
     "revealed": False, "first_ch": 5},
    {"id": "set:dabi", "keywords": ["大比", "擂台", "试炼"],
     "text": "宗门大比三年一届，杂役外门内门层层晋级，头名可入真传。",
     "revealed": False, "first_ch": 6},
    {"id": "set:mijing", "keywords": ["秘境", "灵脉", "上古遗迹"],
     "text": "上古秘境三十年一开，内有灵脉与上古遗迹，各宗弟子入内争机缘。",
     "revealed": False, "first_ch": 10},
    {"id": "set:mozun", "keywords": ["魔尊", "血月", "上古魔道"],
     "text": "千年前血月魔尊以魔道君临修真界，被正道联手镇压；其残魂与布局仍是传说。",
     "revealed": False, "first_ch": 17},
]

# 20 章细纲：每章 3 个 key_events（声明式事件清单）
OUTLINES = {
    "1-1": """# 第 1 章 魂穿杂役院

key_events: [叶岚在杂役院柴房醒来，前世大学生记忆与今生杂役身份交织, 五五开系统绑定成功，叶岚绑定云清瑶修为瞬间升至筑基中期, 陈松克扣灵石，叶岚借筑基气息震慑全场]

细纲要点：
- 叶岚魂穿青云宗杂役弟子，惊觉自己换了一具身体，前世是大学生的记忆还在。
- 脑海中"五五开系统"激活：接触即可绑定，绑定期间修为与对象一致。他误触云清瑶的手，修为瞬间从炼气三层升到筑基中期。
- 陈松克扣杂役灵石，叶岚正犹豫是否暴露修为，一转头发现绑定的筑基气息震慑了全场。
- 结尾钩子：系统冷冰冰提示"解绑后修为回落，请宿主珍惜绑定对象"。
""",
    "1-2": """# 第 2 章 修为忽高忽低的秘密

key_events: [云清瑶察觉叶岚修为忽高忽低，亲自试探, 叶岚绑定李慕白摸清首席虚实，反手布局, 苏婉在药堂发现叶岚灵力波动异常]

细纲要点：
- 云清瑶对杂役弟子修为暴涨起疑，当面试探；叶岚借"误服灵药"搪塞。
- 叶岚暗中绑定李慕白（金丹初期），摸清这位首席的实力与性格，心里有了底。
- 苏婉在药堂配药时发现叶岚的灵力波动异常，记在心里没声张。
- 结尾钩子：系统提示"检测到宿主被调查，是否绑定调查者？"——叶岚决定先按兵不动。
""",
    "1-3": """# 第 3 章 藏经阁的老狐狸

key_events: [叶岚绑定秦叔进入藏经阁三层，意外发现宗门秘辛, 鬼面散人夜袭青云外山，叶岚借秦叔金丹修为周旋, 鬼面败退，墨无极暗中注视外山动静]

细纲要点：
- 叶岚绑定藏经阁管事秦叔（金丹中期），借其身份进入三层，翻到一卷记载"五五开"的残页。
- 鬼面散人夜袭外山劫掠，叶岚借金丹修为周旋击退——在外人看来是"秦叔出手"。
- 墨无极在主峰遥望外山，若有所思。
- 结尾钩子：残页上写着"五五开……血月魔尊……"字迹被刻意抹去一半。
""",
    "1-4": """# 第 4 章 秘境前的风

key_events: [叶岚随队前往上古秘境，途中遭遇狼妖白拦路, 绑定云飞扬借其炼气八层修为护送队伍，雪见出手相助, 秘境入口开启，各宗弟子蜂拥而入]

细纲要点：
- 上古秘境三十年一开，青云宗选派弟子入内，叶岚作为杂役随队打杂。
- 路上狼妖白拦路，叶岚绑定云飞扬（炼气八层）护住队伍；万剑宗雪见恰好路过出手。
- 秘境入口灵力翻涌，各宗弟子抢入。
- 结尾钩子：系统提示"秘境深处有与本系统同源的气息"。
""",
    "1-5": """# 第 5 章 秘境第一滴血

key_events: [叶岚与雪见结伴深入秘境，遭遇血刀门朱烈埋伏, 叶岚绑定雪见以金丹修为反杀，朱烈重伤遁走, 秘境灵脉石现世，叶岚夺下第一块]

细纲要点：
- 秘境内叶岚与雪见结伴，撞上血刀门朱烈带队埋伏。
- 叶岚绑定雪见（金丹后期），修为暴涨反杀；朱烈重伤遁走，留下"血刀门记下了"。
- 灵脉石现世，叶岚抢下第一块，系统提示灵脉石可强化绑定。
- 结尾钩子：雪见盯着叶岚："你的修为，刚才变了两次。"
""",
    "1-6": """# 第 6 章 老祖的试探

key_events: [墨无极召见叶岚，言语试探五五开来历, 叶岚借墨无极化神修为演练藏经阁残页功法, 段无涯宣布宗门大比提前举行]

细纲要点：
- 墨无极以"杂役修为异常"为由召见叶岚，话里话外试探系统来历。
- 叶岚借绑定墨无极的化神修为，当众演练残页上的残缺功法，堵住质疑。
- 段无涯宣布宗门大比提前举行，各阶弟子均有名额。
- 结尾钩子：墨无极留下一句"五五开……三个字，老夫听过"。
""",
    "1-7": """# 第 7 章 大比·一鸣惊人

key_events: [大比首日叶岚绑定云清瑶以筑基修为横扫外门, 李慕白当众挑战叶岚，叶岚借势周旋不败, 燕十三混在人群中观察叶岚的修为波动]

细纲要点：
- 宗门大比首日，叶岚绑定云清瑶（筑基中期）横扫外门组，全场哗然。
- 李慕白上台挑战，叶岚借绑定周旋不败，逼平收场——众人只当他"天赋异禀"。
- 血刀门暗桩燕十三混在人群中，记下叶岚修为波动的规律。
- 结尾钩子：系统警告"绑定对象气息紊乱，检测到高阶修士在探查宿主"。
""",
    "1-8": """# 第 8 章 暗桩与毒手

key_events: [燕十三夜探青云宗被叶岚撞破，两人交手, 叶岚绑定铁牛借刀杀人，燕十三重伤逃遁, 段无涯清查内奸，叶岚洗清嫌疑]

细纲要点：
- 燕十三夜探青云宗被叶岚撞破，交手之下叶岚发现对方金丹后期。
- 叶岚绑定铁牛（炼气五层）伪装成"杂役遇袭"，喊来执法队，燕十三重伤逃遁。
- 段无涯清查内奸，叶岚的"受害者"身份洗清嫌疑。
- 结尾钩子：燕十三临逃时抛下一句"五五开……门主在等你"。
""",
    "1-9": """# 第 9 章 万宝阁的生意

key_events: [叶岚绑定洛天行出入万宝阁，拍卖洗髓丹换资源, 洛天行透露血刀门近期收购灵脉的消息, 叶岚以灵脉石为饵钓出血刀门爪牙]

细纲要点：
- 叶岚绑定万宝阁阁主洛天行（金丹中期），借身份出入拍卖会，卖出洗髓丹换修炼资源。
- 洛天行闲聊透露：血刀门近月大量收购灵脉，似乎冲着青云宗地下的灵脉来。
- 叶岚以灵脉石为饵，钓出在万宝阁活动的血刀门爪牙。
- 结尾钩子：系统提示"灵脉石共鸣——绑定对象的修为可跨对象叠加"（新机制解锁）。
""",
    "1-10": """# 第 10 章 天机阁的白眉

key_events: [白眉道人下山拜访青云宗，点名要看叶岚, 白眉推演叶岚命数被系统反噬，惊疑不定, 秘境正式开启，叶岚决定入秘境探灵脉]

细纲要点：
- 天机阁白眉道人下山拜访青云宗，点名要见"修为忽高忽低的杂役"。
- 白眉推演叶岚命数，被系统反噬吐了口血，只留下一句"此子与上古魔道有牵连"。
- 秘境正式开启，叶岚决定入内探查灵脉与系统同源气息。
- 结尾钩子：系统罕见地沉默了很久，最后说："别去。"
""",
    "1-11": """# 第 11 章 秘境深处

key_events: [叶岚在秘境深处发现与残玉共鸣的上古祭坛, 祭坛引动系统异变，叶岚修为短暂失控, 狼妖白带着狼群围住祭坛，叶岚背水一战]

细纲要点：
- 秘境深处，残玉与一座上古祭坛共鸣发光，叶岚发现祭坛纹路与藏经阁残页一致。
- 祭坛引动系统异变，叶岚修为短暂失控（绑定断开又重连）。
- 狼妖白率狼群围住祭坛，叶岚背水一战，靠新解锁的"跨对象叠加"脱困。
- 结尾钩子：祭坛深处传来一声叹息："五五开……你回来了。"
""",
    "1-12": """# 第 12 章 沙无咎的棋

key_events: [沙无咎亲临青云宗外围，以化神威压逼山, 青云子与墨无极联手应对，叶岚在侧观战, 血刀门正式宣战，青云宗进入备战]

细纲要点：
- 沙无咎亲临青云宗外围，以化神威压逼山，山门弟子跪倒一片。
- 青云子与墨无极联手应对，叶岚在侧观战，趁机绑定墨无极摸清沙无咎虚实。
- 血刀门正式宣战，青云宗进入备战，叶岚被破格提拔入内门。
- 结尾钩子：沙无咎离开前扫了叶岚一眼，那眼神像在看一件货物。
""",
    "1-13": """# 第 13 章 备战

key_events: [叶岚以青云剑诀教习杂役院备战演练, 苏婉坦白发现叶岚修为秘密，愿为他保密, 燕十三带伤潜入青云宗偷取灵脉布防图]

细纲要点：
- 备战期间叶岚以青云剑诀教习杂役院弟子演练，顺带整合人手。
- 苏婉找叶岚坦白：早发现他修为忽高忽低，但她选择保密，并提醒他当心云清瑶。
- 燕十三带伤潜入青云宗偷取灵脉布防图，被叶岚设局拦截。
- 结尾钩子：叶岚看着燕十三的尸体，系统幽幽道："你下手倒是干脆。"
""",
    "1-14": """# 第 14 章 万剑驰援

key_events: [白石真人率万剑宗驰援青云宗，与青云子结盟, 叶岚借灵脉石强化绑定，修为可跨金丹元婴切换, 决战前夜，叶岚与云清瑶摊牌五五开]

细纲要点：
- 白石真人率万剑宗驰援，与青云子结盟共抗血刀门。
- 叶岚用秘境灵脉石强化系统，解锁"跨对象绑定"——修为可在金丹/元婴间切换。
- 决战前夜，云清瑶堵住叶岚摊牌：她早已猜到他修为的真相。
- 结尾钩子：云清瑶说"不管你是谁，此战之后，你欠我一个解释"。
""",
    "1-15": """# 第 15 章 血月之夜

key_events: [血刀门夜袭青云山，血月当空魔气翻涌, 叶岚绑定墨无极以化神修为镇场，逆转战局, 沙无咎亲见五五开威能，露出志在必得的笑]

细纲要点：
- 血刀门夜袭青云山，血月当空，魔气翻涌，正道防线告急。
- 叶岚绑定墨无极（化神后期）镇场，化神威压逆转战局，朱烈被一掌镇杀。
- 沙无咎亲见五五开威能，反而笑了——他等的就是这个。
- 结尾钩子：沙无咎低语"魔尊的遗物，果然在你身上"。
""",
    "1-16": """# 第 16 章 绑定的代价

key_events: [沙无咎祭出血月祭坛，强行剥离叶岚的绑定, 叶岚修为跌回炼气，被沙无咎生擒, 墨无极拼死掩护云清瑶带走叶岚]

细纲要点：
- 沙无咎祭出血月祭坛，以魔尊秘法强行剥离叶岚的五五开绑定。
- 绑定被剥离瞬间叶岚修为跌回炼气三层，被沙无咎一掌生擒。
- 墨无极拼死掩护，云清瑶抢走叶岚遁走，墨无极重伤。
- 结尾钩子：系统声音第一次带上慌乱："宿主……我的核心，被他拿走了。"
""",
    "1-17": """# 第 17 章 魔尊残魂

key_events: [叶岚在残玉庇护下唤醒血月魔尊残魂的真相, 魔尊残魂现世，五五开系统竟是其重生布局的一环, 叶岚与魔尊残魂谈判，夺回系统核心]

细纲要点：
- 重伤昏迷中，残玉庇护叶岚神魂，让他窥见血月魔尊残魂的真相。
- 魔尊残魂现世：五五开系统是千年前布局重生的一环，绑定不过是"养蛊"。
- 叶岚以穿越者的见识与魔尊残魂谈判——你要的是重生，我要的是活着，先合作夺回核心。
- 结尾钩子：魔尊残魂冷笑："合作？你会后悔的。"
""",
    "1-18": """# 第 18 章 反攻

key_events: [叶岚借魔尊残魂之力重获五五开，绑定云清瑶反攻, 苏婉在药堂以丹药压制血刀门魔修, 叶岚突袭血刀门营地夺回系统核心]

细纲要点：
- 叶岚借魔尊残魂的魔道知识重获五五开，绑定云清瑶反攻血刀门营地。
- 苏婉在药堂以丹药压制血刀门魔修，为反攻争取时间。
- 叶岚突袭血刀门营地，夺回系统核心，与沙无咎正面交锋。
- 结尾钩子：系统重新上线第一句："宿主，谢谢你没放弃我。"
""",
    "1-19": """# 第 19 章 决战青云山

key_events: [叶岚绑定墨无极与沙无咎决战青云山巅, 魔尊残魂趁虚而入试图夺舍叶岚，被残玉压制, 叶岚以化神之身镇杀沙无咎，血刀门溃散]

细纲要点：
- 决战青云山巅，叶岚绑定墨无极（化神后期）与沙无咎血战。
- 关键时刻魔尊残魂趁虚而入试图夺舍叶岚，残玉共鸣压制住残魂。
- 叶岚抓住机会镇杀沙无咎，血刀门溃散，青云宗守住灵脉。
- 结尾钩子：魔尊残魂缩回残玉，只留下一句"下次，可没有残玉了"。
""",
    "1-20": """# 第 20 章 五五开的真相

key_events: [战后墨无极向叶岚揭示五五开与魔尊的完整渊源, 叶岚选择保留五五开但立下铁则：不再绑定他人, 云清瑶深夜来找叶岚，叶岚承诺解释一切]

细纲要点：
- 战后墨无极向叶岚揭示：五五开是魔尊以自身道基炼制的上古遗物，绑定即"借道"，宿主要么被夺舍要么超脱。
- 叶岚选择保留五五开，但立下铁则：不再绑定他人，靠自身修炼。
- 云清瑶深夜来找叶岚，叶岚终于坦白穿越者身份与系统真相。
- 结尾钩子：残玉在叶岚怀里微微发烫——魔尊残魂似乎并未就此沉寂。
""",
}


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    ws = Workspace(root=str(WS_ROOT))
    if (WS_ROOT / PID).exists() and not args.force:
        raise SystemExit(f"{PID} 已存在，用 --force 覆盖重建")

    def reset_memory() -> None:
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

    ws.create_project(PID)
    if args.force:
        reset_memory()
    ws.write_json(WS_ROOT / PID / "project.json",
                  {"id": PID, "title": TITLE, "genre": "玄幻修仙·绑定流",
                   "target": "20 章长卷测试（云端 9B）"})

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
    w("outline/volumes.json", [{
        "id": "vol:1", "vol": 1, "title": "五五开",
        "summary": "大学生叶岚魂穿青云宗杂役，觉醒五五开系统绑定强者步步崛起，"
                   "挫败血刀门入侵，揭开系统与血月魔尊的渊源，终以化神之身守护青云宗",
        "chapter_range": [1, 20], "target_words": 48000,
    }])
    for name, text in OUTLINES.items():
        p = ws.outline_chapter_path(PID, *[int(x) for x in name.split("-")])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    init_from_bible(ws, PID)
    Checkpoint(ws).save(PID, {"id": PID, "pipeline_state": "正文"})
    n_ev = len(OUTLINES) * 3
    print(f"工作区就绪：{PID}（{TITLE}）· {len(CHARACTERS)} 人 / {len(OUTLINES)} 章 / {n_ev} key_events")
    print(f"  items {len(ITEMS)} | skills {len(SKILLS)} | settings {len(SETTINGS)} | threads {len(PLOT_THREADS)}")


if __name__ == "__main__":
    main()
