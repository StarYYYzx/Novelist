"""题材工艺卡（Craft Cards）。

定位：介于"用户一句话需求"与"LLM 自由发挥"之间的**可执行规范层**。

为什么需要它（2026-09-05 lingyu5 真机教训）：设定敲定阶段拍板了"写什么"
（倒叙开场/极慢热/双线），但没有把"怎么呈现"固化成规范，风格解释权全交给
style 节点的模型 → 模型自行把"系统提示音/叮/机械音"当爽文俗套加进禁用词表
（Genre Pack 自带仅 7 个现代词，不含这三项），导致一本系统流小说 7654 字里
系统零次发声，题材根基缺位。

卡片形态：带 YAML frontmatter 的 Markdown，`<!-- INJECT:BEGIN/END -->` 之间
的段落是**直接进 LLM prompt 的规范文本**，其余段落是给人看的 rationale。

生命周期：设定敲定阶段勾选 id → 写入 blueprint.style.craft_cards（provenance=user）
→ style 节点生成时读卡（避免再生成冲突规范）→ 正文生成时注入 INJECT 段。
"""
