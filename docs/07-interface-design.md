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
- `ProviderCapabilities` 描述：`tool_calling: bool`、`max_context`、`json_mode: bool`、`streaming: bool`，供降级策略使用（ADR-006）。

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
  "degraded": false
}
```

### 2.3 适配器实现登记
- 通过**插件注册表**注册：`register_provider(name, factory)`。
- 每个适配器仅在**运行时加载**对应 SDK（可选依赖），避免冷启动加载全部。
- `ProviderCapabilities` 不一致时的降级：例如某模型无 tool_calling → 编排器自动切换为"文本 + 后解析工具调用"策略（见 §2.5）。

### 2.4 内置适配器（第一期）
| 名称 | 对接 | 说明 |
| --- | --- | --- |
| `openai` | OpenAI 兼容 API | 亦覆盖多数兼容网关 |
| `deepseek` | DeepSeek API | 中文写作性价比 |
| `anthropic` | Claude API | 可对照 Claude Code 参考 |
| `ollama` | 本地 Ollama | 本地开源模型 |
| `vllm` | 本地 vLLM（OpenAI 协议） | 高性能本地推理 |

### 2.5 结构化输出解析与降级（ADR-006）
1. 请求时置 `response_format=json_object`（若支持）。
2. 返回后 `parse+validate` JSON Schema；
3. 失败 → 自动重试（上限 2 次）；
4. 仍失败 → 宽松解析（取文本、尝试提取 JSON/段），标记 `degraded=true` 并记审计。
5. 降级产物仍可入流水线（通过检查员二次校验），不阻断全书（见质量设计 09）。

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

### 3.3 门禁判定（F6.1/ADR-007）
```
level==safe → execute
level==sensitive && profile.allow → execute
level==sensitive && profile.ask → wait(HumanDecision) | fallback deny-if-timeout
level==danger && profile != allow → wait(HumanDecision); deny if != allow
```
门禁决策来源：CLI 提示 / HTTP 审批端点 / 配置策略文件，三种可并存。

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
novelist server                     # 启动 HTTP 服务
```

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

## 7. Provider 能力矩阵与降级总表

| 能力 | 无此能力时的策略 |
| --- | --- |
| tool_calling | 文本消息 + 后解析 `tool_calls`（正则/结构化提取），仍走门禁 |
| json_mode | 退化为"由 LLM 生成 JSON + 宽松解析" |
| streaming | 关闭流式，改整包返回 |
| 大 context | 触发 04§5.3 压缩 | 

## 8. 错误码约定
`OK / DENIED / NOT_FOUND / BUDGET_EXCEEDED / SCHEMA_FAIL / PROVIDER_ERROR / INTERNAL` —— 贯穿工具结果、事件、HTTP 状态码映射，保证可程序化处理。
