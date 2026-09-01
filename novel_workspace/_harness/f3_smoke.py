"""M3l F3 run_ingest 全链路冒烟（FakeProvider，不调真实 LLM）。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

from novelist.forge import run_ingest, IngestResult
from novelist.forge.ingest import extract_deterministic
from novelist.providers.fake import FakeProvider
from novelist.storage.workspace import Workspace
from novelist.forge.state import ForgeState, read_transcript

REPLY = json.dumps({
    "characters": [{"name": "陆沉", "gender": "male", "realm": "练气三层",
                    "traits": ["坚毅"], "aliases": ["陆师弟"], "relation": "主角"},
                   {"name": "林远", "gender": "male", "realm": "筑基一层",
                    "traits": ["沉稳"], "aliases": [], "relation": "同门"}],
    "realms": ["练气三层", "筑基"],
    "locations": [{"name": "青云宗", "kind": "宗门"}],
    "items": [{"name": "洗髓丹", "kind": "丹药"}],
    "key_events": ["陆沉服用洗髓丹突破练气三层"],
    "pending": ["三日后闭关"],
    "foreshadowing": [],
    "style_notes": "节奏明快，对白少",
}, ensure_ascii=False)

DRAFT = """第 1 章 初入宗门

陆沉在青云宗外门修行三年，与陆沉同门的还有林远。一日，陆沉服用洗髓丹，药力入体，冲击练气三层。
三日后闭关，冲击筑基。宗门上下震动。

第 2 章 出关

林远闭关三月后出关，境界大涨。渡劫九年后失踪，宗门上下搜寻未果。陆沉望着天际，想起洗髓丹的药力。
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="f3_smoke_"))
    src = tmp / "draft.md"
    src.write_text(DRAFT, encoding="utf-8")

    ws = Workspace(root=str(tmp / "ws"))
    pid = "proj-ingest"
    ws.create_project(pid)

    class FakeIO:
        is_tty = False
        def notify(self, text: str) -> None:
            print("[notify]", text.splitlines()[0] if text else "")

    res = run_ingest(ws, pid, str(src), provider=FakeProvider(reply=REPLY),
                     genre="修仙", chapters_per_volume=20, target_words=100,
                     mode="auto", io=FakeIO())
    print("ok          =", res.ok)
    print("chapters    =", res.chapters_ingested)
    print("volumes     =", res.volumes_encoded)
    print("calls_used  =", res.calls_used)
    print("characters  =", res.characters_found)
    print("downgraded  =", res.downgraded)
    for w in res.warnings:
        print("WARN:", w)
    print("bp_path     =", Path(res.blueprint_path).exists())

    # 验证产物
    ch1 = ws.chapter_path(pid, 1, 1)
    ch2 = ws.chapter_path(pid, 1, 2)
    print("ch1 exists  =", ch1.exists(), "| ch2 exists =", ch2.exists())
    if ch1.exists():
        print("ch1 head    =", ch1.read_text(encoding="utf-8").splitlines()[0])
    gist = ws.outline_chapter_path(pid, 1, 1)
    print("gist1 exists=", gist.exists(), "| done=true =", "true" in gist.read_text(encoding="utf-8")[:200] if gist.exists() else "-")

    from novelist.forge.state import Blueprint
    bp = Blueprint.load(ws, pid)
    chars = bp.section("characters")
    print("chars       =", [(c.get("name"), c.get("role"), c.get("id")) for c in chars])
    st = bp.data["style"]
    print("style.pov   =", st.get("pov"))
    print("tone        =", st.get("tone"))
    locs = bp.section("locations")
    items = bp.section("items")
    print("locations   =", [(l.get("name"), l.get("id")) for l in locs])
    print("items       =", [(i.get("name"), i.get("id")) for i in items])
    ws_data = ws.read_json(pid, ws.bible_path(pid, "worldstate"))
    print("bible worldstate pending =", (ws_data or {}).get("pending"))
    ev = ws.bible_path(pid, "entity_progress")
    print("entity_progress exists =", ev.exists())
    st2 = ForgeState.load(ws, pid)
    print("stage       =", st2.stage)
    events = read_transcript(ws, pid)
    print("transcript  =", [e["event"] for e in events])
    assert res.ok and res.chapters_ingested == 2 and res.volumes_encoded == 1
    assert ch1.exists() and ch2.exists() and gist.exists()
    assert any(c.get("role") == "protagonist" for c in chars)
    assert st2.stage == "ingested"
    print("OK: 全链路冒烟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
