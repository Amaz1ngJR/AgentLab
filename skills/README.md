# Skills 使用指南

这个文件夹放的是 **Skill（技能包）**。如果你是第一次接触，看这一篇就够了。

---

## 一、Skill 是什么？

一句话：**Skill 是写给 Agent 看的"任务说明书"。**

打个比方 —— 同样是让一个新人帮你做事：

- 你直接说"帮我看下代码" → 他凭感觉做，每次做法都不一样。
- 你递给他一张《代码审查清单》→ 他照着步骤做，稳定、专业。

这张"清单"就是 Skill。它告诉 Agent：**这类任务应该按什么步骤做、用哪些工具、参考什么资料、输出要满足什么要求。**

### Skill 不是什么（重要）

Skill **只是说明书，不是权限**。

- ✅ Skill 能：给 Agent 提供做事的步骤和上下文。
- ❌ Skill 不能：让 Agent 获得它本来没有的工具权限。

举例：哪怕一个 Skill 写了"建议使用 shell 工具"，如果当前 Agent 没被授予 shell，Agent 还是用不了。工具能不能用，由 Agent 配置和审批策略决定，**Skill 说了不算**。这是故意设计的安全边界。

### Skill / 工具 / MCP 的区别

| 概念 | 解决什么 | 例子 |
|---|---|---|
| **Skill** | *怎么做*一类任务 | "代码审查时先跑测试，再按严重程度输出问题" |
| **工具（Tool）** | 程序内的*原子能力* | 读文件、写文件、跑命令 |
| **MCP** | 连接*外部系统*的能力 | GitHub、数据库、浏览器 |

---

## 二、文件夹长什么样

每个 Skill 是一个**独立子目录**，目录名就是它的 ID：

```text
skills/
  code-review/              ← 一个 Skill，id = code-review
    SKILL.md                ← 必须有：说明书本体
    references/             ← 可选：参考资料，按需查阅
      checklist.md
  confluence-update/        ← 另一个 Skill
    SKILL.md
    scripts/                ← 可选：附带脚本（只能经授权的工具执行）
      confluence_update.py
```

规则很简单：

- **每个 Skill 一个文件夹**，文件夹名就是 Skill 的 id。
- 文件夹里**必须有 `SKILL.md`**，否则会被忽略。
- `references/`（参考资料）和 `scripts/`（脚本）都是**可选**的。

> 你可能还会看到 `xxx.skill` 文件 —— 那是打包好的 Skill 压缩包（方便分发），解开后就是上面的目录结构。

---

## 三、SKILL.md 怎么写

`SKILL.md` 分两部分：**开头的元数据（frontmatter）** + **下面的正文（工作流）**。

```markdown
---
name: code-review
description: 审查代码改动的正确性、测试覆盖和安全问题。
allowed_tools: [read_file, list_dir, code_search, shell]
optional_mcp_servers: [git]
triggers: [review, 审查, 代码审查]
enabled: false
---

# 工作流

1. 先用 code_search 找到改动相关的文件和测试。
2. read_file 精读改动点，确认行为是否符合意图。
3. 优先报告正确性与安全问题，再给摘要。
```

### 开头的元数据字段（两条 `---` 之间）

| 字段 | 必填 | 作用 |
|---|---|---|
| `name` | 是* | 展示名称 |
| `description` | 是* | 一句话说明这个 Skill 干什么 |
| `allowed_tools` | 否 | **建议**用到的工具（只是说明，不授权！） |
| `optional_mcp_servers` | 否 | 可能用到的 MCP server |
| `triggers` | 否 | 触发关键词，用户说的话里命中了就自动推荐这个 Skill |
| `enabled` | 否 | 是否默认启用，**缺省为 `false`（默认关闭）** |

> \* `name` 和 `description` 至少要有一个，否则这个 Skill 会被当成无效而跳过。

### 下面的正文

`---` 之后随便写 Markdown，这部分会被**原样注入**给 Agent，就是真正的"工作流说明书"。写清楚步骤、约束、输出格式即可。

---

## 四、怎么用一个 Skill？

Skill 默认是**关着**的（`enabled: false`）。让它生效有两种方式：

### 方式 1：在 Agent 配置里挂上（推荐）

编辑 `config/agents.yaml`，给某个 Agent 加 `skills` 字段：

```yaml
agents:
  coder:
    name: 代码助手
    model_profile: cloud_claude
    skills: [code-review]      # ← 把 code-review 挂给这个 Agent
    memory_policy: read_write
```

这样只要用 coder 这个 Agent，`code-review` 的工作流就会自动注入到对话里。
（被 Agent 显式挂上的 Skill，即使 `enabled: false` 也会生效 —— 因为这就是你"明确选用"它的表态。）

### 方式 2：在 SKILL.md 里默认开启

把 frontmatter 的 `enabled` 改成 `true`：

```markdown
enabled: true
```

这样它对所有 Agent 全局生效。如果还写了 `triggers`，则只在用户说的话命中关键词时才注入（避免无关时占用篇幅）。

### 验证有没有生效

启动 CLI 时会打印发现和启用的 Skill 数量：

```bash
python -m app --profile cloud_claude
# 输出里会看到：
# Skill    : 发现 2 个 (code-review, confluence-update); 默认启用 0 个
```

---

## 五、动手做一个自己的 Skill

3 步搞定：

1. **建目录**：`skills/my-skill/`
2. **写 `SKILL.md`**：

   ```markdown
   ---
   name: my-skill
   description: 一句话说明这个技能干什么。
   triggers: [关键词1, 关键词2]
   enabled: true
   ---

   # 工作流

   1. 第一步做什么
   2. 第二步做什么
   ```
3. **重启 CLI** —— 启动时会自动扫描到它。

需要参考资料就建 `references/` 子目录放进去；需要脚本就建 `scripts/`（注意脚本仍要经授权的工具才能执行）。

---

## 六、现成的例子

| Skill | 作用 |
|---|---|
| `code-review/` | 代码审查工作流：先定位改动 → 精读 → 按严重程度报告问题。附带审查清单 `references/checklist.md`。 |
| `confluence-update/` | 更新 Confluence 文档页面：通过 REST API 读取/更新页面内容。 |

想看真实写法，直接打开它们的 `SKILL.md` 照着改即可。

---

## 七、安全提醒（再说一遍，很重要）

- Skill **只提供说明，不提供权限**。它列的 `allowed_tools` 只是"建议/需求"，Agent 实际能用什么工具，仍由 Agent 配置和审批决定。
- **来路不明的 Skill 默认关闭**（`enabled` 缺省 false）。启用别人给的 Skill 前，先打开它的 `SKILL.md` 看清楚它想做什么、要用哪些工具。
- Skill 里的 `scripts/` 脚本不会自动运行，必须经过受控的工具调用和审批。
