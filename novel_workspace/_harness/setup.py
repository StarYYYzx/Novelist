"""系统整体测试：搭建《断玉青冥》工作区（设定圣经 + 大纲 + 细纲）。

系统内没有实现「世界观构建师 / 大纲师」子代理（core/subagent.py 不存在），
因此圣经与细纲只能由人工/脚本代写后写入工作区——这正是要暴露的缺口之一。

目录规约见 docs/06 §2。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from novelist.storage.checkpoint import Checkpoint  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

PID = "proj-duanyu"
ROOT = Path(__file__).resolve().parents[1]


def w(ws, rel, data):
    ws.write_json(ws._abs(f"{PID}/{rel}"), data)


# 性别：原 characters.schema.json 无 gender 字段（已补），缺此项 LLM 会把主角写成女性。
GENDER = {
    "char:suwan": "male", "char:xuanjizi": "male", "char:kumu": "male", "char:tiewuya": "male",
    "char:yuqing": "female", "char:peiwuji": "male", "char:shenqw": "female", "char:luolie": "male",
    "char:zhaohu": "male", "char:aQi": "male", "char:xuehe": "male", "char:yinwujiu": "male",
    "char:guishoutuo": "male", "char:hongyan": "female", "char:suduanyue": "male", "char:liushi": "female",
    "char:fubo": "male", "char:duguHan": "male", "char:yaowuchen": "male", "char:luoqingshu": "male",
    "char:aluo": "female", "char:peizheng": "male",
}


# ---------------------------------------------------------------- 人物卡（22 人）
CHARACTERS = [
    # 主角与师门
    {"id": "char:suwan", "name": "苏晚", "aliases": ["阿晚", "外门弃徒"], "species": "human",
     "core_traits": ["隐忍", "果决", "重诺"], "power": {"level": "炼气三层", "faction": "青云宗"},
     "arc": "从外门弃徒到青冥剑主", "first_appear": {"vol": 1, "ch": 1}, "status": "active",
     "relationships": [{"target": "char:peiwuji", "type": "rival"}, {"target": "char:shenqw", "type": "ally"},
                       {"target": "char:suduanyue", "type": "kin"}, {"target": "char:aQi", "type": "friend"}]},

    {"id": "char:xuanjizi", "name": "玄机子", "aliases": ["掌门"], "species": "human",
     "core_traits": ["深谋", "寡言"], "power": {"level": "元婴后期", "faction": "青云宗"},
     "arc": "护宗而斡旋于世家与宗门之间", "first_appear": {"vol": 1, "ch": 1}, "status": "active",
     "relationships": [{"target": "char:suwan", "type": "mentor"}]},

    {"id": "char:kumu", "name": "枯木长老", "aliases": ["枯木头"], "species": "human",
     "core_traits": ["古怪", "惜才"], "power": {"level": "金丹中期", "faction": "青云宗"},
     "arc": "藏经阁守阁人，暗查断玉佩之秘", "first_appear": {"vol": 1, "ch": 5}, "status": "active",
     "relationships": [{"target": "char:suwan", "type": "mentor"}]},

    {"id": "char:tiewuya", "name": "铁无涯", "aliases": ["执法长老"], "species": "human",
     "core_traits": ["严苛", "公直"], "power": {"level": "金丹初期", "faction": "青云宗"},
     "arc": "执法不阿，逐苏晚出内门", "first_appear": {"vol": 1, "ch": 1}, "status": "active",
     "relationships": [{"target": "char:suwan", "type": "judge"}]},

    {"id": "char:yuqing", "name": "玉磬长老", "aliases": ["传功长老"], "species": "human",
     "core_traits": ["温和", "守成"], "power": {"level": "金丹初期", "faction": "青云宗"},
     "arc": "主持外门传功", "first_appear": {"vol": 1, "ch": 3}, "status": "active",
     "relationships": []},

    {"id": "char:peiwuji", "name": "裴无忌", "aliases": ["大师兄"], "species": "human",
     "core_traits": ["骄矜", "好胜"], "power": {"level": "筑基后期", "faction": "青云宗"},
     "arc": "内门首席，因苏晚而失势", "first_appear": {"vol": 1, "ch": 4}, "status": "active",
     "relationships": [{"target": "char:suwan", "type": "rival"}, {"target": "char:shenqw", "type": "sibling"}]},

    {"id": "char:shenqw", "name": "沈青梧", "aliases": ["二师姐"], "species": "human",
     "core_traits": ["清冷", "护短"], "power": {"level": "筑基中期", "faction": "青云宗"},
     "arc": "血煞来袭时断后重伤", "first_appear": {"vol": 1, "ch": 4}, "status": "active",
     "relationships": [{"target": "char:peiwuji", "type": "sibling"}, {"target": "char:suwan", "type": "ally"}]},

    {"id": "char:luolie", "name": "罗烈", "aliases": ["三师兄"], "species": "human",
     "core_traits": ["豪爽", "粗中有细"], "power": {"level": "筑基初期", "faction": "青云宗"},
     "arc": "外门演武的仲裁者", "first_appear": {"vol": 1, "ch": 4}, "status": "active",
     "relationships": []},

    {"id": "char:zhaohu", "name": "赵虎", "aliases": ["虎哥"], "species": "human",
     "core_traits": ["欺软", "势利"], "power": {"level": "炼气五层", "faction": "青云宗"},
     "arc": "外门霸头，被苏晚击败后怀恨", "first_appear": {"vol": 1, "ch": 2}, "status": "active",
     "relationships": [{"target": "char:suwan", "type": "enemy"}]},

    {"id": "char:aQi", "name": "阿岐", "aliases": ["药童"], "species": "human",
     "core_traits": ["怯懦", "忠诚"], "power": {"level": "凡人", "faction": "青云宗"},
     "arc": "外门药童，随苏晚成长", "first_appear": {"vol": 1, "ch": 2}, "status": "active",
     "relationships": [{"target": "char:suwan", "type": "friend"}]},

    # 血煞宗
    {"id": "char:xuehe", "name": "血河老祖", "aliases": ["老祖"], "species": "human",
     "core_traits": ["残忍", "深沉"], "power": {"level": "元婴中期", "faction": "血煞宗"},
     "arc": "图谋青云宗地脉", "first_appear": {"vol": 1, "ch": 6}, "status": "active",
     "relationships": [{"target": "char:yinwujiu", "type": "master"}]},

    {"id": "char:yinwujiu", "name": "殷无咎", "aliases": ["少宗主"], "species": "human",
     "core_traits": ["阴鸷", "求败"], "power": {"level": "筑基大圆满", "faction": "血煞宗"},
     "arc": "血洗青云外门的执行者", "first_appear": {"vol": 1, "ch": 6}, "status": "active",
     "relationships": [{"target": "char:xuehe", "type": "master"}, {"target": "char:suwan", "type": "enemy"}]},

    {"id": "char:guishoutuo", "name": "鬼手陀", "aliases": ["护法"], "species": "human",
     "core_traits": ["阴毒", "贪生"], "power": {"level": "筑基后期", "faction": "血煞宗"},
     "arc": "途中围杀苏晚", "first_appear": {"vol": 1, "ch": 9}, "status": "active",
     "relationships": [{"target": "char:suwan", "type": "enemy"}]},

    {"id": "char:hongyan", "name": "红魇", "aliases": ["妖女"], "species": "human",
     "core_traits": ["妖媚", "难测"], "power": {"level": "筑基中期", "faction": "血煞宗"},
     "arc": "对苏晚的断玉佩生出异样兴趣", "first_appear": {"vol": 1, "ch": 9}, "status": "active",
     "relationships": [{"target": "char:suwan", "type": "ambiguous"}]},

    # 身世
    {"id": "char:suduanyue", "name": "苏断岳", "aliases": ["断岳侯"], "species": "human",
     "core_traits": ["刚烈", "失踪"], "power": {"level": "元婴初期", "faction": "青云宗"},
     "arc": "十五年前镇守北境后失踪，断玉佩原主", "first_appear": {"vol": 1, "ch": 7}, "status": "unknown",
     "relationships": [{"target": "char:suwan", "type": "kin"}, {"target": "char:peizheng", "type": "sworn"}]},

    {"id": "char:liushi", "name": "柳氏", "aliases": ["苏夫人"], "species": "human",
     "core_traits": ["温婉", "早逝"], "power": {"level": "凡人", "faction": "无"},
     "arc": "苏晚生母，病故", "first_appear": {"vol": 1, "ch": 7}, "status": "dead",
     "relationships": [{"target": "char:suwan", "type": "kin"}]},

    {"id": "char:fubo", "name": "福伯", "aliases": ["老仆"], "species": "human",
     "core_traits": ["沉默", "守秘"], "power": {"level": "凡人", "faction": "苏家"},
     "arc": "抚养苏晚，知其身世而不言", "first_appear": {"vol": 1, "ch": 7}, "status": "active",
     "relationships": [{"target": "char:suwan", "type": "servant"}]},

    # 外援
    {"id": "char:duguHan", "name": "独孤寒", "aliases": ["剑痴"], "species": "human",
     "core_traits": ["痴剑", "孤高"], "power": {"level": "金丹后期", "faction": "散修"},
     "arc": "途中出手救苏晚", "first_appear": {"vol": 1, "ch": 8}, "status": "active",
     "relationships": [{"target": "char:suwan", "type": "mentor"}]},

    {"id": "char:yaowuchen", "name": "药无尘", "aliases": ["丹师"], "species": "human",
     "core_traits": ["贪财", "医者仁心"], "power": {"level": "筑基后期", "faction": "散修"},
     "arc": "以丹药换苏晚的玉佩秘密", "first_appear": {"vol": 1, "ch": 8}, "status": "active",
     "relationships": [{"target": "char:suwan", "type": "ally"}]},

    {"id": "char:luoqingshu", "name": "洛青书", "aliases": ["阵法师"], "species": "human",
     "core_traits": ["书呆", "严谨"], "power": {"level": "筑基初期", "faction": "青云宗"},
     "arc": "修复护宗大阵", "first_appear": {"vol": 1, "ch": 10}, "status": "active",
     "relationships": []},

    {"id": "char:aluo", "name": "阿萝", "aliases": ["神秘少女"], "species": "human",
     "core_traits": ["灵动", "来历不明"], "power": {"level": "未知", "faction": "未知"},
     "arc": "在断玉佩中留下过声音", "first_appear": {"vol": 1, "ch": 3}, "status": "unknown",
     "relationships": [{"target": "char:suwan", "type": "ambiguous"}]},

    {"id": "char:peizheng", "name": "裴铮", "aliases": ["镇北侯"], "species": "human",
     "core_traits": ["威重", "权谋"], "power": {"level": "凡人武夫巅峰", "faction": "大梁朝廷"},
     "arc": "持旧约到青云宗要人", "first_appear": {"vol": 1, "ch": 10}, "status": "active",
     "relationships": [{"target": "char:suduanyue", "type": "sworn"}, {"target": "char:xuanjizi", "type": "negotiator"}]},
]

LOCATIONS = [
    {"id": "loc:qingyun", "name": "青云宗", "type": "宗门",
     "desc": "落霞山脉主峰，护宗大阵残破", "first_appear": {"vol": 1, "ch": 1}},
    {"id": "loc:waimen", "name": "外门杂役院", "type": "院落",
     "desc": "外门弟子与杂役居所，赵虎称霸", "first_appear": {"vol": 1, "ch": 2}},
    {"id": "loc:cangjing", "name": "藏经阁", "type": "楼阁",
     "desc": "三层阁楼，顶层残篇禁阅", "first_appear": {"vol": 1, "ch": 5}},
    {"id": "loc:xuesha", "name": "血煞宗", "type": "宗门",
     "desc": "北境魔道宗门", "first_appear": {"vol": 1, "ch": 6}},
    {"id": "loc:duanbei", "name": "断碑谷", "type": "谷地",
     "desc": "苏断岳失踪之地", "first_appear": {"vol": 1, "ch": 7}},
]

PLOT_THREADS = [
    {"id": "pt:duanyu", "desc": "断玉佩的来历与其中声音", "status": "planted",
     "planted": {"vol": 1, "ch": 1}, "report_deadline": {"vol": 2, "ch": 0}, "returned": None},
    {"id": "pt:qingming", "desc": "《青冥诀》残篇是否完整", "status": "planted",
     "planted": {"vol": 1, "ch": 5}, "report_deadline": {"vol": 2, "ch": 0}, "returned": None},
    {"id": "pt:shenshi", "desc": "苏晚身世与苏断岳失踪真相", "status": "planted",
     "planted": {"vol": 1, "ch": 7}, "report_deadline": {"vol": 2, "ch": 0}, "returned": None},
    {"id": "pt:xuesha", "desc": "血煞宗图谋青云宗地脉", "status": "planted",
     "planted": {"vol": 1, "ch": 6}, "report_deadline": {"vol": 2, "ch": 0}, "returned": None},
    {"id": "pt:jiuyue", "desc": "镇北侯所持的旧约内容", "status": "planted",
     "planted": {"vol": 1, "ch": 10}, "report_deadline": {"vol": 2, "ch": 0}, "returned": None},
]

TIMELINE = [
    {"id": "tl:1", "at": {"era": "青冥历", "year": 1137, "season": "春"}, "event": "苏晚被逐出内门",
     "in_chapters": [{"vol": 1, "ch": 1}]},
    {"id": "tl:2", "at": {"era": "青冥历", "year": 1137, "season": "春"}, "event": "苏晚拾得断玉佩",
     "in_chapters": [{"vol": 1, "ch": 1}]},
    {"id": "tl:3", "at": {"era": "青冥历", "year": 1137, "season": "夏"}, "event": "外门演武，苏晚胜赵虎",
     "in_chapters": [{"vol": 1, "ch": 4}]},
    {"id": "tl:4", "at": {"era": "青冥历", "year": 1137, "season": "秋"}, "event": "血煞宗袭青云外门",
     "in_chapters": [{"vol": 1, "ch": 6}]},
    {"id": "tl:5", "at": {"era": "青冥历", "year": 1137, "season": "冬"}, "event": "镇北侯持旧约到访青云宗",
     "in_chapters": [{"vol": 1, "ch": 10}]},
]

WORLDVIEW = {
    "name": "青冥界",
    "power_system": {
        "levels": ["炼气", "筑基", "金丹", "元婴", "化神"],
        "note": "每层分初、中、后、大圆满四境；青云宗以《青云诀》为正典",
    },
    "factions": [
        {"id": "char:suwan", "faction": "青云宗", "note": "正道宗门，护宗大阵残破"},
        {"id": "char:xuehe", "faction": "血煞宗", "note": "魔道，炼血煞之气"},
        {"id": "char:peizheng", "faction": "大梁朝廷", "note": "凡人王朝，镇北侯掌北境兵权"},
    ],
    "rules": [
        "修士不可对凡人出手（宗门铁律，违者逐出）",
        "内门弟子三年一考，末位逐出外门",
        "藏经阁顶层残篇禁阅，须长老手令",
    ],
}

STYLE = {
    "protagonist": {"id": "char:suwan", "name": "苏晚", "gender": "male", "note": "男频修仙，主角为少年男子"},
    "pov": "第三人称，主角限知视角",
    "tense": "过去时",
    "tone": ["冷峻", "快节奏", "重因果"],
    "forbidden_words": ["系统", "签到", "叮"],
    "glossary": [{"term": "青冥诀", "note": "残篇功法"}, {"term": "断玉佩", "note": "苏断岳遗物"}],
    "narration": "短句为主，少形容词，多动作与对白",
    "target_words_per_chapter": 800,
}

# ---------------------------------------------------------------- 大纲与细纲
VOLUMES = [
    {"id": "vol:1", "vol": 1, "title": "青云试炼", "summary": "弃徒苏晚自外门起步，断玉佩初显，血煞来袭，身世揭幕",
     "chapter_range": [1, 10], "target_words": 8000},
]

CHAPTERS = {
    (1, 1): {
        "title": "弃徒", "pov": "苏晚",
        "key_events": ["三年一考末位，苏晚被逐出内门", "离山途中拾得半枚断玉佩"],
        "turns": ["opening-hook", "setup", "conflict", "cliffhanger"],
        "threads_involved": ["pt:duanyu"],
        "body": (
            "## 细纲要点\n"
            "- 开场钩子：内门大榜揭晓，苏晚三年末位，执法长老铁无涯当众摘其玉牌。\n"
            "- 冲突：裴无忌冷言讥讽，围观众人无人出声；苏晚一言不发，叩首退下。\n"
            "- 转折：离山下阶时踢到半枚焦黑玉佩，触之掌心发烫，耳畔有女子低唤。\n"
            "- 结尾钩子：玉佩裂纹中透出一线青光，远处藏经阁顶枯木长老睁眼。\n"
        ),
    },
    (1, 2): {
        "title": "外门", "pov": "苏晚",
        "key_events": ["苏晚入外门杂役院", "赵虎挑衅夺玉佩", "药童阿岐暗中相助"],
        "turns": ["setup", "conflict", "rescue"],
        "threads_involved": ["pt:duanyu"],
        "body": (
            "## 细纲要点\n"
            "- 外门杂役院：劈柴担水，赵虎收保护费。\n"
            "- 冲突：赵虎瞥见断玉佩，出手抢夺；苏晚不敌，被按入泥水。\n"
            "- 转折：药童阿岐偷偷递来止血散，并说外门演武在即，胜者可入内门。\n"
        ),
    },
    (1, 3): {
        "title": "引气", "pov": "苏晚",
        "key_events": ["断玉佩引气入体", "苏晚修为破炼气三层", "玉佩中传出阿萝之声"],
        "turns": ["setup", "breakthrough", "mystery"],
        "threads_involved": ["pt:duanyu", "pt:shenshi"],
        "body": (
            "## 细纲要点\n"
            "- 夜半柴房：苏晚握玉佩行气，玉佩生寒，引一缕青气入体。\n"
            "- 突破：连破两层，至炼气三层；经脉有灼痛，玉佩裂纹加深。\n"
            "- 悬念：玉佩中女子自称阿萝，只说了一句「你终于来了」便寂然。\n"
        ),
    },
    (1, 4): {
        "title": "演武", "pov": "苏晚",
        "key_events": ["外门演武，苏晚连胜", "苏晚击败赵虎", "大师兄裴无忌注意到苏晚"],
        "turns": ["setup", "climax", "aftermath"],
        "threads_involved": [],
        "body": (
            "## 细纲要点\n"
            "- 演武台：三师兄罗烈仲裁，玉磬长老监场。\n"
            "- 高潮：赵虎以炼气五层压制，苏晚以玉佩青气反震，一招将其击下台。\n"
            "- 余波：高台上裴无忌眯眼；沈青梧记下苏晚名字。\n"
        ),
    },
    (1, 5): {
        "title": "藏经", "pov": "苏晚",
        "key_events": ["苏晚入藏经阁", "得《青冥诀》残篇", "枯木长老暗中观察"],
        "turns": ["setup", "discovery", "watch"],
        "threads_involved": ["pt:qingming"],
        "body": (
            "## 细纲要点\n"
            "- 入阁：胜者三日可阅二层典籍。\n"
            "- 发现：一本被虫蛀的《青冥诀》残篇，只有前三页，末页写着「断岳」二字。\n"
            "- 观察：顶层阴影里枯木长老未出声，只在苏晚离去后翻开同一本书。\n"
        ),
    },
    (1, 6): {
        "title": "血煞", "pov": "苏晚",
        "key_events": ["殷无咎率血煞宗突袭外门", "外门弟子死伤惨重", "沈青梧断后重伤"],
        "turns": ["raid", "chaos", "sacrifice"],
        "threads_involved": ["pt:xuesha"],
        "body": (
            "## 细纲要点\n"
            "- 突袭：血光自北压来，殷无咎一掌碎外门牌坊。\n"
            "- 混乱：杂役四散，赵虎死于血煞掌下；苏晚拖阿岐躲入柴房。\n"
            "- 牺牲：沈青梧以筑基中期断后，被殷无咎重创，血溅石阶。\n"
        ),
    },
    (1, 7): {
        "title": "断岳", "pov": "苏晚",
        "key_events": ["苏晚身世揭开一角", "断玉佩确为苏断岳遗物", "福伯道出十五年旧事"],
        "turns": ["setup", "reveal", "resolve"],
        "threads_involved": ["pt:shenshi", "pt:duanyu"],
        "body": (
            "## 细纲要点\n"
            "- 战后：苏晚于废墟中拾到父亲旧物。\n"
            "- 揭幕：老仆福伯认出断玉佩，说苏断岳十五年前镇北失踪。\n"
            "- 立誓：苏晚以玉佩立誓，北境断碑谷，必去一趟。\n"
        ),
    },
    (1, 8): {
        "title": "出山", "pov": "苏晚",
        "key_events": ["苏晚奉命下山送信", "途中结识剑痴独孤寒", "丹师药无尘以丹换秘"],
        "turns": ["journey", "encounter", "deal"],
        "threads_involved": ["pt:duanyu"],
        "body": (
            "## 细纲要点\n"
            "- 送信：掌门玄机子命苏晚送求援信往落霞城。\n"
            "- 相遇：断桥处一邋遢剑客独孤寒，一剑斩落山间落石。\n"
            "- 交易：丹师药无尘闻玉佩气息而来，以一枚培元丹换一句实话。\n"
        ),
    },
    (1, 9): {
        "title": "围杀", "pov": "苏晚",
        "key_events": ["鬼手陀途中围杀苏晚", "妖女红魇现身", "独孤寒出手相救"],
        "turns": ["ambush", "despair", "rescue"],
        "threads_involved": ["pt:xuesha", "pt:duanyu"],
        "body": (
            "## 细纲要点\n"
            "- 伏击：鬼手陀以血煞阵困苏晚，索要玉佩。\n"
            "- 绝境：玉佩裂纹迸血，苏晚五感将失。\n"
            "- 转机：红魇忽至，盯着玉佩失神；独孤寒一剑破阵将其救走。\n"
        ),
    },
    (1, 10): {
        "title": "回宗", "pov": "苏晚",
        "key_events": ["苏晚归宗报讯", "掌门玄机子召见", "镇北侯裴铮持旧约到访"],
        "turns": ["return", "audience", "cliffhanger"],
        "threads_involved": ["pt:jiuyue", "pt:shenshi", "pt:xuesha"],
        "body": (
            "## 细纲要点\n"
            "- 归宗：苏晚带伤回报血煞图谋。\n"
            "- 召见：玄机子令洛青书修复护宗大阵，并问起断玉佩。\n"
            "- 钩子：山门外马蹄如雷，镇北侯裴铮持一纸旧约，指名要见「苏家遗孤」。\n"
        ),
    },
}


def build() -> None:
    ws = Workspace(root=str(ROOT))
    if not ws.exists(PID):
        ws.create_project(PID)
    ck = Checkpoint(ws)
    ck.save(PID, {
        "id": PID,
        "title": "断玉青冥",
        "brief": "外门弃徒苏晚拾得半枚断玉佩，自青云宗崛起，卷入血煞宗图谋与十五年身世悬案。",
        "genre": "男频修仙",
        "prefs": {"pov": "第三人称限知", "target_words_per_chapter": 800, "volumes": 1, "chapters": 10},
        "budget": {"max_tokens_out_per_chapter": 700},
        "pipeline_state": "细纲",
        "event_seq": 0,
    })

    for c in CHARACTERS:
        c["gender"] = GENDER.get(c["id"], "unknown")
    w(ws, "bible/characters.json", CHARACTERS)
    w(ws, "bible/locations.json", LOCATIONS)
    w(ws, "bible/plot_threads.json", PLOT_THREADS)
    w(ws, "bible/timeline.json", TIMELINE)
    w(ws, "bible/worldview.json", WORLDVIEW)
    w(ws, "bible/style.json", STYLE)
    w(ws, "outline/volumes.json", VOLUMES)

    for (vol, ch), spec in CHAPTERS.items():
        p = ws.outline_chapter_path(PID, vol, ch)
        p.parent.mkdir(parents=True, exist_ok=True)
        front = (
            "---\n"
            f"id: ch:{vol}:{ch}\n"
            f"vol: {vol}\n"
            f"ch: {ch}\n"
            f"title: {spec['title']}\n"
            f"pov: {spec['pov']}\n"
            "key_events: [" + ", ".join(spec["key_events"]) + "]\n"
            "turns: [" + ", ".join(spec["turns"]) + "]\n"
            "threads_involved: [" + ", ".join(spec["threads_involved"]) + "]\n"
            "---\n\n"
        )
        p.write_text(front + spec["body"], encoding="utf-8")

    # 一致性基线：立项后应有的一次快照
    ck.save(PID, json.loads(ws.project_json_path(PID).read_text(encoding="utf-8")))
    print(f"built project {PID} at {ws.project_dir(PID)}")
    print(f"  characters: {len(CHARACTERS)}")
    print(f"  plot_threads: {len(PLOT_THREADS)}")
    print(f"  timeline: {len(TIMELINE)}")
    print(f"  chapters: {len(CHAPTERS)}")
    events = sum(len(c["key_events"]) for c in CHAPTERS.values())
    print(f"  key_events in outline: {events}")


if __name__ == "__main__":
    build()
