"""章级近重复治理：事件重演 / 结尾段复制（ch7、ch8 实证）。

## 为什么单列一个测试文件

前两章归因时修的重复检测（`_dup_stats`）只做**精确**比对，日志报得出
"整段重复 1~2 处"，却漏掉了真正刺眼的问题：ch7 与 ch8 都把**同一事件或同一
收尾动作换措辞写了两遍**——

- ch8：先写"叶岚躺地等执法队"，再写执法堂审讯，结尾把"恐惧消散 / 摸伤口皮开肉绽
  /『戏，演得不错』/ 闭眼等待"整组动作几乎原样又来一次；
- ch7：李慕白收剑离去后，又重演一遍"起身 → 李慕白拱手走 → 燕十三观察"，
  且第二遍文风突变。

精确比对对此全部失效（措辞变了），只能靠字符 n-gram 覆盖率。本文件把这两段真实
语料（精简后）固化为回归基线，锁定阈值与"只删后写的那份、保住新信息"的行为。
"""

import re

import pytest

from novelist.core.orchestrator import _completeness_problems
from novelist.core.polish import (
    NEAR_DUP_THRESHOLD,
    completeness,
    find_near_dup_paragraphs,
    strip_near_dup_sentences,
)


# ch8 结尾重演（取自 novel_workspace/proj-yelan3/drafts/chapters/1-8.md 尾部，
# 省略中间的审讯对白，保留重演结构）
CH8_REPLAY = """# 第 8 章 残玉引疑云五五开显威

叶岚躺在地上，听着远处逐渐接近的脚步声，眼中的恐惧瞬间消散，取而代之的是一片冰冷的清明。

他摸了摸肩膀上的伤口，那里皮开肉绽，鲜血直流。

“戏，演得不错。”

他轻声说道，然后闭上了眼睛，等待着执法队的到来。

执法队的灯笼把夜空照得惨白，照不亮叶岚眼底那抹刻意伪装的惊恐。

段无涯冷哼一声，将残玉扔回叶岚怀中，转身离去，背影孤傲而决绝。叶岚躺在地上，听着远处逐渐远去的脚步声，眼中的恐惧瞬间消散，取而代之的是冰冷的清明。

他摸了摸肩膀上的伤口，那里皮开肉绽，鲜血直流，但他却感到前所未有的轻松。

“戏，演得不错。”

他轻声说道，嘴角勾起一抹不易察觉的弧度，然后闭上了眼睛，等待着下一场风暴的来临。
"""

# 正常行文：同一批人物、连续动作，但每一段都在推进新信息
NORMAL_TEXT = """# 第 9 章 晨课

叶岚推开山门，晨雾里传来悠远的钟声。

他沿着石阶往下走，昨夜的伤还在隐隐作痛。

演武场上已经站了十几个弟子，各自舒展筋骨、活动关节。

云清瑶立在台侧，目光扫过人群，最后停在叶岚身上。

叶岚低头行礼，心里却在盘算下一步该如何脱身。
"""


def test_near_dup_detects_rewritten_ending():
    """重演段落必须被检出，且精确比对（旧口径）查不出——这是本检测存在的理由。"""
    pairs = find_near_dup_paragraphs(CH8_REPLAY)
    assert len(pairs) >= 3, f"重演未被充分检出：{pairs}"
    sims = [s for _, _, s in pairs]
    assert max(sims) >= NEAR_DUP_THRESHOLD
    # 旧口径（精确比对）只抓到 1 处逐字重复（"戏，演得不错。"），
    # 真正刺眼的 3 处换措辞重演全部漏掉——这是本检测存在的理由。
    assert completeness(CH8_REPLAY)["dup_paragraphs"] == 1


def test_near_dup_absent_in_normal_text():
    assert find_near_dup_paragraphs(NORMAL_TEXT) == []


def test_strip_removes_only_the_later_copy():
    """删后写的那一份，且**保住重演段里夹带的新信息**。"""
    out, removed = strip_near_dup_sentences(CH8_REPLAY)
    assert removed >= 3
    # 首次出现保留
    assert "听着远处逐渐接近的脚步声" in out
    assert "等待着执法队的到来" in out
    # 重演的第二份被删
    assert "听着远处逐渐远去的脚步声" not in out
    assert "等待着下一场风暴的来临" not in out
    # 重演段首句是新信息，必须留下（整段删除会误杀它）
    assert "段无涯冷哼一声，将残玉扔回叶岚怀中，转身离去，背影孤傲而决绝。" in out


def test_strip_keeps_title_and_normal_text():
    out, removed = strip_near_dup_sentences(NORMAL_TEXT)
    assert removed == 0
    assert out.strip() == NORMAL_TEXT.strip()


def test_strip_is_idempotent():
    once, n1 = strip_near_dup_sentences(CH8_REPLAY)
    twice, n2 = strip_near_dup_sentences(once)
    assert n2 == 0
    assert twice == once


def test_completeness_reports_near_dup():
    assert completeness(CH8_REPLAY)["dup_near_paragraphs"] >= 3
    assert completeness(NORMAL_TEXT)["dup_near_paragraphs"] == 0


def test_completeness_problems_blocks_near_dup():
    """近重复必须进审校问题清单——此前只有截断/元叙事/精确重复三类。"""
    problems = _completeness_problems(completeness(CH8_REPLAY))
    assert any("近似重复段落" in p for p in problems), problems
    assert not _completeness_problems(completeness(NORMAL_TEXT))


@pytest.mark.parametrize("text", [CH8_REPLAY, NORMAL_TEXT])
def test_strip_never_breaks_ending(text):
    """确定性修复不得把结尾弄坏（末字仍须是句末标点）。"""
    out, _ = strip_near_dup_sentences(text)
    assert out.strip()[-1] in "。！？」）】…》”’\"'"


# ---------------------------------------------------------------- 误删防线（ch6/ch7 实证）

# ch6 实证：老夫先后两次说"五五开……三个字，老夫听过"，后者是**有意的呼应**。
# n-gram 相似度 0.857，比 ch8 真正重演的 0.636 还高——阈值分不开，只能靠"对白不删"。
CH6_ECHO = """墨无极眯起眼，手指在扶手上敲了两下。

“五五开……三个字，老夫听过。”

他挥手让叶岚退下，转身望向窗外的云海。

“娃娃，你身上的东西，老夫不打听来处。可你要记住——五五开……三个字，老夫听过。”
"""

# ch7 实证：系统面板行逐字复现是设计使然；且按句切分会把行尾的 】 切出去，
# 删半截会留下孤零零的 "】"。
CH7_PANEL = """叶岚笑容一僵，缓缓抬起头，目光穿过人群看向远处阴影中的那双眼睛。

【警告：绑定对象气息紊乱。】

【警告：检测到高阶修士在探查宿主。】

他收回目光，心跳却久久没有平复下来。

【警告：绑定对象气息紊乱。】

【警告：检测到高阶修士在探查宿主。】
"""


def test_intentional_echo_survives():
    """对白里的呼应/回收不得被当成重演删掉。"""
    out, removed = strip_near_dup_sentences(CH6_ECHO)
    assert removed == 0
    assert "可你要记住——五五开……三个字，老夫听过。" in out


def test_system_panel_line_not_corrupted():
    """面板行整行保留：既不被删，也不被切成半句留下孤零零的 】。"""
    out, removed = strip_near_dup_sentences(CH7_PANEL)
    assert removed == 0
    assert out.count("【警告：检测到高阶修士在探查宿主。】") == 2
    orphan = [ln for ln in out.splitlines()
              if ("】" in ln or "【" in ln) and not re.fullmatch(r"【[^】]*】", ln.strip())]
    assert orphan == [], f"面板行被切坏：{orphan}"
