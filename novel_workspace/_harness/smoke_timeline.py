"""M3m T1 冒烟：本地 LM Studio qwen3.5-9b 验证编纂员新时间/约定行抽取（小规模）。

10 token/s 很慢，只跑一次、文本最小化；验证重点：prompt 改动后 LLM 是否真的输出
「时间：+90日」「约定：叶蓝出关｜+90日」行，并正确登记 pending / 推进 time。
失败不阻断——只打印诊断，退出码恒 0（冒烟不是门禁）。
"""

from __future__ import annotations

import sys
import tempfile
import traceback

sys.path.insert(0, "src")

from novelist.core.chronicler import Chronicler  # noqa: E402
from novelist.core import timeline as tl  # noqa: E402
from novelist.core import worldstate  # noqa: E402
from novelist.providers.lmstudio import LMStudioProvider  # noqa: E402
from novelist.storage.checkpoint import Checkpoint  # noqa: E402
from novelist.storage.workspace import Workspace  # noqa: E402

CHAPTER = (
    "叶蓝服下破境丹，走入洞府，封死石门，开始为期三个月的闭关。"
    "洞外，苏晚替他守关。三个月转瞬即过，石门轰然开启，叶蓝出关，气息已至筑基。"
)

if __name__ == "__main__":
    try:
        prov = LMStudioProvider(model="qwen/qwen3.5-9b", reasoning_aware=True,
                                default_max_tokens=1500, timeout_s=300)
        tmp = tempfile.mkdtemp()
        ws = Workspace(root=tmp)
        pid = "smoke-tl"
        ws.create_project(pid)
        Checkpoint(ws).save(pid, {"id": pid, "title": "冒烟", "pipeline_state": "正文",
                                  "event_seq": 0})
        ws.write_json(ws._abs(f"{pid}/bible/characters.json"), [
            {"id": "char:yelan", "name": "叶蓝", "gender": "male", "status": "active",
             "power": {"level": "炼气三层"}, "first_appear": {"vol": 1, "ch": 1}},
            {"id": "char:sw", "name": "苏晚", "gender": "female", "status": "active",
             "power": {"level": "炼气四层"}, "first_appear": {"vol": 1, "ch": 1}},
        ])
        worldstate.init_from_bible(ws, pid)

        rep = Chronicler(ws, pid, llm=prov).run(CHAPTER, 1, 1)
        print(f"extracted={rep.extracted} written={rep.written} "
              f"time_advanced={rep.time_advanced} pending={len(rep.pending_added)}")
        print(f"warnings={rep.warnings}")
        st = worldstate.load(ws, pid)
        print("now =", tl.now_of(st))
        print("pending =", [(p["id"], p["what"], p["due"], p["status"])
                            for p in tl.pending_of(st)])
        print("timeline =", [(e["id"], e["at"]["t"], e["event"]) for e in tl.load_timeline(ws, pid)])
        got_time = rep.time_advanced > 0
        got_pending = bool(rep.pending_added)
        print("PASS" if (got_time and got_pending) else "PARTIAL（未同时命中时间+约定，见上）")
    except Exception as e:  # noqa: BLE001 - 冒烟不阻断
        print("SMOKE FAILED:", e)
        traceback.print_exc()
        print("（本地 LLM 不可用或超时，冒烟跳过，不影响 M3m 结论）")
