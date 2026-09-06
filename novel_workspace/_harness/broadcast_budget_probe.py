"""B1 预算实验：DeepSeek v4 思考模式下，超大池 prompt 的 content 稳定性对拍。

目的：回答"能否靠取消/极大化 max_tokens 解决 content 被思考吃光"，并验证
"thinking budget 预算分离"是否可行。同一 prompt，三种预算配置各跑 N 次：
  A_now1600  : 现状 thinking + max_tokens=1600（复现 degrade 基线）
  B_budget   : thinking budget_tokens=900 + max_tokens=2400（给 content 留头寸）
  C_no_limit : thinking + 不传 max_tokens（字面"取消限制"）+ 只传 max_content_tokens 大值

记录每次：content 是否空 / finish_reason / reasoning_tokens / 总 out token / 耗时 / 是否参数报错。
用法：python broadcast_budget_probe.py   （需 DEEPSEEK_API_KEY）
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_httpx = __import__("httpx")
from novelist.providers.secrets import resolve_api_key  # noqa: E402

BASE = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash"   # 全局只用 flash（用户规则禁用 pro）

# ---- 模拟超大池：26 行 ×~90 字（复刻 yelan3 broadcast 池量级，含别名+关系锚） ----
_POOL_ROWS = [
    ("青云子", "掌门·老祖·青云老祖", "青云宗宗主，化神；宿敌=叶岚的引路人"),
    ("叶岚", "叶小岚·青云弟子", "炼气三层，宿主，五五开；执念=林清月"),
    ("张浩", "张师兄·青云大师兄", "剑修，叶岚宿敌；root 动机=击败叶岚"),
    ("林清月", "月仙·清月仙子", "玉清宫圣女，叶岚心上人，他人婚约"),
    ("影皇", "影皇·暗皇", "北荒魔主，重出江湖，搅动青云风云"),
    ("顾长老", "顾老·青云长老", "炼虚长老，主持门规；与叶岚有旧"),
    ("苏晚晴", "晚晴·苏师妹", "炼丹弟子，叶岚同门，暗恋叶岚"),
    ("陈玄", "玄真人·陈师叔", "青云剑阁阁主，指剑传人"),
    ("白鹤", "鹤童·白鹤童子", "青云守山童，叶岚替身故交"),
    ("柳如烟", "柳姑娘·烟烟", "天机阁解签女，算叶岚命数"),
    ("慕容清", "慕容·清公子", "慕容世家少主，林清月未婚夫"),
    ("玄冥", "冥王·玄冥老祖", "玄冥教教主，争夺秘籍"),
    ("青鸾", "青鸾圣女·鸾儿", "青鸾山圣女，与影皇盟"),
    ("陆凝霜", "凝霜·霜姑娘", "北荒女修，张浩旧识"),
    ("秦川", "秦师兄·川哥", "青云测灵长老，招叶岚入门"),
    ("赵无极", "老祖·无极真人", "青云闭关老祖"),
    ("周胜", "周大宝·胜哥", "市井混混，叶岚发小"),
    ("陈素素", "素素·陈姑娘", "青云药房大夫，医术"),
    ("沈璧君", "璧君·沈小姐", "江南沈家女，藏秘籍残页"),
    ("李慕华", "慕华·李师叔", "青云传功长老"),
    ("王二狗", "二狗·王哥", "青云杂役，消息灵通"),
    ("孙婆婆", "孙婆·药婆婆", "青云后山药婆，识毒"),
    ("韩立", "韩道友·韩真", "散修，专修体魄"),
    ("东方不败", "东方·东方先生", "魔教右使，善易容"),
    ("独孤求败", "独孤·剑魔", "隐世剑客，独行"),
    ("风清扬", "风老·疯剑客", "华山剑庐，痴武"),
]
_EVENT = "叶岚在青云宗广场当众得知宿敌张浩击败心上人林清月的消息，顾长老现身主持公道。"


def _run(payload: dict) -> dict:
    t0 = time.time()
    key = resolve_api_key(None, ["DEEPSEEK_API_KEY", "DeepSeek-API-KEY"])
    try:
        r = _httpx.post(BASE, json=payload,
                        headers={"Authorization": f"Bearer {key}"}, timeout=120)
    except Exception as e:  # noqa: BLE001
        return {"err": str(e), "time_s": round(time.time() - t0, 1)}
    body = r.text[:400]
    dt = round(time.time() - t0, 1)
    if r.status_code != 200:
        return {"http": r.status_code, "body": body, "time_s": dt}
    data = json.loads(r.text)
    choice = data["choices"][0]
    msg = choice.get("message", {})
    content = msg.get("content") or ""
    fin = choice.get("finish_reason", "?")
    usg = data.get("usage", {})
    det = usg.get("completion_tokens_details") or {}
    return {
        "content_empty": not content.strip(),
        "content_head": content.strip()[:28] if content.strip() else "",
        "finish": fin,
        "rtok": det.get("reasoning_tokens", "?"),
        "out": usg.get("completion_tokens", "?"),
        "time_s": dt,
    }


def main() -> None:
    pool_lines = "\n".join(
        f"- {i + 1}. {n}｜{a}｜{rel or '—'}" for i, (n, a, rel) in enumerate(_POOL_ROWS)
    )
    system_p = "你是修仙小说的选角导演，只做事件选角推理。严守可及池，按 reason_category 归类，输出 JSON。"
    user_p = (
        "事件：\n" + _EVENT + "\n\n可及池（26 人，含别名｜关系）：\n" + pool_lines
        + "\n\n规则：1.只许从池里挑人 2.判断谁职能上该在场 3.输出 {\"present\":[{\"name\",\"reason_category\",\"reason\"}],\"needs\":[]}"
    )
    msgs = [{"role": "system", "content": system_p},
            {"role": "user", "content": user_p}]

    cfgs = {
        "C_8000": {**{"model": MODEL, "messages": msgs},
                   "thinking": {"type": "enabled"}, "max_tokens": 8000,
                   "response_format": {"type": "json_object"}},
        "D_16000": {**{"model": MODEL, "messages": msgs},
                    "thinking": {"type": "enabled"}, "max_tokens": 16000,
                    "response_format": {"type": "json_object"}},
    }

    print("== flash 高上限对拍：超大池 26 行 思考模式 ==")
    for name, payload in cfgs.items():
        print(f"\n### {name}")
        for k in range(2):
            rec = _run(payload)
            print(f"  run{k+1}: " + json.dumps(rec, ensure_ascii=False))


if __name__ == "__main__":
    main()