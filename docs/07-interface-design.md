# 07 · 接口与协议设计

> 系统对外与内部的接口契约：**LLM Provider 适配器**、**工具协议**、**事件总线**、**对外 API**（CLI/HTTP）。所有接口以"先契约、后实现"的方式定义，是工程实现的约束性输入。

## 1. 接口总览

```
对外 API (CLI/HTTP)  ──►  编排器服务
                           ├─ LLM Provider 适配器接口 ◄── 云 / 本地
                           ├─ 工具注册表 + 权限门禁
                           ├─ 一致性规则引擎
                           └─ 事件总线（审计/进度）
```

## 2. LLM Provider 适配器接口（ADR-003，F5.3）

### 2.1 抽象定义
```python
# 概念接口（实现语言 Python，契约用类型描述）
class LLMProvider(Protocol):
    def complete(
        self, req: LLMRequest
    ) -> LLMResult: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...
```
- 核心层只依赖该抽象，不 import 任何 SDK。
- `ProviderCapabilities` 描述：`tool_calling: bool`、`max_context`、`json_mode: bool`、`streaming: bool`、`embedding: bool`，供降级策略使用（ADR-006）。

### 2.1.1 Embedding 接口（F5.3，记忆 RAG / ADR-011）
```python
class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    @property
    def dim(self) -> int: ...        # 向量维度，供索引建表与切换重建
    @property
    def kind(self) -> str: ...       # "cloud" | "local" | "keyword-fallback"
```
- 可由 `LLMProvider` 附带实现（多数云 API 如此），也可独立注册（本地模型 / 独立服务）。
- **无任何 Embedding 时自动降级**：`query_memory` 退化为中文分词 + 倒排索引检索（07§7.3），接口不变、可用性保留（NFR-9/14，F9.4）。
- Provider 切换导致 `dim` 变化时须重建索引（07§7.3 `reindex_memory`）。

### 2.2 请求 / 响应结构
```json
// LLMRequest
{
  "model": "operator-chosen-or-provider-picked",
  "messages": [{ "role": "system|user|assistant", "content": "..." }],
  "tools": [ {ToolDef} ],            // 仅在 capabilities.tool_calling 时传递
  "temperature": 0.7,
  "response_format": {"type": "json_object" | "text"},
  "budget": {"max_tokens_out": 4000}
}

// LLMResult
{
  "ok": true,
  "content": "text-or-json-as-string",
  "tool_calls": [ {ToolCall} ],
  "usage": {"tokens_in": 1200, "tokens_out": 900, "cost_estimate": 0.004},
  "finish_reason": "stop|tool_calls|length|error",
  "provider": "openai|deepseek|ollama|vllm|...",
  "degraded": false,
  "blocked": false,          // 供应商审核拦截（ADR-015）
  "block_reason": null,      // "safety" | "moderation" | "empty" | ...
  "provider_note": null      // 供应商原始提示/错误（脱敏后）
}
```

### 2.3 适配器实现登记
- 通过**插件注册表**注册：`register_provider(name, factory)`；实例化统一走单一工厂
  `providers.create(name, **kw)`（P0-2 归一，docs/11 §13 P0-2 已闭环）。**禁止在 cli 以 if/elif
  硬编码 provider 分支**——新增 Provider = 实现 `LLMProvider` Protocol + 在
  `providers/__init__.py` 注册工厂（唯一入口），命令层只透传 `--provider/--api-base/--api-key/--model`。
- API Key 来源（`providers/secrets.py`）：**CLI 显式参数 > 环境变量 > 本机 `.env`**
  （gitignored）。`custom` 支持用户手填任意 OpenAI 兼容端点的 base_url + key + model。
- 每个适配器仅在**运行时加载**对应 SDK（可选依赖），避免冷启动加载全部。
- `ProviderCapabilities` 不一致时的降级：例如某模型无 tool_calling → 编排器自动切换为"文本 + 后解析工具调用"策略（见 §2.5）。

### 2.4 内置适配器（现期登记，2026-09-07）
命名预设统一按 `PRESETS` 维护默认 `base_url` / `model` / `key_env`；CLI 可用
`--api-base/--api-key/--model` 逐项覆盖。云端（lmstudio/cloud 服务器）已停用。

| 名称 | 对接 | 默认基础 URL / 模型 | Key 环境变量（.env 亦可） |
| --- | --- | --- | --- |
| `deepseek` | DeepSeek API（思考型，禁 pro，仅 v4-flash） | `https://api.deepseek.com` / `deepseek-v4-flash` | `DEEPSEEK_API_KEY`（兼容旧名 `DeepSeek-API-KEY`） |
| `openai` | OpenAI 兼容 API | `https://api.openai.com/v1` / `gpt-4o-mini` | `OPENAI_API_KEY` |
| `qwen` | 阿里云百炼 DashScope 兼容模式 | `https://dashscope.aliyuncs.com/compatible-mode/v1` / `qwen-plus` | `DASHSCOPE_API_KEY`、`QWEN_API_KEY` |
| `kimi` | 月之暗面 Moonshot | `https://api.moonshot.cn/v1` / `moonshot-v1-8k` | `MOONSHOT_API_KEY`、`KIMI_API_KEY` |
| `glm` | 智谱 AI 开放平台 | `https://open.bigmodel.cn/api/paas/v4` / `glm-4-flash` | `ZHIPU_API_KEY`、`GLM_API_KEY` |
| `anthropic` | Claude API | `https://api.anthropic.com` / `claude-3-5-sonnet` | `ANTHROPIC_API_KEY` |
| `ollama` | 本地 Ollama（OpenAI 网关） | `http://localhost:11434/v1` | `OLLAMA_API_KEY` |
| `vllm` | 本地 vLLM（OpenAI 协议） | `http://localhost:8000/v1` | `VLLM_API_KEY` |
| `custom` | 任意 OpenAI 兼容端点，**用户自配** | 需显式 `--api-base/--api-key/--model` | 省略 key 时回退 `key_env`/`.env` |
| `fake` / `scripted` | 确定性测试替身（不走网络，docs/09 §2.1） | — | — |

**Key 存放（方案 A）**：API Key 永不落 git 跟踪文件。推荐写进工作区根
`novel_workspace/.env` 或当前目录 `.env`（均已 gitignore），形如
`DEEPSEEK_API_KEY=sk-...`；也可用启动时环境变量。优先级：CLI 显式参数 > 环境变量 > `.env`。

### 2.5 结构化输出解析与降级（ADR-006）
1. 请求时置 `response_format=json_object`（若支持）。
2. 返回后 `parse+validate` JSON Schema；
3. 失败 → 自动重试（上限 2 次）；
4. 仍失败 → 宽松解析（取文本、尝试提取 JSON/段），标记 `degraded=true` 并记审计。
5. 降级产物仍可入流水线（通过检查员二次校验），不阻断全书（见质量设计 09）。

### 2.6 供应商审核拦截降级（ADR-015，人工审查意见第 4 点）
- 云 LLM API 有厂商内容审核，可能以**拒答、审核错误、安全拒绝、空输出**等形式出现。Provider 层将这些统一识别为 `moderation_blocked` 结果类别（映射规范见 §9 错误码）。
- 处理链（按序，均受预算与次数上限约束）：
  1. **改写重试**：由当前 Agent 换措辞重发（通常 1–2 次）；
  2. **切换 Provider**：若配置备用模型，切换重发；
  3. **人工介入**：挂起该片段，向用户请求改写/授权/弱化处理；
  4. 全部失败 → 片段置为 `moderation_pending`，**不静默跳过/编造**，该章暂不转正，待人工后再继续。
- **上游预检**：正文生成/对白前运行本地敏感词预检，命中即提示改写，降低厂商侧拦截概率（见 04§5.10）。
- `LLMResult` 新增字段：`blocked: bool`、`block_reason?: string`、`provider_note?: string`；拦截事件入审计与统计。

## 3. 工具协议

Agent 侧请求、系统侧执行的统一契约（F5.4，04§4.2）。

### 3.1 工具注册表 Schema（ToolDef）
```json
{
  "name": "write_draft",
  "description": "写入章节草稿",
  "level": "safe | sensitive | danger",
  "parameters": { "type": "object", "properties": { ... }, "required": [...] }
}
```
- 所有 Agent（主编剧/子代理）看到同一注册表，但权限面不同（05§4.1）。

### 3.2 执行接口（服务侧）
```python
def invoke_tool(
    session: SessionInfo,      # 调用者 + 权限面
    name: str,
    params: dict,
    budget: Budget
) -> ToolResult: ...
```
`ToolResult` 统一含：`status(ok|denied|error)`、`data`、`usage`、`audit_id`。

**核心类型定义（契约，编码实现直接建模）**
```python
@dataclass
class SessionInfo:
    project_id: str            # 所属项目
    agent: str                 # 调用者：orchestrator | sub:<name> | actor:<char_id>
    permission_profile: str    # 权限面名（见 §3.4 policy）
    actor_char_id: str | None  # 仅演员 Agent 有值（数据隔离依据，05§7）
    task_id: str               # 当前派发任务 id（审计关联）

@dataclass
class Budget:
    max_tokens_out: int
    max_tokens_in: int | None = None
    max_cost: float | None = None
    max_rounds: int | None = None   # 循环/围读会轮次上限
```

### 3.3 门禁判定（F6.1/ADR-007）
```
level==safe → execute
level==sensitive && profile.allow → execute
level==sensitive && profile.ask → wait(HumanDecision) | fallback deny-if-timeout
level==danger && profile != allow → wait(HumanDecision); deny if != allow
```
门禁决策来源：CLI 提示 / HTTP 审批端点 / 配置策略文件，三种可并存。

### 3.4 权限策略文件（policy，TOML）
门禁判定引用 `profile`（权限面名，来自 `SessionInfo.permission_profile`）；策略文件定义各 profile 对工具级 `level` 的处置：

```toml
[profile.auto]          # 全自动：仅 safe 自动执行
sensitive = "deny"
danger = "deny"

[profile.supervised]    # 默认：sensitive 询问、danger 拒绝
sensitive = "ask"
danger = "deny"

[profile.trusted]       # 信任态：sensitive 放行、danger 询问
sensitive = "allow"
danger = "ask"

[profile.allow-all]     # 调试用：全部放行（禁止生产）
sensitive = "allow"
danger = "allow"
```
- **工具级覆盖**：`[profile.<name>.tools.<tool_name>]` 可对单个工具覆盖默认处置（如 `write_draft = "allow"`）。
- 策略文件路径由配置项 `security.policy_file` 指定；未配置时回退 `supervised` 默认。

## 4. 事件总线（F7.1，可观测）

进程内统一事件流，所有子系统发布、交互层/日志/统计订阅。

### 4.1 事件类型
```json
// 一条事件（无格式规定的宽松事件）
{
  "ts": "2025-01-01T00:00:00Z",
  "kind": "agent.think | agent.tool | agent.spawn | pipeline.step | consistency.alert | human.decision | system.error",
  "seq": 42,
  "session": {"project": "p001", "agent": "orchestrator|sub:文字匠"},
  "payload": { ... }          // kind 相关
}
```
### 4.2 用途
- **审计**：全量落盘至 `logs/`（F7.1，可经 read_audit 查询）。
- **进度**：交互层渲染 `agent_status`（planning/running/serving/awaiting/finished）。
- **重放**：恢复场景从事件重建未批准草稿后续（04§5.6）。
- **计量**：汇总 token/cost 统计（NFR-9）。

## 5. 一致性规则引擎接口（F4.1）

```python
# 确定性规则检
def run_rule_check(project) -> [
  {level, rule_id, object_ref, detail}
]

# 语义检（调用审校师子代理）
def run_semantic_check(chapter_refs) -> [
  {level: "block|warn", object_ref, issue, suggest}
]

def merge_alerts(rules, semantic) -> ConsistencyReport
```
`ConsistencyReport` 被编排器消费，决定进入 `[修订]` 或放行（04§5.4）。

## 6. 对外 API（交互层 / F8）

### 6.1 CLI（第一形态）
```
novelist init <dir>                # 新建项目
novelist run <dir> --to chapters   # 跑流水线到指定工序
novelist chapter <dir> 3 5         # 生成/续写第3卷第5章
novelist review <dir> 3 5          # 对单章做一致性审查
novelist status <dir>              # 进度与统计
novelist grant <dir>               # 处理待决门禁(审批/拒绝)
novelist export <dir> --format md  # 导出发布包
novelist forge seed <dir> ...      # 模式一：一句话/已有稿子 → bible+大纲+细纲
novelist forge build <dir> ...     # 模式二：已有稿子/设定 → 精修
novelist server                    # 启动 HTTP 服务
```
涉及 LLM 的命令统一支持连接透传选项（P0-2）：
```
--provider <name>              # LLM 后端：fake/scripted/deepseek/openai/qwen/kimi/glm/
                               #   anthropic/ollama/vllm/custom（默认 fake，见 §2.4）
--api-key <key>                # 显式 API Key（优先级：值 > 环境变量 > .env）
--api-base <url>               # 自定义 OpenAI 兼容 base_url（覆盖预设默认；custom 必填）
--model <model>                # 覆盖该 provider 默认模型名
```
例：`novelist chapter <dir> 3 5 --provider deepseek --api-key sk-xxx`；
或接任意中转网关 `--provider openai --api-base https://gw/... --model gpt-4o`；
自定义端点 `--provider custom --api-base <url> --api-key <key> --model <model>`。
未给 `--api-key` 时会回退环境变量 / `.env`（见 §2.4 Key 存放）。

### 6.2 HTTP REST（第二形态，Web 应用）
```
POST /projects                创建项目
GET  /projects/{id}/status    查询进度/统计
POST /projects/{id}/run       驱动流水线 {to, params}
POST /projects/{id}/chapters  生成章节
POST /projects/{id}/review    触发审查
GET  /pending-decisions       拉取待人工决策
POST /decisions/{id}          提交人工审批结果
GET  /projects/{id}/export    导出发布包
   （鉴权：本地 token / 简单会话）
```

### 6.3 降级与一致性
- 关停 HTTP 不影响核心引擎；CLI 可独立驱动全部能力。
- 对外 schema（Req/Resp）以 OpenAPI 定义，在实现阶段随 08 产出。

## 7. 记忆子系统接口（ADR-011/012/013）

记忆子系统分为**读取（检索）**与**写入（编纂）**两组接口；读取工具面向所有 Agent，写入工具默认仅授予主编剧/编纂员。

### 7.1 检索接口（safe，只读）
```
query_memory(query, {
  filters?: { char_id?, chapter_scope?, after?, kinds?: [...] },
  top_k?: 5, min_score?: 0.0
}) -> {
  hits: [{
    sig: str,                  # 记忆碎片唯一签名
    kind: "experience|plot_event|relationship|thread",
    text: str,                 # 摘要文本（可控长度，不拖全文）
    source: {vol, ch},         # 来源定位
    refs: [bible_id],          # 引用的圣经实体（供一致性定位）
    score: float
  }]
}

get_character_history(char_id, {recent_n}) -> [experience entry 摘要]
get_plot_events({filter, around})          -> 剧情事件流（因果/伏笔回溯）
```
- 检索结果默认以**摘要**注入上下文；文字匠可再按需 `read_file` 取详细原章。
- **可选 LLM 侧选 rerank（ADR-027，默认关）**：`MemoryQuery` 可带 `reranker`
  （`Callable[[query,候选], list[(sig, reason)]]`）与 `rerank_pool`。开启时，先在相似度
  打分之上从候选池（`top_k×3`）让 LLM 精审"最相关 ≤top_k 条"，救回分词不重合但语义相关的
  碎片；`reason`（为何优先）写入命中项，未入选按原相似度回补。回调 None = 保持纯确定性地现
  行检索，零 LLM 零配额；编排层负责把回调接到判断类 thinking 路由。

### 7.2 写入接口（sensitive，编纂员/主编剧）
```
append_experience(char_id, entry)        # 人物经历史追加（带冲突校验）
append_plot_event(event)                 # 剧情事件流追加
record_relationship_change(a, b, event)  # 关系变化记录
```
- 每个写入请求先过 **MemoryValidator**（规则 + LLM 语义双检，见 09§4）：失败 → 返回 `conflicted`，不写入；成功 → 写 `memory/` + 登记 `fragment_index.json`。
- 全部写操作留审计（`session` + params 摘要 + 结果状态）。

### 7.3 索引维护
```
reindex_memory(project, {incremental?: true})   # 重建 RAG 索引（可后台异步）
rebuild_indexdb(project)                        # 从文件重建 .index.db（ADR-016）——可随时运维恢复
```
- 语义向量化由 Embedding 提供（云 API 或本地模型，见 §8 能力矩阵）。
- 关键词兜底：无 Embedding 时退化为中文分词 + 倒排索引检索（保可用性）。
- 检索/范围查询优先走 `.index.db`（SQLite，ADR-016 的辅助索引）；索引缺失或过期时自动降级为文件扫描并后台重建。

### 7.4 试演片段
```
write_take(char_id, chapter_id, take_seq, content)  # safe, 写 takes/
```
- 用于角色演员产出 `character_take`；写入路径受沙箱约束，仅限本 `chapter_id` 下。

### 7.5 围读会接口（ADR-014，受控群聊）
主持人（主编剧/编排层）可通过以下 safe 接口驱动一场围读会，满足收敛判据即结束。
```
create_scene(scene_id, {topic, actors: [char_id], context_refs, max_rounds})   # 主持人
join_scene(scene_id)                  # 演员加入，返回当前谈话流摘要
say_line(scene_id, speech)            # 广播给同场景其他演员，返回下一回合提示
leave_scene(scene_id)                 # 演员退出，返回剩余活跃人数
close_scene(scene_id, {reason})       # 主持人收场；落地 scene.transcript → 并入 takes
```
- 结束判据（见 05§5.3）：全体 `leave_scene` / 达 `max_rounds` / 主持人判定收敛 / 预算超时 / 异常强制收场。任一满足即由主持人 `close_scene`。
- 谈话纪要 `scene.transcript` 作为演员 `character_take` 的组成部分供文字匠整合。

## 8. Provider 能力矩阵与降级总表

| 能力 | 无此能力时的策略 |
| --- | --- |
| tool_calling | 文本消息 + 后解析 `tool_calls`（正则/结构化提取），仍走门禁 |
| json_mode | 退化为"由 LLM 生成 JSON + 宽松解析" |
| streaming | 关闭流式，改整包返回 |
| **embedding** | 语义检索退化为关键词+倒排索引；保留 `query_memory` 可用性（07§7.3） |
| **审核行为可识别性** | 若 Provider 不吐明确的审核错误（统一返回空/拒答），需按"空输出 + 预检命中"启发式判断并走上游预检，见 07§2.6 |
| 大 context | 触发 04§5.3 压缩 | 

## 9. 错误码约定
`OK / DENIED / NOT_FOUND / BUDGET_EXCEEDED / SCHEMA_FAIL / PROVIDER_ERROR / MODERATION_BLOCKED / INTERNAL` —— 贯穿工具结果、事件、HTTP 状态码映射，保证可程序化处理。

### 9.1 错误码 ↔ HTTP / 工具结果映射表
| 错误码 | HTTP | 工具 status | 说明 |
| --- | --- | --- | --- |
| `OK` | 200 | `ok` | 正常 |
| `DENIED` | 403 | `denied` | 权限门禁拒绝 |
| `NOT_FOUND` | 404 | `error` | 实体/文件不存在 |
| `BUDGET_EXCEEDED` | 429 | `error` | 预算/配额超限 |
| `SCHEMA_FAIL` | 422 | `error` | 结构化输出/参数校验失败 |
| `PROVIDER_ERROR` | 502 | `error` | LLM 供应商错误（可重试） |
| `MODERATION_BLOCKED` | 451 | `error` | 供应商审核拦截（ADR-015，走 07§2.6 处理链） |
| `INTERNAL` | 500 | `error` | 未预期内部错误 |
- `MODERATION_BLOCKED`：LLM 输出被供应商审核拦截（ADR-015），上游按 07§2.6 处理链降级。
- 围读会相关的场景级状态（未加入/已结束/越权/轮次达到上限）由工具返回结构化 `status` 字段表达，不新增顶层错误码。
