@AGENTS.md

# Novelist · Claude Code 桥接

上一行导入本仓库的唯一规范源 `AGENTS.md`。**通用规范一律写进 `AGENTS.md`，不要写在这里**——
本文件只放 Claude Code 专属内容（hooks、权限、模型选择等），否则就有两个规范源了。

## 技能

技能（Agent Skills）的唯一源在 `.ai/skills/`，随 git 分发。

本仓库的 `.claude/skills` 是指向它的目录联接（由 `python scripts/ai_bootstrap.py` 建立），
所以项目技能会自动被发现。若你发现 `.claude/skills` 不存在，先跑一次：

```bash
python scripts/ai_bootstrap.py
```

## 提交前

```bash
python scripts/check.py --all
```

CI 也会跑同一条命令，本地漏了不会漏到远端。
