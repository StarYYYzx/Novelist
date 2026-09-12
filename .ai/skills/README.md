# `.ai/skills` —— 团队技能的唯一源

这个目录是**技能（Agent Skills）的唯一源**，随 git 分发。团队任何成员 clone 后都能拿到同一套技能。

## 为什么放在这里

- **工具中立**：不绑定 WorkBuddy / Claude Code / Cursor 任何一家。
- **随 git 走**：`.workbuddy/`、`.claude/` 都是本机目录且被 `.gitignore` 排除，放在那里的技能**传不出去**。
- **一份源，多处原生可见**：各工具只认自己的目录，所以用目录联接（junction）桥过去——
  `.workbuddy/skills` 与 `.claude/skills` 都是指向本目录的链接，**零复制、零漂移**。

## 怎么用

```bash
python scripts/ai_bootstrap.py     # 建链接（Windows 用目录联接，无需管理员权限）
python scripts/ai_bootstrap.py --status   # 只看现状
```

跑一次即可。`AGENTS.md` 里写了"接手第一步就是这个"，所以 AI agent 读到会替你执行。

## 技能格式

标准 Agent Skills 格式：`<name>/SKILL.md`，YAML frontmatter 必须含 `name` 与 `description`。

```
.ai/skills/
├── README.md                        ← 本文件
├── novelist-dev-workflow/
│   └── SKILL.md                     ← 本仓库的开发约定（解释器、回归命令、禁区）
└── python-repo-quality-remediation/
    └── SKILL.md                     ← 通用：分批质量整治闭环
```

`description` 决定 agent 何时自动加载，**要写清"做什么 + 什么时候用"**，别写成一句摘要。
正文只在技能被加载时才进上下文，所以可以写得具体；但每一行都是重复成本，别写废话。

## 加新技能

1. `mkdir .ai/skills/<kebab-case-name>` 并写 `SKILL.md`
2. 若是**本仓库专用**的，在这里（团队共享）；若是**你个人跨项目**的习惯，放 `~/.workbuddy/skills/`（或个人级目录），**不要提交**
3. 技能既影响每个人的 agent 行为，也影响每一次任务 → **改动走 PR**（见 `.github/CODEOWNERS`）

## 与"规范"的分工

- **规范**（必须遵守的约束、命令、禁区）→ `AGENTS.md`，全工具生效。
- **技能**（可复用的流程、方法论）→ 这里，按需加载。

别把同一件事两边都写——那正是本仓库历史上一类反复出现的病（同一事实多处不一致）。
