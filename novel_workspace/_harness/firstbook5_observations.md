# 新书 5 章测试观察（proj-20260903194907 · DeepSeek · 通用包）

> 目的：模型能力足够（deepseek-chat）时，暴露 Novelist 系统设计缺陷 + 实测成本。
> 书：都市修仙（灵气复苏回归流），主角李天劫，1 卷 × 5 章 × 2400 字。
> 命令面：forge seed --smoke → forge build → forge validate --smoke → chapter 1-5。
> 环境真相：可用解释器 = `/e/python/ana`（3.13.9，Anaconda）；C:/Python314 为裸解释器（工作记忆"系统解释器 C:/Python314"过时）。

## 已确认缺陷 / 发现（构建与校验阶段）

### D1. DeepSeek json_object 400：prompt 未含 "json" 字样
- 现象：`L1 worldview`、`L1 character_group` 节点 openai http 400
  `Prompt must contain the word 'json' to use response_format of type 'json_object'`，
  节点回退父层产物。
- 根因：DeepSeek 的 json_object 模式要求 prompt 内含 "json" 字样（OpenAI 无此约束），
  该节点 prompt 模板缺 json 引导词（跨模型差异未适配）。
- 影响：worldview 节点回退 → 复用 book 输出（实际质量尚可：rules/factions 有料，
  但 power_system.levels=[] 空）；character_group 回退 → 无角色分组增量。

### D2. forge build 不产 settings.json，V3（≥5 条）必挡
- 现象：validate `[V3] settings 仅 0 条（要求 ≥ 5）——知识库检索会空转`。
- 根因：bible/settings.json 由 enrich（M3r 26 卡）产出，forge build 节点序列
  （book/style/thread_set/volume/chapter）无 settings 节点 → build 后直接 validate
  必然 block。流程衔接断点：build → enrich → validate 还是 build → validate → enrich？
  （yl3 ingest 模式 settings 由 ingest/enrich 来，seed 模式路径存疑）

### D3. items.json 条目缺必填 `type`
- 现象：`[V1] schema bible/items violated at 1: 'type' is a required property`
- 根因：通用包 item 生成未填 type（修仙男频包词表可能兜底了 type，通用包无）。
- 影响：schema block，V1 校验不过。

### D4. 章细纲 threads_involved 引用不存在线程 id
- 现象：`[V2] 细纲 1-4 threads_involved pt:jade_talisman 不存在`
  （plot_threads 实际 7 条 pt:1..pt:7）。
- 根因：thread_set 节点输出 id 体系（pt:N）与 chapter 节点引用 id（pt:jade_talisman
  语义名）不一致——跨节点共享 id 的契约未收敛。

### D5. title 字段 = 原始 brief 整句（种子提炼）
- 现象：blueprint meta.title = "都市高武与修仙结合：大学生李天劫（男，23岁），"
- 根因：seed 提炼把 title 槽位用 brief 首句直填，未做"书名化"摘要。
- 影响：project.json title 与章标题无关（章标题由延迟拟题正常产出）。

### D6. CLI embedding 无法指定本地 nomic 模型
- make_embedding 只认 keyword/openai，openai 分支 model 硬编码 text-embedding-3-small，
  无 env/config 覆盖 → LM-Studio 本地 nomic 不可达（会 400）。CLI 通道 recall 只能
  keyword-fallback。语义检索在 CLI 路径不可用（harness 直调 produce_chapter 可传 nomic）。
- 本测试按真实使用路径走 keyword-fallback，观察 recall 空转影响。

## 正向观察
- seed 提炼 logline 精准；规模 1×5×2400 正确；模板=通用（genre-pack 强制生效）。
- worldview 回退产物 rules 自洽（含"高等级修士不干涉凡人"天罚规则）；factions 成型。
- 角色 6 张（主角/mentor/rival/love/minor×2）由通用包 character_slots 兜出。
- 细纲 1-1 标题"重生与开学"，key_events 具体、出场人物齐。
- V4 可写冒烟通过（FakeProvider 直出 ch1）。
- build 成本：11 calls / 42s（DeepSeek，快且便宜）。

## 5 章正文实测（DeepSeek deepseek-chat，ALL OK）

| 章 | 秒 | 字数 | 拟题 | 完整 | 事件 | 视角 | 审校block | AI味 | in/out tokens | ¥ |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 158 | 6095 | 一掌拍碎演武石 | ✓ | 4 | 12 | 4 | 24.4→20.4 | 42.7K/16.6K | 0.076 |
| 2 | 179 | 5089 | 咸鱼摊上校花局 | ✓ | 5 | 17 | 3 | 9.1→9.0 | 76.9K/18.5K | 0.114 |
| 3 | 198 | 7714 | 数学书砸翻黑衣人 | ✓ | 4 | 10 | 3 | 17.9→13.3 | 62.3K/17.5K | 0.097 |
| 4 | 200 | 6014 | 玉符认主暗纹惊现 | ✓ | 6 | 9 | 3 | 15.2→15.2 | 66.1K/20.3K | 0.107 |
| 5 | 118 | 3692 | 苏老的茶与暗影的名单 | ✓ | 4 | 6 | 2 | 9.1→9.1 | 42.5K/11.5K | 0.066 |
| Σ | ~14min | 28.6K字 | — | 5/5 | 23 | — | 15 | — | 290.5K/84.4K | **0.459** |

成本：forge build 11 calls 8.9K/4.8K ≈ ¥0.019；正文 ¥0.459；章后 Reviewer 审校 ×5 未记账（估 ¥0.07）。
**全流程（一句话→5章正文+审校）实测 ≈ ¥0.48，含审校预估 ≈ ¥0.55。单章正文 ≈ ¥0.09-0.11。**

### 关键正向验证
- **ADR-023 账本端到端生效**：8 对关系、flip_count=3（提案入 enrich pending）。
  例：love_interest|protagonist "决定暗中护她 升温 events=12"；mentor|protagonist
  "仍存提防但接受合作 转向 events=11"。N4 必答词表（今天上午改）在真书收到增量。
- 5 章全部完整结尾、零元叙事、零截断；正文有真实网文质感（比喻/口语/节奏），
  非 AI 八股（DeepSeek + polish 链路工作）。
- 延迟拟题自然（一掌拍碎演武石/咸鱼摊上校花局/数学书砸翻黑衣人）。
- worldstate 状态人 2-3/章、事件回写 4-6/章正常。

### 正文段新发现（审校 15 block 归类）
- D8. **战力"隐藏实力"误报**：bible 无"实际战力>>表面修为"机制字段，主角扮猪吃虎
  （练气三层一掌拍飞筑基）被 Reviewer 判"战力越级/设定矛盾"block×3。作者爽点 vs
  一致性引擎冲突——需 bible 补 hidden_power 类字段或 reviewer 上下文。
- D9. **细纲未覆盖 block×若干**：正文按节奏改写了细纲事件顺序/合并（如苏老邀请
  变两次、王傲天放学拦人挪到操场）→ 审校逐事件比对过死。细纲 vs 正文允许偏差
  的阈值问题（小改算不算"未覆盖"）。
- D10. **时间轴小矛盾**：ch2 开头承接与 after_days/前情提要的回溯表述有出入
  （轻度，1-2 处，worldstate.time 未随章推进问题的表现之一——见总账下轮 1）。
- D11. **settings=0 的实感影响**：V3 空库下 supplement_settings 全程 0 补（无卡可
  归类/滚动），新名词没沉淀。正文靠 bible 注入撑着没崩——但知识库检索空转确认。

## 定稿待办（用户拍板修哪些）
优先级建议：D1（json 引导词，一处模板 fix）> D3（items type）> D8（hidden_power 或
reviewer 豁免）> D9（细纲偏差阈值）> D2（settings 流程衔接）> D4 > D5 > D6 > D7。
D7（CLI chapter 多项目定位）对多项目工作流影响大，建议改 _locate_project 复用
_resolve_forge_target。

## 人工审查 4 问答复（2026-09-03 晚，用户通读正文后提问；附证据）

### Q1 张胖子 ch2 反复"窜出来"
- 现象：1-2.md L61"不知从哪个花坛后面蹦出来…从一棵万年青后面钻出来"、
  L135"不知从哪棵梧桐树后窜出来"——同章两次同套路句式，且他明明一路跟着主角
  （L3 磨蹭跺脚/L73 缩脖子扯袖）。
- 根因（调度层无事件间位置记忆）：v1-c2-e1.json 调度 how="躲在花坛后面，见王傲天
  被拍飞后蹦出来"；v1-c2-e3.json how="躲在操场边的梧桐树后…小跑过来"。ADR-020
  导演调度**按事件切片独立生成**，给配角的 how 都是"先躲→再蹦出"，切片间不传递
  "他正跟主角同行"的结伴状态（仅主角作为镜头隐式连续）→ 配角被当场景道具每次
  重新布置；正文照单全收 + 模型对"功能位入场"的套路化偷懒句。
- 归属：director 调度层设计缺口（D12 候选）。

### Q2 前世遗留痕迹 + "一年"缺失 + "十年"组织
- 三件实证：
  1. **"一年"在 seed 提炼被改写**：blueprint.json meta.time_origin="灵气复苏后
     **第三年**，李天劫重生之日"（worldstate.time.origin_text 同源复用）。全 5 章
     正文 0 处"一年"；ch1 甚至出现"灵气复苏纪元第17届新生入学须知"——时间纵深
     漂到十几年。
  2. **两界缝合源头在 build 伏笔层，不在正文**：plot_threads.json pt:2 轮回石"与他
     前世有关"、pt:5《九转轮回诀》功法缺失、pt:6 导师"知道重生秘密"；volumes
     key_beats "玉符异动暗示前世线索"。正文 ch4 据此把林婉儿写成身负前世修仙世界
     "千丈灵木传承"（灵木宗/上古修士封印）——蓝星本地人与前世体系同源。blueprint
     worldview.rules 仅 3 条（灵气复苏/天赋/天罚），**无一条"两界隔离"铁律** →
     模型把"前世因果延续到蓝星"当默认悬念手法。
  3. "十年组织"无字面证据（ch4 的"十年"是"推演了三十 年"子串），但观感真实：
     古武世家/协会京都总部/19 岁筑基天才/第17届——组织时间厚度全由"一年锚点丢失
     +模型按复苏多年想象"堆出。
- 系统根因：一句话设定中的**数值锚点（一年）与结构铁律（两界隔离）未被提炼管线
  结构化保留**——blueprint 只有 logline 级复述，worldview.rules 由 LLM 自由生成，
  无"从 brief 抽取硬约束"机制（D13 候选）。

### Q3 渡劫大佬逼格不够
- 归因修正：**"练气三层"确为正文 LLM 即兴**——bible 角色卡 power.level 只写
  "渡劫圆满（前世）"，无练气三层；正文自造"肉身练气三层 + 神魂危急才涌真力"
  （为圆修为不高，实则"一年"约束已丢，模型自洽补的）。bible 的 power 字段**未建模
  "神魂修为 vs 肉身修为"双轨**。
- 但更深的系统压制：ADR-020 directions 每事件 taboo 都写"绝不能露出前世渡劫圆满的
  威压"（v1-c2-e2 主角 taboo 原文）——隐藏实力=每事件硬禁忌，主角永远被动挨打后
  "本能"出手。
- 且细纲 key_events 全为"被动接招"型（王傲天挑战/暗影袭击/协会邀请），无"主角
  主动了断麻烦"的事件（core_traits 有"腹黑"但调度未利用）。
- 结论：逼格不够 = 模型即兴保守双轨 × 调度层每事件禁暴露 × 被动型情节设计，三重
  叠加（D14 候选，含 D8 同源）。

### Q4 开篇交代不足（人物初登场/世界背景）
- 系统侧四层缺失：
  1. worldview.json 缺 summary/civilizations/modern_words，levels=[]（worldview
     节点 D1-400 回退的不完整产物）→ system prompt【世界设定】实际只有世界名+3 铁律。
  2. context.build_system_prompt / user_goal **无"开篇章需向读者交代世界观/背景"
     元写作指令**；orchestrator 开篇阶段判定（:1117）只调篇幅系数×1.3，不要求内容。
  3. 细纲 key_events 直奔冲突（开学暴露实力），无 intro beat 槽位。
  4. characters.json 有 first_appear 字段但**未接入注入面**——首次登场角色无
     "给读者登场镜头"指令。
- 正面：ch1 靠模型网文本能补了几笔主角侧背景（渡劫台/重生/测灵根），但世界侧
  （复苏机制/三方势力/诡异）无交代——bible 压根没存这些内容。与用户"总体质量合格"
  判断一致，属结构性缺口非生成事故（D15 候选）。

## 修复落地（2026-09-03 深夜，用户拍板 D1→D3→D8→D7）

| 项 | 修法 | 落点 | 测试 |
|---|---|---|---|
| D1 | `run_node` 入口 `_ensure_json_hint`：prompt 无 "json" 字样时追加 JSON 引导（只影响缺失调用，成功路径不变） | forge/nodes.py | test_d1_json_hint.py（4） |
| D3 | ①book 协议行 category→type 枚举；②`sync_bible` 导出口 `_normalize_item_type` 中文词表归一化（LLM/ingest 双路径单点收敛，兜底 other） | forge/nodes.py | test_d3_items_type.py（5） |
| D8 | 人物卡 `power.hidden_level` 贯通三处：book 协议按需给字段；审校 brief 渲染+战力维度豁免说明；导演调度卡注明"越级出手是有意设定"。schema 本就 additionalProperties:true，补属性描述文档化 | forge/nodes.py、consistency/reviewer.py、core/director.py、schemas/bible/characters | test_d8_hidden_power.py（4） |
| D7 | 抽 `_resolve_project(ws, directory)`（直指项目目录/根均可，ws 必要时重绑父目录防路径翻倍）；`_locate_project` 收敛为根扫描；`_resolve_forge_target` 委托之；11 处 CLI 调用点统一 | cli.py | test_d7_cli_locate.py（6） |

- **新发现 D16（已顺手修）**：`memory/character_history` schema 的 `relations` 仍要求
  字符串数组，chronicler（ADR-020 N4）实写 `{who, delta}` 对象——schema 滞后于代码，
  6 个人物史文件 V1 全挡。schema 改 anyOf(string | {who,delta})。
- **实测数据修复**：proj-20260903194907 经确定性 `sync_bible` 重写后，items
  （护身玉符→artifact、聚灵丹→consumable）与全部 character_histories 过 V1。
  validate 阻断 10 → 2：剩 V2（D4 线程 id 断裂）与 V3（D2 settings 衔接），
  均在既定范围外，待拍板。
- 全量回归 557 passed / 2 skipped（--ignore=tests/test_http.py；ana 环境无 fastapi，
  test_http 不可收集——环境漂移，与本次改动无关）。

## 第二批修复落地（2026-09-03 深夜续，validate 全绿收口）

| 项 | 修法 | 落点 | 测试 |
|---|---|---|---|
| D4 线程 id 断裂 | ①chapter prompt 注入本书伏笔清单（id+desc 摘要）并"禁止自造"；②apply 层对齐 characters 悬空引用处理——丢弃+warning | forge/nodes.py | test_d4_thread_refs.py（3） |
| D2 settings 空库 | sync_bible 在蓝图 settings 段与磁盘文件双空时，零 LLM 从蓝图实体合成种子卡（worldview/characters/locations/items/skills 各一张，不发明新设定）；enrich 增量不被覆盖。同时缓解 D11（0 补） | forge/nodes.py | test_d2_seed_settings.py（4） |

- 存量数据修复：blueprint.json + 细纲 1-4 的 `pt:jade_talisman` → `pt:2`（暗影组织
  找前世物品，语义对应玉符线）；sync_bible 产 9 张种子卡；Checkpoint checksum 重录
  （外部重写文件属合法修复路径，verify 一致）。
- **validate 全绿**：block 0 / warn 3（V5 伏笔未回收 / V6 堆积与开篇实体超额，均
  非阻断，5 章测试书本身已完结），pipeline 推进「细纲」。
- 全量回归 564 passed / 2 skipped（+7 新测试）。
