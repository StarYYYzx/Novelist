# Novelist · AI 协作入口

本仓库的**唯一规范源**是根目录的 [`AGENTS.md`](../AGENTS.md)。请先读它。

本文件只是入口指针，**刻意不复制** AGENTS.md 的内容——同一事实写两处必然漂移，
而这个仓库历史上最大的问题恰恰是"同一事实多处不一致"。

三条最容易踩的：

- **接手第一步**：`python scripts/ai_bootstrap.py`（装载技能 + 挂 git hook）
- **提交前**：`python scripts/check.py --all` 必须全绿（CI 也跑，别跳过）
- **只读禁区**：`tests/test_noval/`、`.workbuddy/`、`core/scene_tools.py`
