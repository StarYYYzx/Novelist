"""第九批分阶段工作流 5 章端到端测试（云端 autodl：LLM 6006 + embedding 6008）。

项目 proj-t5：一卷 5 章。
- ch1–2 开篇期（opening_chapters=2）：预算 ×1.3、设定分批 3 条、新实体配额 3
- ch3 行文期
- ch4–5 收尾期（tail_chapters=2）：回收清单注入、payoff=True 判 paid_off、
  R-THREAD（ch4 warn / ch5 生成前 block）

设计验证点：
- 卷内伏笔 thread:yunwenling（scope=volume）须在 ch5 兑现 → paid_off
- 全书线 thread:guixuhui（scope=book）**不**进回收清单，留作下卷钩子
- EntityTracker deferred：开篇期新实体超配额 → 推迟 → 下一章优先注入

用法：python run_t5.py [--ch N]（可单章重跑）
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from novelist.core.llm import LLMProvider  # noqa: F401
from novelist.core.orchestrator import produce_chapter
from novelist.core.session import SessionInfo
from novelist.providers.lmstudio import LMStudioProvider
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace

PID = "proj-t5"
CLOUD_LLM = "http://127.0.0.1:6006"
CLOUD_EMB = "http://127.0.0.1:6008/v1"
GEN_TOKENS = 1200

BIBLE = {
    "bible/worldview.json": {
        "name": "落霞界",
        "power_system": {"levels": ["练气", "筑基", "金丹", "元婴"]},
        "rules": ["云纹令只认陆氏血脉"],
        "phase_policy": {"opening_chapters": 2, "tail_chapters": 2},
    },
    "bible/style.json": {
        "pov": "第三人称限知（陆沉视角）",
        "tone": ["热血激昂"],
        "target_words_per_chapter": 2400,
        "forbidden_words": ["打卡", "手机"],
        "protagonist": {"id": "char:luchen", "name": "陆沉", "gender": "male"},
    },
    "bible/characters.json": [
        {"id": "char:luchen", "name": "陆沉", "gender": "male", "is_protagonist": True,
         "core_traits": ["谨慎隐忍", "重情", "不服输"],
         "power": {"level": "凡人（开局）"},
         "background": "落霞宗杂役弟子，父母双亡，遗物仅一块云纹玉牌",
         "first_appear": {"vol": 1, "ch": 1}},
        {"id": "char:yunxi", "name": "云曦", "gender": "female",
         "core_traits": ["飒爽", "护短", "心思敏锐"],
         "power": {"level": "筑基后期"},
         "background": "落霞宗内门师姐，巡视杂役院时发现陆沉的天赋",
         "first_appear": {"vol": 1, "ch": 1}},
        {"id": "char:hepao", "name": "黑袍人", "gender": "male",
         "core_traits": ["阴鸷", "谨慎"],
         "power": {"level": "筑基巅峰"},
         "background": "归墟会成员，潜入落霞宗收买执事，图谋血祭",
         "first_appear": {"vol": 1, "ch": 3}},
    ],
    "bible/plot_threads.json": [
        {"id": "thread:yunwenling", "status": "planted",
         "desc": "云纹玉牌的来历与认主之谜",
         "scope": "volume", "target_vol": 1,
         "planted": {"vol": 1, "ch": 1}},
        {"id": "thread:guixuhui", "status": "planted",
         "desc": "归墟会与血祭阴谋的幕后主使",
         "scope": "book",
         "planted": {"vol": 1, "ch": 3}},
    ],
    "bible/settings.json": [
        {"id": "set:xisuidan", "term": "洗髓丹",
         "keywords": ["洗髓丹", "丹药"],
         "text": "外门弟子踏入练气一层的标准辅助丹药，服后引气入体，清除杂质"},
        {"id": "set:yinqijue", "term": "引气诀",
         "keywords": ["引气诀", "口诀"],
         "text": "落霞宗外门入门功法，练气期主修，讲究以呼吸导引天地灵气"},
        {"id": "set:xuejia", "term": "血祭",
         "keywords": ["血祭", "血色令牌"],
         "text": "归墟会的邪术仪式，以生灵精血喂养血月魔器，重启血月之劫"},
    ],
    "bible/locations.json": [
        {"id": "loc:zayiyuan", "name": "杂役院", "aliases": ["杂役院柴房"]},
        {"id": "loc:yanchang", "name": "演武场", "aliases": ["外门演武场"]},
        {"id": "loc:houshan", "name": "后山禁地", "aliases": ["后山"]},
    ],
    "bible/items.json": [
        {"id": "item:yupai", "name": "云纹玉牌", "aliases": ["玉牌", "云纹令"],
         "desc": "陆沉母亲遗物，刻云纹，实为失踪多年的宗主信物云纹令"},
    ],
    "bible/worldstate.json": {"characters": {}},
    "bible/timeline.json": [],
}

GISTS = {
    1: """# 第 1 章 玉牌发烫

key_events: [杂役弟子陆沉擦洗母亲遗物玉牌，玉牌十五年来首次发烫并浮现微光, 内门师姐云曦巡视杂役院，撞见陆沉搬运灵草时灵气亲和的异象, 云曦留下一枚洗髓丹，叮嘱陆沉入夜后到外门演武场等候]

细纲要点：
- 陆沉是落霞宗杂役弟子，父母双亡，唯一遗物是一块刻着云纹的旧玉牌。
- 玉牌发烫是十五年来头一次，陆沉心中疑惑但不敢声张。
- 云曦是内门筑基后期师姐，性格飒爽，注意到陆沉灵气亲和异于常人。
- 结尾钩子：玉牌的微光似乎在为某样东西指路。
""",
    2: """# 第 2 章 引气诀

key_events: [陆沉服下洗髓丹踏入练气一层，正式获得修习引气诀的资格, 陆沉发觉玉牌发烫的方向始终指向后山禁地，越靠近越烫, 演武场上云曦传授引气诀第一层口诀，察觉陆沉进境极快却未点破]

细纲要点：
- 洗髓丹是外门弟子踏入练气一层的标准机缘。
- 玉牌发烫的频率与方位有关，越靠近后山越烫。
- 云曦传授口诀时起了疑心：普通杂役不可能一夜引气入体。
- 结尾钩子：后山禁地在入夜后传来一声极轻的兽吼。
""",
    3: """# 第 3 章 后山交易

key_events: [陆沉入夜循玉牌指引潜入后山，撞见黑袍人与宗门执事秘密交易, 黑袍人以血色令牌换取落霞宗护山大阵的阵眼方位，自称归墟会, 陆沉藏身处被灵识扫过，玉牌自行发光遮蔽气息救他一命]

细纲要点：
- 黑袍人声音沙哑，执事称其为「首座」。
- 血色令牌上刻着与玉牌同源的云纹，陆沉心惊。
- 玉牌遮蔽气息后自行冷却，仿佛在保护陆沉。
- 结尾钩子：黑袍人离开时留下一句「玉牌出世，血月将至」。
""",
    4: """# 第 4 章 追杀

key_events: [黑袍人察觉玉牌气息外泄，深夜突袭杂役院追索玉牌, 陆沉且战且退被逼入演武场绝境，云曦赶到与黑袍人交手负伤, 危急时刻玉牌爆发金色光幕震退黑袍人，云曦认出那是宗主信物云纹令的手法]

细纲要点：
- 黑袍人修为筑基巅峰远超云曦，但玉牌光幕挡下致命一击。
- 云曦在交手中受伤，陆沉背她撤离。
- 「云纹令」三字让陆沉想起母亲临终呓语。
- 结尾钩子：黑袍人撂话三日后血祭重启。
""",
    5: """# 第 5 章 云纹令

key_events: [云曦带陆沉面见掌门，玉牌验明正是失踪多年的宗主信物云纹令, 掌门揭晓真相：陆沉母亲是前任宗主之女，因血月魔劫隐居杂役院, 陆沉继承云纹令成为记名弟子，将归墟会血祭图谋上报宗门，护山大阵临时加固]

细纲要点：
- 玉牌真相揭晓：云纹令认陆氏血脉，玉牌之谜解开。
- 归墟会与血祭阴谋的情报上交，护山大阵加固，本卷明线收束。
- 云曦与陆沉约定共同追查归墟会下落（此线跨卷，不在本卷回收）。
- 结尾钩子：血月之夜将至，归墟会首座在暗处注视。
""",
}


def setup_project(ws: Workspace, force: bool = False) -> None:
    """建项目：默认已存在则跳过（幂等）；--rebuild 才清空重建。

    注意不要在批量跑章的循环里 rmtree——沙箱守卫会拦截批量删除。
    """
    import shutil

    pdir = ws.project_dir(PID)
    if pdir.exists():
        if not force:
            return
        shutil.rmtree(pdir)
    ws.create_project(PID)
    Checkpoint(ws).save(PID, {"id": PID, "pipeline_state": "正文"})
    for rel, data in BIBLE.items():
        ws.write_json(ws._abs(f"{PID}/{rel}"), data)
    _write(ws, PID, "outline/volumes.json", [
        {"id": "vol:1", "vol": 1, "order": 1, "title": "云纹令",
         "summary": "杂役弟子陆沉凭母亲遗物云纹玉牌踏入修行，挫败归墟会血祭图谋，"
                    "揭开玉牌即宗主信物云纹令的身世真相",
         "chapter_range": [1, 5], "target_words": 12000,
         "threads_to_payoff": ["thread:yunwenling"]}])
    for ch, text in GISTS.items():
        gp = ws.outline_chapter_path(PID, 1, ch)
        gp.parent.mkdir(parents=True, exist_ok=True)
        gp.write_text(text, encoding="utf-8")
    print(f"project {PID} ready (5 chapters)", flush=True)


def _write(ws, pid, rel, data):
    ws.write_json(ws._abs(f"{pid}/{rel}"), data)


def main() -> None:
    only_ch = None
    if "--ch" in sys.argv:
        only_ch = int(sys.argv[sys.argv.index("--ch") + 1])
    setup_project(Workspace(root=str(ROOT / "novel_workspace")))

    provider = LMStudioProvider(base_url=CLOUD_LLM, model="Qwen3.5-9B-Q8_0.gguf",
                                api_key="none", enable_thinking=False,
                                reasoning_aware=True, default_max_tokens=GEN_TOKENS,
                                timeout_s=300.0)
    from novelist.core.embedding import OpenAIEmbedding

    emb = OpenAIEmbedding(model="nomic-embed-text-v1.5", base_url=CLOUD_EMB,
                          api_key="none", dim=768)
    ws = Workspace(root=str(ROOT / "novel_workspace"))
    log_path = HERE / "run_t5_log.jsonl"
    report_path = HERE / "t5阶段报告.md"

    chapters = [only_ch] if only_ch else [1, 2, 3, 4, 5]
    for ch in chapters:
        t0 = time.time()
        sess = SessionInfo(project_id=PID, agent="orchestrator")
        res = produce_chapter(
            ws, PID, 1, ch, provider, session=sess,
            prefer_direct=True, generation_tokens=GEN_TOKENS, embedding=emb,
            inject_bible=True, event_loop=True, event_review=True,
            polish=False, readback=(ch > 1), jit_characters=True,
            max_retries=1)
        dt = time.time() - t0
        rec = {
            "ch": ch, "ok": res.ok, "phase": getattr(res, "phase", "?"),
            "phase_reason": getattr(res, "phase_reason", ""),
            "events": res.events_committed, "entity_new": res.entity_new,
            "alerts": res.entity_alerts, "review_blocks": res.review_blocks,
            "chars": len(res.result or ""), "secs": round(dt, 1),
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        if not res.ok:
            print(f"!! ch{ch} failed: {res.result}", flush=True)
            continue
        # 升格草稿为正文（简化：直接复制，正式 promote 命令未实现——P1 遗留）
        draft = ws.draft_path(PID, 1, ch)
        pub = ws._abs(f"{PID}/chapters/1-{ch}.md")
        pub.parent.mkdir(parents=True, exist_ok=True)
        pub.write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
        time.sleep(5)  # 显存/服务冷却
    print("done", flush=True)


if __name__ == "__main__":
    main()
