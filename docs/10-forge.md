# 10. 构建层（Forge）：从一句话或已有稿子，到"可以直接开写"的细纲

> 本章是**构建层**的设计文档。它填补当前系统最大的一段空白：
> 流水线的前四道工序（立项 → 世界观 → 大纲 → 细纲）**至今是空转**。
> 设计已与用户三轮敲定（2026-09-01，含 29 项分支决策，见 §15），全部分支闭合；落地计划见 §14。

---

## 1. 背景：一段真实存在的断链

**现状证据**（逐条核对代码，非文档自述）：

| 环节 | 代码位置 | 实际行为 |
| --- | --- | --- |
| `novelist init` | `cli.py:37-61` | 建目录 + 写 `project.json`；`brief` 恒为 `""`，**不读任何用户输入** |
| `novelist run --to 世界观` | `cli.py:87-99` | 只推进 `PipelineStateMachine` 的**字符串状态**，不生成任何内容 |
| `bible/*.json` | 全库搜索 | **只有读，没有写**：`context.py:67-76`、`rules.py`、`knowledge.py:132`、`entity.py:112`、`phase.py:118`、`checkpoint.py:21` |
| 自动"造人" | `orchestrator.py:588+` `_jit_characters` | 只解析细纲 front-matter 的 `出场人物`，**不命名**（LLM 只补属性） |
| 自动"造设定" | `orchestrator.py:305+` `_supplement_settings` | 只在写作期从事件文本抽新专名，属**滚动补充**，不是从零构建 |
| `core/subagent.py` | 不存在 | docs/05 §3 的 7 个命名子代理（含世界观构建师、大纲师）零实现 |

**后果**：`docs/02` 的 **A1**（给定一句创意 → 世界观 + 大纲 + X 个完整章节）**不满足**；
真实的起点是"人手写 bible + 细纲"（`run_t5.py:41-102` 的 bible 是硬编码 dict，
`proj-t5` 的三张人物卡是手写的），只有 `produce_chapter` 是自动化的。

**本层的目标验收**：A1、UC-01（新书立项）、UC-02（构建世界观）、F2.1（集中数据结构）。
F5.2 的"世界观构建师/大纲师"**职能由本层承担，但不引入 subagent 框架**（见 §2 边界与 ADR-017）。

---

## 2. 定位与边界

**做什么**：把"用户的一句话"或"用户已写的若干章"变成一套**符合下游写入契约**的
bible + outline +（模式二下的）初始记忆，使 `novelist chapter 1 1` 可以立即开写。

**不做什么**（明确划线，避免本批失控）：

| 不做 | 理由 / 归属 |
| --- | --- |
| 写正文 | `produce_chapter` 的职责（ADR-002 串行逐章） |
| 实现 `core/subagent.py` 与 Agent 循环派发 | 用户拍板：本批不建，Forge 自成一体的状态机；子代理另开一批，Forge 未来可挂进去 |
| 改造 `produce_chapter` 的既有注入逻辑 | 本层只保证"喂给它的文件是对的"（§10 契约总表） |
| HTTP/Web 交互界面 | 首批只做 CLI；问答通道抽象成接口（`io_console.py`），日后换 HTTP 不动引擎 |
| 流派模板库的"全流派覆盖" | 首版 2 套（修仙男频 / 通用），模板是数据不是代码，可增量补 |
| 模式二 `--discard`（已有稿仅作素材、正文从头写） | 首版不做（2026-09-01 拍板）：F3 只做"保留为正"，纯素材模式留待后续 |

---

## 3. 总体流程

两种输入模式收敛到**同一个中间产物**——`Blueprint（构建蓝图）`，
再由**递归构建引擎**从蓝图长出最终产物。这样"模式二缺口回落模式一的商讨"
在架构上是免费的：两者操作的是同一份蓝图。

```
模式一 seed                          模式二 ingest
一句话 brief                         已写章节 *.md
   │                                     │
   ▼ S1 种子提炼（1 次 LLM）             ▼ I1 切片 + 确定性抽取
SeedSpec（流派/卖点/主角雏形/规模）        │ I2 LLM 抽取（每片 1 次）
   │                                     │ I3 归并消歧 + 文风画像
   ▼ S2 授权询问（**仅一次**）             │ I4 卷章编码 + 记忆初始化
 ┌────┴─────┐                            │ I5 缺口检测
 │          │                            │
全权(auto)  商讨(interactive)              │
 │          │ 分轮分组问答（8–12 问）       │
 │          │ ←──────── 缺口回落 ─────────┘
 └────┬─────┘
      ▼
  Blueprint（workspace/forge/blueprint.json，可编辑、可续跑）
      ▼
  递归构建引擎（每节点：1 次 LLM + 校验 + 落盘，模型自判是否继续细化）
      ▼
  bible/*.json + outline/volumes.json + outline/chapters/*.md
      ▼
  Forge Validator（schema + 交叉引用 + 覆盖度 + 可写冒烟）
      ▼
  pipeline_state = 细纲，写构建报告 workspace/forge/report.md
```

---

## 4. 数据契约

### 4.1 `project.json` 新增 `forge` 段（schema 同步改）

```jsonc
"forge": {
  "mode": "seed | ingest",              // 输入模式
  "interaction": "auto | interactive",  // 全权 / 商讨
  "stage": "slots | ask | build | validate | done",
  "blueprint_rev": 3,                   // 蓝图修订号（每次问答/构建递增）
  "calls_used": 27,                     // 已消耗 LLM 调用（预算治理，§7.3）
  "started_at": "2026-09-01T13:40:00Z",
  "last_error": null
}
```

### 4.2 `Blueprint`（`workspace/forge/blueprint.json`）

蓝图的字段 = **下游产出字段的超集 + provenance**。它是唯一中间态，可手工编辑后 `--build` 重跑。

```jsonc
{
  "rev": 3,
  "provenance": {                       // 每个字段的来源，驱动"问不问/信不信"
    "worldview.power_system.levels": {"src": "llm", "confidence": 0.6},
    "characters[char:luchen].name":    {"src": "user", "confidence": 1.0},
    "style.tone":                      {"src": "template", "confidence": 0.4},
    "volumes[0].summary":              {"src": "ingested", "evidence": "chapters/1-3.md"}
  },
  "meta":    { "title": "…", "genre": "修仙", "template": "修仙男频",
               "logline": "一句话卖点", "themes": [],
               "scale": {"volumes": 3, "chapters_per_volume": 20, "target_words_per_chapter": 2400} },
  "worldview": { "name": "…", "power_system": {"levels": [], "mechanic": "…"},
                 "rules": [], "civilizations": [], "factions": [] },
  "characters": [ { "id": "char:luchen", "name": "陆沉", "aliases": [], "gender": "male",
                    "age": 16, "role": "protagonist|mentor|rival|love_interest|minor",
                    "core_traits": [], "power": {"level": "练气三层", "faction": "落霞宗"},
                    "arc": "…", "first_appear": {"vol": 1, "ch": 1},
                    "status": "active", "relationships": [{"target": "char:yunxi", "type": "师姐"}] } ],
  "locations": [], "items": [], "skills": [],
  "settings":   [ { "id": "set:xisuidan", "keywords": ["洗髓丹"], "text": "…",
                    "revealed": false, "first_ch": 1 } ],
  "threads":    [ { "id": "pt:yupai", "desc": "玉牌为何发烫", "scope": "volume|book",
                    "target_vol": 1, "planted": {"vol": 1, "ch": 1}, "status": "planted" } ],
  "style":     { "pov": "第三人称限知（陆沉视角）", "tense": "过去", "narration": "…",
                 "tone": ["热血激昂"], "target_words_per_chapter": 2400,
                 "forbidden_words": [], "glossary": [{"term": "…", "note": "…"}],
                 "protagonist": {"id": "char:luchen", "name": "陆沉", "gender": "male"} },
  "volumes":   [ { "vol": 1, "title": "…", "chapter_range": [1, 20], "summary": "…",
                   "themes": [], "key_beats": [], "threads_to_payoff": ["pt:yupai"],
                   "target_words": 48000 } ],
  "chapters":  [ { "vol": 1, "ch": 1, "title": "玉牌发烫", "pov": "…",
                   "key_events": ["…", "…"], "turns": ["opening-hook", "conflict", "cliffhanger"],
                   "characters": ["char:luchen"], "threads_involved": ["pt:yupai"],
                   "after_days": 90,             // 可选（ADR-019）：该事件距上一事件的相对天数，
                                                  // 构建期据此登记 worldstate.pending（定时事件）
                   "done": false } ],         // done=true 表示已有正文（模式二），不再生成细纲
  "ingested":  { "source_files": ["…"], "chapter_count": 3, "encoded_as": "vol1 ch1-3" }
}
```

**`role` 是蓝图内部字段，落盘剥离**（2026-09-01 拍板，B3）：`character.role=protagonist`
是主角判定的**唯一事实源**——落盘时由它派生 `style.protagonist`（id/name/gender）并给
人物卡打 `is_protagonist` 标记；`role` 本身不写入 `characters.json`（其 schema
`additionalProperties:False` 且无此字段）。下游 `context.py:132 _mark_protagonist` 照旧工作。

**provenance 是关键设计**：它决定三件事——
① 商讨时**问什么**（低置信 + 必填槽位优先）；
② 递归时**细化什么**（`src=llm` 且 confidence < 阈值 → 优先展开）；
③ 报告里**披露什么**（哪些是用户拍板、哪些是模型猜的，出问题好回溯）。

### 4.3 `Transcript`（`workspace/forge/transcript.jsonl`）

问答留痕，每行一个事件：`{"t":…, "event":"ask|answer|skip|build_node|validate", ...}`。
用途：中断续跑（§7.4）、回放审计、以及**同一问题不重复问**。

### 4.4 落盘布局（新增目录）

```
<project>/workspace/forge/
  blueprint.json      # 蓝图（唯一中间态，可手工编辑）
  transcript.jsonl    # 问答与构建留痕
  report.md           # 构建报告：来源分布、缺口、校验结果、耗时与调用数
  nodes/              # 递归构建的每个节点产物（JSON，续跑与调试用）
    L1-volume-1.json
    L3-chapter-1-3.json
```

`workspace/` 已在 `SUBDIRS`（`storage/workspace.py:28-43`）中，无需改目录骨架。

---

## 5. 模式一 `seed`：一句话 → 蓝图

### 5.1 种子提炼（1 次 LLM）

输入用户原始 brief + 模板清单 → 输出 `SeedSpec`：

```
{ genre, template_suggestion, logline, protagonist_hint{name,gender,cheat},
  conflict, tone_hint, scale_hint{volumes, chapters_per_volume},
  unknowns: [...]            // 模型自述"这句话里我没法确定的"，直接转成问题
```

模型必须显式列 `unknowns`——这是后面商讨的问题种子之一。

### 5.2 授权询问（**仅一次**，用户明确要求）

种子提炼后**先展示提炼结果**（让用户看到模型理解对了没），然后问一次：

```
根据「<brief>」我理解为：
  流派：修仙（系统流）  卖点：五五开系统，绑定他人共享修炼
  主角：叶蓝，男，穿越者  规模：3 卷 × 20 章 × 2400 字
  不确定的：金手指规则细节、结局走向

接下来怎么构建？
  [1] 全权构建 —— 我按流派模板补全全部设定，最后给你一份清单过目
  [2] 商讨构建 —— 分 3–4 轮问你 8–12 个关键设定，每问都给候选值
选择 [1/2]：
```

### 5.3 商讨：槽位表 + 分轮分组问答

**核心原则：问题由引擎（确定性）决定，候选由模型（LLM）生成。**
不让模型自由决定"要不要问、问几个"——否则要么问个没完，要么关键项漏问。

**槽位表**（`forge/slots.py`，数据驱动）：

```jsonc
{ "key": "style.tone",
  "label": "文风基调",
  "level": "required | recommended | optional",
  "kind": "choice | confirm | free",
  "ask": "这本书的基调偏哪种？",
  "candidates_from": "llm | template | enum",   // llm=每轮批量生成候选
  "enum": ["严谨冷肃","诙谐幽默","热血激昂","温柔细腻"],
  "default": "严谨冷肃",
  "group": 3,                                   // 第几轮问
  "why": "写入 style.json 的 tone，直接驱动润色 prompt（第七批第 2 条）" }
```

**必填槽位（required，8 个）**：① 流派/模板 ② 主角姓名 + 性别（**防性别漂移**，B-02 实测）
③ 金手指/核心机制 ④ 境界体系 levels ⑤ 文风基调 ⑥ 视角 pov ⑦ 规模（卷×章×字）
⑧ 核心冲突/卖点。
**推荐槽位（recommended）**：世界名、势力格局、世界铁律、反派设定、感情线、结局走向、
禁用词、术语表、主角性格三词、叙述时态。

**分轮分组**（用户拍板的形态）：

| 轮次 | 组 | 典型问题数 | 内容 |
| --- | --- | --- | --- |
| 1 | 书级必填 | 4–5 | 流派/规模/卖点/主角名与性别/金手指 |
| 2 | 世界与规则 | 3–4 | 境界体系/世界名/势力/铁律 |
| 3 | 文风与叙事 | 3–4 | 基调/视角/时态/禁用词/术语 |
| 4 | 人物与伏笔 | 3–4 | 反派/感情线/结局走向/主线伏笔 |

**每轮的交互形态**（单屏呈现，一次回答）：

```
── 第 2 轮：世界与规则 ─────────────────────────
[1] 境界体系？  (1)练气-筑基-金丹-元婴  (2)炼体-通脉-宗师  (3)自定义
[2] 世界名？    (1)落霞界  (2)自定义        ← 回车取 (1)
[3] 势力？      (1)三宗两门一皇朝  (2)自定义
[4] 铁律？      (1)云纹令只认陆氏血脉  (2)自定义
──────────────────────────────────────────
回车=全用推荐值 | 输入 "3 xxx" 改某一项 | Q=结束问答（剩余用推荐值）
```

**成本**：候选批量生成，**每轮 1 次 LLM 调用**（不是每题一次），共 3–4 次。
轮次边界也是**保存点**：中断后 `forge resume` 从下一轮继续。

**自由答案**（回答超出候选，如候选修仙/都市而用户答「东方蒸汽朋克」）：直接采纳，
`src=user`、confidence=1.0；同时检查 Genre Pack 库——无对应类型包则装载**通用包**，
并把用户的类型描述注入全部后续节点 prompt（§8）。

**非交互环境**（无 TTY：CI/测试/管道）：`interactive` **自动降级为 `auto`**（全部取
推荐值），打印一行告警并写入 transcript。AG3 用例依赖此行为。

### 5.4 全权模式

跳过全部问答，槽位按 `template 默认 → 模型推断` 填充，`provenance.src` 记 `template`/`llm`。
构建结束后**必须**出一份 review 清单（缺口 + 低置信项），用户可直接编辑 `blueprint.json`
再 `forge build` 重跑。这样"全权"不是黑箱，只是把交互挪到了后面。

---

## 6. 模式二 `ingest`：已有稿子 → 蓝图（+ 接着写）

用户拍板：**保留为正 + 记忆初始化**，即真正的"从 N+1 章接着写"。

### 6.1 摄入与切片

- 输入：目录或文件列表（`--recursive`），支持 `.md`/`.txt`。
- 切章：优先识别 `第X章` / `Chapter N` 标题行；识别不到则按空行分段后按 `chapters_per_volume`
  的目标字数切。
- **切章确认 = 预览表 + 可编辑**（2026-09-01 拍板）：列出编号预览表（章号、标题/首行、
  字数）；回车=全部确认 / 输入切错的章号=手工指定该处边界 / `R`=重切（换切章正则或
  分隔符）。未确认前不写库，切错可完全重来。

### 6.2 抽取（确定性优先，LLM 补语义）

| 抽取对象 | 确定性手段 | LLM 补充 |
| --- | --- | --- |
| 人物 | 中文人名正则 + 频次统计 + 称谓表（师姐/长老/门主） | 性别、境界、性格三词、关系、别名 |
| 境界/力量体系 | 流派模板词表匹配（练气/筑基/金丹…） | 体系说明、机制规则 |
| 地点/势力 | 后缀词表（`宗/门/城/谷/殿`）+ 共现 | 简介、立场 |
| 物品/功法 | 后缀词表（`丹/剑/诀/经/符`）+ 共现 | 用途、归属 |
| 事件 | —— | 每章 3–5 条 `key_events`（作为已有章节的细纲） |
| 定时约定 | 时间量词启发式（`N日后/闭关N月/N年之后`） | 落为 `worldstate.pending[]`（ADR-019：已写正文中的"闭关三月后出关"类未来约定，接着写时才会到点触发） |
| 伏笔 | 疑问句/悬置陈述启发式 | 待回收线索、`scope`、`target_vol` |
| 对白归属 | 引号 + 说话人动词（道/喝道/笑道） | —— |

**每片一次 LLM 调用**（结构化 JSON 返回），片长按 `max_context` 的 60% 切，超限再切。

**超配额降级**（2026-09-01 拍板）：抽取调用独立配额 `--ingest-max-calls`（默认 30）。
超出后剩余章节降级为**纯确定性抽取**（正则/词表继续抽人名/境界/物品，LLM 语义部分
标「未抽取」），report 披露哪些章未做语义抽取。

### 6.3 归并消歧

复用 `core/entity.py` 的别名归一化思路：人名/称谓/别名合并为同一 `char:` 键；
`叶岚/叶师弟`、`五五开系统/系统` 必须合成一个（第八批教训：合并错了后面全错）。
合并后按出现频次排序，**频次 ≥ 阈值**的进主线人物，其余进配角。

### 6.4 文风画像

确定性指标（句长均值/方差、对白占比、段落长度、语气词频、专名密度）+ LLM 归纳 `tone`。
产出的 `style.json` 带 `provenance.src=ingested`，商讨时**只 confirm 不重问**。

### 6.5 卷章编码 + 记忆初始化

1. 已有 N 章按 `chapters_per_volume` 编入卷 1..k，写入 `chapters/<vol>-<ch>.md`（**已是正式章节**）；
2. 每章的 `key_events` 反写为 `outline/chapters/<vol>-<ch>.md` 细纲，标 `done=true`；
3. **记忆初始化**：复用 `core/chronicler.py` 对每章抽一次事件/经历/关系写入 `memory/`，
   重建索引（`MemoryIndex.rebuild`）。**没有这一步，第 N+1 章的"先忆"是空的**
   （`cli.py:293 _recall_lines` 会拿到空列表），接着写必然断裂。
4. **实体进度 warm-up**（2026-09-01 补的真实缺口）：用已有正文逐章跑
   `EntityTracker.update_from_chapter`（纯正则、零 LLM 成本）重建
   `bible/entity_progress.json`。不做的话第 N+1 章会把**所有老人物当首次登场**
   （`needs_expansion` 要求展开介绍、开篇配额误判、deferred 错误推迟）。
   与第 3 步 chronicler 同批执行。
5. `bible/worldstate.json` 初始化为第 N 章末状态（谁在哪、什么境界、什么伤势）。

### 6.6 缺口检测 → 回落商讨

对槽位表做覆盖度检查，输出缺口清单，例如：
`缺：境界体系完整层级（只有 3 层）、缺：世界铁律、缺：卷 2 及以后的主线、缺：结局走向`。
**只问缺口涉及的槽位**，分组规则同 §5.3，默认从"已写内容已能推断"的项里剔除。

---

## 7. 递归构建引擎（本层核心）

用户拍板：**递归生成，每一步由模型判断是否需要进一步细化**——
接受更多次调用换更高质量（与第七批"分层深化"同宗旨）。

### 7.1 构建树

```
L0 book（唯一根）── 主题/卖点/基调/规模
 ├── L1 volume ───────── 卷主线
 │    ├── L2 arc（可选）── 3–5 章的章段小弧
 │    │    └── L3 chapter ── 细纲（key_events / 出场人物 / turns）
 │    │         └── L4 beat（可选）── 重场戏的"拍"级提示
 │    └── L3 chapter（模型判定无需 arc 时直连）
 ├── L1 worldview ── 世界名/铁律
 │    └── L2 system ── 力量体系 / 势力 / 地理
 │         └── L3 setting_entry ── settings.json 条目
 ├── L1 character_group ── 角色阵容规划
 │    └── L2 character ── 人物卡（主线人物可再细化一次：骨架 → 完整卡）
 ├── L1 style ── 文风
 │    └── L2 glossary/banned（可选）
 └── L1 thread_set ── 伏笔布局（可下放到各卷）
```

**卷闸门**（2026-09-01 拍板，B2）：构建期只展开 **vol=1** 的 chapter 节点——卷 2+
尚无实际剧情可参考，凭空生成细纲质量低（第七批"分步喂前一步"教训）。全书卷主线
（volumes.json）仍一次出齐。后续卷细纲由 **`novelist forge roll <vol>`** 显式生成：
写到卷末章时 CLI 打印提示，`roll` 在前一卷已有正文与记忆的条件下滚动生成该卷细纲。

### 7.2 节点协议

**输入**（拼装进 prompt）：父节点 artifact + 祖先摘要 + 蓝图相关切片 + 流派模板 +
（商讨模式）用户对同类槽位的答案 + 已定稿的兄弟节点摘要。

**输出**（严格 JSON，解析失败按 §12 降级）：

```jsonc
{
  "artifact": { ... },                 // 本层产出（可为 null，表示只做分解）
  "decide": "done | expand",           // 模型自判：定稿 or 继续细化
  "reason": "……",                      // 必填：为什么这么判（审计 + 判据引导）
  "children": [ {"id": "arc-1", "brief": "……", "focus": "……"} ],  // expand 时给出，≤ max_width
  "open_questions": [ {"key": "…", "candidates": ["…","…"]} ]      // 想问用户的（商讨模式收集）
}
```

**判据引导**（写进 prompt，让"模型判断"有锚而不是随机）：

| 节点 | 建议 expand 的信号 | 建议 done 的信号 |
| --- | --- | --- |
| volume | 卷内章数 > 8，或含 ≥3 个关键转折 | 章数 ≤ 8 且主线单一 |
| arc | 该章段跨越多场景/多势力 | 一场景一冲突的过渡段 |
| chapter | 标记为重场戏（交锋/揭秘/大战） | 日常推进章 |
| system | 体系有 ≥2 个子维度（等级 + 派系 + 资源） | 单一线性等级 |
| character | 主线人物（需完整卡） | 阶段配角（骨架即可，JIT 期再补） |

### 7.3 防失控：模型判断 + 引擎硬边界

引擎不盲信 `decide`，四道硬约束（第七批"防失控三原则"在构建层的具体化）：

| 约束 | 默认值 | 触发行为 |
| --- | --- | --- |
| `max_depth` | 4（book→volume→arc→chapter→beat 共 5 层） | 到底层强制 `done`，丢弃 `children` 并记 reason |
| `max_width` | 4 | 超出截断前 4 个，记 warn |
| `max_calls`（**分阶段配额**，2026-09-01 拍板） | build 卷 1 = **60**；`forge roll` 每卷 = **40**；ingest 抽取 = **30**（`--ingest-max-calls`）。用户明示「可接受更多调用换质量」，卷 1 由 40 放宽至 60 | 各阶段独立计数、独立兜底（模板/上层摘要填充，报告标"预算耗尽"）；CLI `--max-calls` 可整体覆盖 |
| 每节点 `max_retries` | 1 | 解析/校验失败重试一次，再失败**回退父层产物**（检查点兜底），绝不静默跳过 |

**每个节点产出必须过校验才落盘**：schema（`schemas/**`）+ 交叉引用（见 §9 V2）。
校验不过 → 重试 → 回退。**未通过校验的中间态不写入 bible/outline**。

### 7.4 调度与续跑

- **深度优先（DFS）**，完一个分支再走下一个：保证同卷内章与章之间的因果连续。
- **增量落盘**：每个节点 artifact 落盘同时写 `nodes/L?-*.json`，`state.calls_used` 递增。
- **续跑**：`forge resume` 读 `project.json.forge.stage` + `nodes/` 已存在产物，
  跳过已完成节点，从断点继续。幂等：重跑同一节点覆盖写，不产生重复条目
  （人物/伏笔按 id upsert）。
- **进度**（2026-09-01 拍板）：每个节点完成打一行
  `[12/60] L3 chapter 1-5 玉牌现世 … ok (calls=12, 2.3s)`；`--verbose` 追加
  decide/reason/children 摘要。
- **中断**：SIGINT（Ctrl+C）在**当前节点结束后**落盘退出，不硬杀；被中断的节点
  不落盘、`calls_used` 不回退，`forge resume` 从该节点重跑。

### 7.5 各节点 → 目标文件

| 节点 | 写入 |
| --- | --- |
| book | `blueprint.meta`（回写） |
| volume | `outline/volumes.json`（upsert by vol） |
| chapter | `outline/chapters/<vol>-<ch>.md`（含 front-matter） |
| worldview / system | `bible/worldview.json` |
| setting_entry | `bible/settings.json`（upsert by id） |
| character_group / character | `bible/characters.json`（upsert by id） |
| style | `bible/style.json`（含 protagonist） |
| thread_set | `bible/plot_threads.json`（upsert by id） |
| （非节点，build 末尾一步） | `bible/worldstate.json` **确定性合成**（2026-09-01 拍板）：`time = {now: 0, origin_text: meta.time_origin}`、characters 初始状态由蓝图 `power/arc/faction` 合成、pending/unavailable 为空——**零 LLM 调用**，不进构建树 |

### 7.6 重跑、修订与回滚（2026-09-01 拍板）

| 场景 | 行为 |
| --- | --- |
| 手改 `blueprint.json` 后重跑 `forge build` | **provenance 保护**：`src=user` 的字段永不被模型产物覆盖；模型只能填空槽、或覆盖 `src=llm/template` 的低置信槽。重跑是安全的增量操作 |
| 写到一半改设定（人物性格/伏笔/文风） | **影响分析子树重建**：`forge build --diff` 比对蓝图改动 → 定位引用被改字段的已生成产物 → 只重建受影响节点（如引用该人物的章细纲）。已写正文不动，差异由一致性引擎与后续章节消化 |
| 项目已有 `chapters/`（已 ingest 或已写章）时跑 seed/build | 默认**拒绝**并提示原因；`--force` 才执行 |
| 构建前快照 | build/roll 前自动打 checkpoint（复用双轨快照）；`forge rollback` 回退到构建前状态 |

### 7.7 `forge roll <vol>` 的上下文注入清单（2026-09-01 拍板）

滚动生成第 N 卷细纲时，prompt 注入四块（贴合"已发生的事实"，这正是滚动优于一次生成的理由）：

1. **前卷主线**：`volumes.json` 中第 N−1 卷的 summary + threads_to_payoff 兑现情况；
2. **前卷末 3 章记忆**：memory 检索（`KnowledgeBase.retrieve`）取实际发生的事件/经历，而非规划态；
3. **worldstate 现状**：当前 time.now、人物状态、未回收 pending（ADR-019：到点该引出的定时事件）；
4. **收尾清单**：第 N 卷 `payoff_checklist`（`phase.py:173`）——本卷必须回收的伏笔。

---

## 8. 类型包 Genre Pack（数据，非代码；2026-09-01 升级）

`src/novelist/forge/genres/*.json`。**一个类型包 = 一种小说类型的全部构建知识**，
不止默认值——同时驱动商讨候选（`slots`）、模式二确定性抽取（`extract_lexicon`）
与构建节奏（`chapter_rhythm`）：

```jsonc
{ "id": "修仙男频", "genre": "修仙", "aliases": ["仙侠", "修真"],
  "slots": { /* 覆盖槽位表：境界体系候选、类型特有问题（师承/宗门/丹药） */ },
  "extract_lexicon": {         // 模式二确定性抽取词表（§6.2 表格的词表来源）
    "realms": ["练气","筑基","金丹","元婴"],
    "faction_suffix": ["宗","门","城","谷","殿"],
    "item_suffix": ["丹","剑","诀","经","符"],
    "address": ["师姐","长老","门主"] },
  "power_system": {"levels": ["练气","筑基","金丹","元婴","化神"], "mechanic": "…"},
  "character_slots": [ {"role":"protagonist","hint":"…"},
                       {"role":"mentor"}, {"role":"rival"},
                       {"role":"love_interest"}, {"role":"minor","count":4} ],
  "chapter_rhythm": ["opening-hook","setup","conflict","climax","cliffhanger"],
  "default_style": {"pov":"第三人称限知","tone":["热血激昂"],"target_words_per_chapter":2400},
  "default_banned": ["打卡","手机","系统崩溃"],
  "volume_arc_hint": "入门→宗门大比→外域历练→宗门危机→真相揭露" }
```

**装载时机**：seed 提炼出 `genre` → 按包名/`aliases` 匹配 → 装载对应包；
**库内无此类型 → 装载通用包**（`通用.json`：候选泛化、词表为空、可写任何类型），
并把用户的类型描述注入全部节点 prompt（§5.3 自由答案的兜底）。

首版 2 包：**修仙男频** + **通用**。新增类型 = 加一个 JSON，代码零改动
（项目既有约定：`换类型只需重写 bible`）。

---

## 9. 契约校验与定稿（Forge Validator）

| 编号 | 检查 | 失败后果 |
| --- | --- | --- |
| V1 | **双层 schema 校验**（2026-09-01 拍板）：文件层（新增 `schemas/file/*.schema.json` 描述 list/object 形态与必含键）→ 条目层（既有条目 schema 逐条）。前置修复 4 处差异：characters +`background`/`is_protagonist`、style 对齐**扁平**结构、plot_threads/volume 补 array 形态（实测现状 5 文件全 FAIL） | block |
| V2 | 交叉引用：`relationships.target` 存在、`threads.scope/target_vol` 合法、`volumes.chapter_range` **连续无重叠无缝隙**、细纲 `characters[]/threads_involved[]` 指向存在的 id | block |
| V3 | 覆盖度：每章 ≥1 个 `key_events`、主角在 vol1 ch1 出场、settings ≥ N 条、每卷 `threads_to_payoff` 非空、`style.protagonist` 与 characters 一致 | block |
| V4 | **可写冒烟**（`--smoke`）：用 `FakeProvider` 跑 `produce_chapter(1,1)`，断言 `ok` 且 `bible_injected=True` 且 cast 非空 | block（这是"完全符合下一层需求"的可执行判据） |
| V5 | 质量提示（非阻断）：单章 key_events 数、新实体密度、卷末未回收伏笔数 | warn，写入 report.md |
| V6 | 叙事质量软检查（2026-09-01 拍板：**全部确定性规则，零 LLM、零配额消耗**）：章间因果链=相邻章 key_events/characters/threads **承接重合度**（零重合 warn"疑似断章"；两章 `pov` 不同豁免——刻意换线不算断）；卷内节奏曲线=turns 序列统计（连续 5+ 章无 conflict/climax warn"平缓"、相邻双 climax warn"过密"）；伏笔密度=卷中点 planted:paid_off > 5:1 且零回收 warn"堆积"；实体密度=对照 PhasePolicy 配额（复用 `budget_check` 表） | warn，写入 report.md |

通过后：`pipeline_state = 细纲`，报告**双写**（2026-09-01 拍板）——全量写
`workspace/forge/report.md`（供 resume/build 引用），摘要写
`reports/stats/forge-<时间戳>.md`（顺带解决 reports/ 零写入遗留）。内容：来源分布 /
缺口 / 校验结果 / 调用数与耗时 / **token 用量与估算成本**（transcript 每条 build_node
记 usage）/ 每个节点的 `decide` 与 `reason` 摘要。**不通过则不推进状态**，
报告里给出可修的具体条目。

---

## 10. 输出契约总表（本层必须满足的下游需求）

| 产出文件 | 关键字段 | 消费方（代码位置） |
| --- | --- | --- |
| `project.json` | `genre`、`pipeline_state=细纲`、`forge` | `cli.py:240` 传 `genre`；`context.py:161` |
| `bible/worldview.json` | `name`、`power_system.levels`、`rules`、`factions`、`phase_policy` | `context.py:153/154/172`；`phase.py:122`（phase_policy 覆盖） |
| `bible/style.json` | `pov`/`tense`/`narration`/`tone[]`/`target_words_per_chapter`/`forbidden_words`/`glossary[{term,note}]`/`protagonist{id,name,gender}` | `context.py:183-202`；polish 读 tone |
| `bible/characters.json` | `id`/`name`/`aliases`/`gender`/`core_traits`/`power.level`/`arc`/`first_appear{vol,ch}`/`status`/`relationships[]` | `context.py:114-118`（过滤与登场判定）、`209-211`（名单行）、`entity.py`（别名） |
| `bible/settings.json` | `id(set:*)`/`keywords[]`/`text`/`revealed`/`first_ch` | `knowledge.py:73-78`、`entity.py` |
| `bible/locations|items|skills.json` | `id`/`name`/`category`\|`faction` | `knowledge.py:128/139/150` |
| `bible/plot_threads.json` | `id(pt:\|thread:)`/`desc`/`scope`/`target_vol`/`status` | `phase.py:173 payoff_checklist`、R-THREAD 规则、chronicler |
| `outline/volumes.json` | `vol`/`title`/`chapter_range[起,止]`/`summary`/`threads_to_payoff`/`target_words` | `phase.py:71`（卷内进度/收尾期）、`context.py:164-167`（卷主线注入） |
| `outline/chapters/<vol>-<ch>.md` | 首行 `# 第X章 标题`、`key_events:[…]`、`出场人物:[…]`、`turns`、`threads_involved` | `context.py:87 parse_key_events`、`orchestrator.py:577 _jit_characters`、`context.py:121`（细纲点名） |
| `bible/entity_progress.json`（模式二） | warm-up 产物：老人物进 established | `entity.py`（needs_expansion / 配额 / deferred） |
| `bible/worldstate.json` | **构建期确定性合成**（§7.5）：`time{now,origin_text}`、characters 初始状态、pending 空 | T2 临近事项注入、R-STATE、R-TIME（ADR-019） |
| `memory/*`（模式二） | 事件/经历/关系/索引 | `cli.py:293 _recall_lines` → `produce_chapter(memories=)` |
| `chapters/*`（模式二） | 已有正文 | 回读（`--readback`）、导出（`core/export.py`） |

---

## 11. CLI 与模块布局

```bash
# 模式一
novelist forge seed "一句话创意" [--dir DIR] [--mode auto|interactive]
    [--provider fake|lmstudio|deepseek|openai] [--genre-pack 修仙男频]
    [--volumes 3] [--chapters-per-volume 20] [--target-words 2400]
    [--max-calls 60] [--max-depth 4] [--max-width 4] [--smoke]

# 模式二（--discard 首版不做）
novelist forge ingest <path...> [--recursive] [--mode auto|interactive]
    [--chapters-per-volume 20] [--ingest-max-calls 30]

# 通用
novelist forge show <dir>              # 打印蓝图 / 缺口 / 进度 / 调用数
novelist forge resume <dir>            # 断点续跑（问答或构建）
novelist forge build <dir> [--diff] [--force]
                                      # --diff = 影响分析子树重建；--force = 已有 chapters 时强制
novelist forge roll <dir> <vol>        # 滚动生成第 N 卷细纲（写到该卷前执行）
novelist forge rollback <dir>          # 回退到上次构建前快照
novelist forge validate <dir>          # 只跑 V1–V6
```

模块（**实现状态**：✅=F0 已建；其余待 F1–F5）：

```
src/novelist/forge/
  __init__.py      # 对外：show_summary（F0）；run_seed / run_ingest / build / validate 待 F1–F5
  state.py         # ✅ ForgeState + Blueprint 读写 + provenance（docs/08 F0 落地记录）
  slots.py         # ✅ 槽位表 + 分组 + 缺口检测（确定性，零 LLM）
  genres.py        # ✅ Genre Pack 装载（id/别名匹配，通用兜底，装载即自身 schema 校验）
  ask.py           # 问答协议（引擎侧：生成问题、解析回答、transcript）—— F2
  io_console.py    # 问答通道的终端实现（接口化，便于日后换 HTTP）—— F2
  seed.py          # 模式一：种子提炼 + 授权询问 —— F1
  ingest.py        # 模式二：切片 / 抽取 / 消歧 / 文风画像 / 卷章编码 / 记忆初始化 —— F3
  engine.py        # 递归构建引擎（调度 + 边界 + 落盘 + 续跑）—— F1
  nodes.py         # 各节点类型的 prompt 装配 + artifact 解析 + 落盘 —— F1
  genres/          # ✅ 修仙男频.json / 通用.json（Genre Pack，数据；新增类型=加 JSON 零改码）
  validate.py      # V1–V6 + 报告生成 —— F5
  report.py        # report.md 渲染 —— F5
```

---

## 12. 失败与降级

| 失败 | 处置 |
| --- | --- |
| LLM JSON 解析失败 | 重试 1 次（prompt 加"只输出 JSON"硬约束）→ 仍失败则**回退父层产物**并记 warn；连续 3 个节点解析失败 → 中止并提示换 provider |
| 供应商审核拦截（ADR-015） | 复用既有 `ModerationBlockedError` 链路：改写措辞重试 → 仍失败则该节点标 `blocked`，报告列明，人工补 |
| 递归到 `max_calls` | 剩余节点用模板默认值 + 父层摘要兜底填充，报告顶部标红"预算耗尽，以下条目为兜底值" |
| 用户中途退出商讨 | 已答写入 transcript，未答按推荐值填充，`forge resume` 可继续 |
| 已有章节切章错误 | 切章后强制一次确认（§6.1），错了可重切（不写库即可重来） |
| ingest 抽取超配额 | 剩余章节降级纯确定性抽取，report 披露未做语义抽取的章（§6.2） |
| 非交互环境下选了 interactive | 自动降级 auto + 告警，写 transcript（§5.3） |

---

## 13. 测试策略（docs/09 §2.1：单测绝不真调 LLM）

| 层 | 用例 |
| --- | --- |
| `slots.py` | 缺口检测：给定蓝图和槽位表 → 精确输出待问槽位（确定性，无 LLM） |
| `ask.py` | 分组与轮次；"回车取推荐值"、"自定义"、"Q 退出"三种回答的解析；transcript 续跑 |
| `seed.py` | `ScriptedProvider` 喂固定 SeedSpec → 断言蓝图字段与 provenance |
| `ingest.py` | 3 章样章 → 断言抽出 ≥N 人物、别名合并正确、记忆非空、`chapters/` 已落盘 |
| `engine.py` | 边界测试：`decide=expand` 超 `max_depth`/`max_width`/`max_calls` 时正确截断；校验失败回退父层产物；DFS 顺序 |
| `validate.py` | V2 交叉引用故意造错（引用不存在的 char:、章号有缝隙）→ 必须 block |
| **端到端（验收）** | **AG1**：一句话 + FakeProvider → 项目可直接 `chapter 1 1` 且 `bible_injected=True`、cast 非空；**AG2**：3 章样章 → 第 4 章起可接写且 `_recall_lines` 非空；**AG3**：商讨模式 12 问内收敛；**AG4**：构建调用数不超 `--max-calls` |

---

## 14. 里程碑拆分

| 里程碑 | 内容 | 产出 |
| --- | --- | --- |
| **F0' schema 对齐**（前置） | `schemas/file/*.schema.json` 文件层 + 4 处字段差异修复 + **`schemas/forge/blueprint.schema.json` 与 `genres.schema.json`**（2026-09-01 补：蓝图与类型包是新文件格式，V1 须校验自身中间态，手改打错键名不能等到 build 才爆） | 既有项目（proj-t5）全部 bible/outline 文件过 V1；蓝图/Genre Pack 可校验 |
| **F0 骨架** | `state.py` + `slots.py` + Genre Pack 装载 + `forge show` | 能加载/编辑/校验蓝图（无 LLM） |
| **F1 模式一（全权）** | `seed.py` + `engine.py` 最小树（book→volume→chapter，**卷闸门 vol=1**）+ 落盘 + provenance 保护 | 一句话 → bible + volumes + 卷 1 细纲 |
| **F2 商讨** | `ask.py` + `io_console.py` + transcript 续跑 + 授权询问 + 非 TTY 降级 + 自由答案 | 分轮问答可用，可中断续跑 |
| **F3 模式二** | `ingest.py`：切章预览 / 抽取（超限降级）/ 消歧 / 文风 / 卷章编码 / 记忆初始化 / **实体 warm-up** + 缺口回落 | 已有稿子 → 接着写 |
| **F4 递归深化 + 滚动** | 旁支节点（worldview/character/style/threads）+ 可选 arc/beat 层 + `forge roll` + Genre Pack 扩充 | 卷 2 细纲滚动生成，质量提升 |
| **F5 定稿** | `validate.py` V1–V6 + 冒烟 + report 双写 + rollback / --diff + pipeline 推进 | A1 可验收 |

依赖顺序：F0' → F0 → F1 → F2（F1 之后即可跑通主链路）；F3 与 F4 可并行推进；F5 收口。
**与 M3m 的顺序**（2026-09-01 拍板）：**M3m（时间线 T1–T3）先行**——F3 的 ingest 记忆初始化依赖
chronicler 的"时间：/约定："行（T1 产物），先接泵再摄入，ingest 后 `time.now` 才是真实累计值。

---

## 15. 已拍板 / 待确认

**已拍板（2026-09-01，三轮敲定，全部分支闭合）**：

设计主干（第一轮）：

1. 商讨形态 = 分轮分组问答（3–4 轮 × 2–4 问，单屏呈现，可回车取推荐值、可 Q 退出）
2. 模式二 = 已有章节**保留为正 + 记忆初始化**，从 N+1 章接着写
3. 大纲 = **递归生成，每步由模型判断是否需要细化**（接受更多调用换质量）
4. **不**建 `core/subagent.py`，Forge 自成一体的状态机

第二轮（阻塞项 + 全部分支）：

5. **schema 对齐 = 文件级 + 条目级双层**：新增 `schemas/file/*.schema.json`；V1 先文件层后条目层；修复 4 处字段差异（B1，实测现状 5 文件全 FAIL）
6. **后续卷细纲 = `forge roll <vol>` 显式命令**；构建期卷闸门只展开 vol=1，写到卷末章 CLI 提示（B2）
7. **`role` 是蓝图内部字段、落盘剥离**；`character.role=protagonist` 为主角唯一源，派生 `style.protagonist` + `is_protagonist`（B3）
8. **预算 = 分阶段配额**：build 卷 1 = 60、roll 每卷 = 40、ingest 抽取 = 30；用户明示「可接受更多调用换质量」，卷 1 由 40 放宽至 60
9. **重跑 = provenance 保护**：`src=user` 字段永不被模型产物覆盖
10. **修订 = 影响分析子树重建**（`forge build --diff`）；已写正文不动，差异由一致性引擎消化
11. **护栏 = 已有 chapters 时默认拒绝**，`--force` 强制；build/roll 前自动 checkpoint，`forge rollback` 可回退
12. **V6 叙事质量软检查**（章间因果链/节奏曲线/伏笔密度/实体密度），全 warn 不阻断
13. **成本 = transcript 记 token usage**，report 汇总用量与估算成本
14. **进度 = 每节点一行**（`[12/60] … ok`）；SIGINT 在当前节点结束后落盘退出
15. **report 双写**：`workspace/forge/report.md` 全量 + `reports/stats/forge-<ts>.md` 摘要
16. **非 TTY 自动降级 auto** + 告警（AG3 依赖此行为）
17. **自由答案直接采纳**（`src=user`、confidence=1.0）；类型不在库内 → 装载通用 Genre Pack
18. **类型包 Genre Pack 机制**（替代"模板库"概念）：槽位候选 + 抽取词表 + 节奏 + 默认文风，一个 JSON 一个类型，首版 2 包（修仙男频 + 通用）
19. **写入通道 = 直接原子写**白名单路径，不走 tools/registry 门禁（那是为 LLM 自主工具调用设计的风险面）；每次写入记 transcript
20. **切章确认 = 预览表 + 可编辑**（回车确认 / 指定边界 / R 重切）
21. **ingest 超配额降级确定性抽取**，report 披露未做语义抽取的章
22. **--discard 首版不做**（F3 只做"保留为正"）

第三轮（完备性复核，2026-09-01，7 处缝隙闭合）：

23. **worldstate.json 由蓝图确定性合成**（模式一 build 末尾一步，零 LLM）：`time={now:0, origin_text: meta.time_origin}`、人物初始状态从 `power/arc/faction` 合成；不设递归节点
24. **执行顺序 = M3m（T1–T3）先行**，再回 F0'→F1→…（T1 的时间行是 F3 ingest 的前置）
25. **V6 全部确定性规则、零 LLM**：因果链=承接重合度（pov 不同豁免）、节奏=turns 统计、伏笔=比例阈值、实体=配额对照；后续可选 `--v6-llm` 升级
26. **R-TIME 软 block + 自动过期**：block 语义=本章 key_events 须引用该 pending；连续 3 次 block 自动转 `expired` 放行，report 与告警留痕；`cancelled` 由用户手改 worldstate
27. **blueprint / Genre Pack 自身进 F0'**：`schemas/forge/blueprint.schema.json` + `genres.schema.json`
28. **roll 注入清单**（§7.7）：前卷 summary + 前卷末 3 章记忆 + worldstate 现状 + payoff 清单
29. **unavailable_until 触发词表放 Genre Pack**（`unavailable_states` 字段，通用包给基础词表：闭关/失踪/昏迷/被囚/渡劫）

**待确认**：无。全部分支已闭合，可进入 M3m T1 编码。
