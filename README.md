# AgentLab

运行在个人电脑上的本地 Agent 应用。支持对话、读写文件、搜索代码、执行命令、交互式终端会话、维护任务清单、操作浏览器、多 Agent 会话与长期记忆，可在本地模型（Ollama / Qwen 等）和云端模型（Claude / GPT）之间切换，只改配置不改代码。

## 功能

- **对话**：多轮上下文对话；流式输出文本逐字展示，token 实时增长
- **文件操作**：read_file / write_file / list_dir，workspace 内按风险执行，越界必须审批
- **代码搜索**：`code_search` 支持 text / regex / file / symbol 四种模式，优先用 ripgrep，无 rg 时 Python fallback；遵守 `.gitignore`、命中行密钥脱敏
- **Web 搜索**：`web_search` 在互联网上搜索信息，返回标题、链接和摘要；使用 DuckDuckGo（注重隐私，无需 API key）
- **Shell 命令**：跨平台 shell 工具（Mac/Linux 用 bash，Windows 用 PowerShell），默认 cwd 为 workspace，外部 cwd 需审批
- **交互式终端**：`terminal_open` / `terminal_send` / `terminal_close` / `terminal_list` 维持 PTY 会话，适合远程登录、REPL、交互式安装器等需要持续对话的程序
- **图片输入**：支持 `/image <路径> [-- 提示词]`、`/paste-image`，以及在聊天输入框直接按 Ctrl+V / Shift+Insert 粘贴剪贴板图片；图片以受控附件引用持久化
- **浏览器控制**：通过 Playwright MCP 打开网页、截图、读 DOM、点击、输入（默认禁用，需显式启用）
- **多 Agent / 会话**：`/session` 命令族在一个进程里创建、切换、归档多个 Agent 会话；会话消息存 SQLite，可恢复
- **长期记忆**：按 AgentProfile 的 `memory_policy` 检索历史记忆注入上下文，`read_write` 策略会话结束写摘要
- **上下文压缩**：接近模型窗口上限时自动压缩可压缩历史，原始消息保留在 SQLite，`/context` 命令族查看与手动控制
- **任务面板**：模型用 todo_write 维护多步任务清单，CLI 实时显示进度（`✓ done` / `❯ in_progress` / `○ pending`）
- **方向键审批**：写操作 / shell 命令 / 浏览器动作前弹出菜单 `允许这次 / 总是允许 / 拒绝`，↑↓ 选择 + Enter
- **可中断执行**：执行中按 Esc 或 Ctrl-C 停下，可直接输入新指令调整方向；`/resume` 继续上一轮未完成任务
- **模型切换**：`--profile` 或 `.env` 切换本地 / 云端，不改代码；`/model` 命令族运行时查看和切换模型；本地端点不通时给出可执行修复指引
- **进度可见**：LLM 调用期间 `✻ thinking… (3.2s · ↓ 42 tokens)` 实时刷新；每轮末尾打印耗时与 token

## 安装

需要 **Python 3.11+**。

`python -m pip install -e .` 会安装依赖并注册当前虚拟环境内的 `agentlab` 命令；源码修改后无需重复安装。

### Windows：创建 `myenv` 并部署

Windows 建议使用 Python Launcher 创建项目专属虚拟环境，避免 MSYS2 的 `python.exe` 与系统 `pip.exe` 指向不同环境。以下命令均在 AgentLab 项目根目录的 PowerShell 中执行。

1. 确认已安装 Python 3.11：

```powershell
py --list
py -3.11 --version
```

如果找不到 `py`，请安装 [python.org](https://www.python.org/downloads/windows/) 提供的 Windows Python，并在安装时勾选 **Add Python to PATH** 和 **Install launcher for all users**。

2. 创建并激活名为 `myenv` 的虚拟环境：

```powershell
py -3.11 -m venv myenv
.\myenv\Scripts\Activate.ps1
```

如果 PowerShell 提示禁止运行脚本，可仅对当前终端临时放开策略，然后重新激活：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\myenv\Scripts\Activate.ps1
```

激活成功后，命令行前会显示 `(myenv)`。

3. 验证环境并安装 AgentLab：

```powershell
python -c "import sys; print(sys.executable)"
python -m pip --version
python -m pip install --upgrade pip
python -m pip install -e .
Copy-Item .env.example .env
```

`sys.executable` 和 pip 路径都应包含当前项目的 `myenv\Scripts`。请始终使用 `python -m pip`，不要混用裸 `pip` 和其他位置的 `python`。

4. 编辑 `.env` 配置模型凭据，然后启动：

```powershell
python -m app
# 或使用安装后注册的命令
agentlab --workspace .
```

5. 退出环境；以后重新进入项目时再次激活：

```powershell
deactivate

# 下次使用
cd C:\Users\YanJunru\Code\YJR\AgentLab
.\myenv\Scripts\Activate.ps1
python -m app
```

如果不方便激活，可直接使用虚拟环境解释器完成部署和启动：

```powershell
.\myenv\Scripts\python.exe -m pip install --upgrade pip
.\myenv\Scripts\python.exe -m pip install -e .
.\myenv\Scripts\python.exe -m app
```

遇到 `ModuleNotFoundError` 或 `python -m pip` 不可用时，先检查命令实际指向：

```powershell
Get-Command python,pip
where.exe python
where.exe pip
python -c "import sys; print(sys.executable)"
```

如果 `python` 指向 `C:\msys64\...\python.exe`，而 `pip` 指向 `C:\Users\...\Python311\Scripts\pip.exe`，说明环境发生错配；退出当前终端，重新使用 `py -3.11 -m venv myenv` 创建环境，不要用裸 `pip` 修补另一个 Python。

### macOS / Linux：Conda 安装

```bash
conda create -n agentlab python=3.11
conda activate agentlab
python -m pip install -e .
cp .env.example .env
```

**可选依赖**（根据需要安装，跨平台通用）：

```bash
# Web 搜索功能（web_search 工具）
pip install duckduckgo-search requests beautifulsoup4
```

浏览器控制还需要 Node.js LTS / npx（首次启动会自动下载 Playwright MCP
和浏览器内核）。Windows 会自动解析 `npx.cmd`，macOS、Linux 和 Windows
共用同一份 MCP 配置。

## 快速开始

### 选项 A：用 Anthropic Claude（推荐起步，凭代理 token 即可）

编辑 `.env`：

```env
ACTIVE_PROFILE=cloud_claude
ANTHROPIC_AUTH_TOKEN=cr_xxxxxxxx
ANTHROPIC_BASE_URL=https://your-proxy/api/
```

```bash
python -m app
```

### 选项 B：用 Ollama 下载并启用本地模型

先安装 [Ollama](https://ollama.com/download)，然后启动服务：

```powershell
# Windows（安装后通常会自动启动；未启动时手动执行）
ollama serve
```

```bash
# macOS / Linux
ollama serve
```

打开另一个终端下载模型。推荐 16 GB 显存设备使用 Qwen3 14B：

```bash
ollama pull qwen3:14b
ollama list

# 先直接测试模型；输入 /bye 退出
ollama run qwen3:14b
```

如需将模型保存在项目目录，必须在首次 `pull` 和每次启动 Ollama 服务前设置相同的 `OLLAMA_MODELS`。路径按实际项目位置修改：

```powershell
# Windows PowerShell
$env:OLLAMA_MODELS = "$PWD\.ollama\qwen3-14b\models"
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $env:OLLAMA_MODELS, "User")
ollama serve
ollama pull qwen3:14b
```

```bash
# macOS / Linux
export OLLAMA_MODELS="$PWD/.ollama/qwen3-14b/models"
ollama serve
# 在另一个同样设置了 OLLAMA_MODELS 的终端执行：ollama pull qwen3:14b
```

激活 AgentLab 的 Qwen3 14B profile：

```dotenv
# .env
ACTIVE_PROFILE=local_qwen3_14b
```

```bash
python -m app
# 或不修改 .env，单次指定 profile
python -m app --profile local_qwen3_14b -p "用一句话介绍你自己"
```

也可以使用现有的一键脚本安装默认的 Qwen2.5-Coder 7B：

```bash
bash scripts/install_local_model.sh                 # macOS / Linux
# Windows: powershell -ExecutionPolicy Bypass -File scripts\install_local_model.ps1
```

模型选型 / 硬件评估 / 局域网 GPU 模式见 [`docs/local_model_guide.md`](docs/local_model_guide.md)。

## 交互示例

```
== AgentLab ==
provider : anthropic
model    : claude-sonnet-4-6
profile  : cloud_claude
能力     : chat, tools
workspace: /Users/you/AgentLab
工具     : read_file / write_file / list_dir / shell / web_search / terminal_* (交互式会话) / todo_write
输入 /reset 清空会话; /resume 继续未完成任务; /session [list|new|switch|...] 管理多 Agent; exit/quit 退出.

▸ 帮我重构 cli.py 里的 spinner 逻辑

  4 tasks (1 done, 1 in progress, 2 open)
    ✓ 读取并理解 cli.py 当前实现
    ❯ 找出可重构的点
    ○ 改写 spinner
    ○ 跑测试

  ✻ thinking… (3.4s · ↓ 87 tokens)
  · tool code_search({"query": "spinner", "mode": "symbol"})
    [ok] (12ms) ...
  · tool read_file({"path": "app/cli.py"})
    [ok] (1ms) ...

[流式中文回复 + 后续工具循环]

  [stats] turn 12.3s in=1234 out=456 | session 12.3s in=1234 out=456
```

写文件 / 执行 shell 命令 / 浏览器动作会按风险弹出方向键菜单：

```
工具: write_file
参数: {"path": "app/cli.py", "content": "..."}

是否允许执行?
❯ 1. 允许这次
  2. 本会话在当前工作区允许 write_file
  3. 修改建议
  4. 拒绝

↑↓ 移动 · Enter 确认 · 1-9 快捷键 · Esc 取消
```

工作区内可安全分析的 shell 命令还会提供“本会话允许命令前缀”，例如
`git status`、`npm run test`。规则按参数 token 精确匹配并限定在当前 Agent
Session 和 workspace；管道或命令链会逐段检查。越界目录、重定向、变量替换、
通配符、解释器和破坏性命令不会生成可复用授权，仍需逐次确认。

## 命令行参数

```bash
agentlab --workspace .                 # 从任意目录启动，以当前目录为工作区
agentlab -w /path/to/project           # 指定其他工作区
agentlab -w . --profile local_qwen     # 指定工作区和模型 profile

python -m app --version                # 显示当前版本后退出
python -m app --image ./error.png -p "分析截图"  # 单次图片 prompt
python -m app                          # 交互式 REPL
python -m app -p "帮我看 README.md"    # 单次 prompt 后退出
python -m app -y                       # 自动放行所有工具（跳过审批）
python -m app --profile local_qwen     # 临时切换 profile,不改 .env
```

`agentlab` 可以在任意目录执行。模型、Agent、Skill 和 MCP 配置仍从 AgentLab
源码目录加载；`--workspace` 是默认工作范围。工具可以请求访问其他目录，但
每次越界都必须经过独立审批。
`python -m app` 继续作为源码目录内的开发启动方式。

## REPL 内命令

在交互模式里可输入这些斜杠命令（不发给模型）：

| 命令 | 作用 |
|---|---|
| `/rtk` | 查看 AgentLab 内置 RTK Shell 输出压缩状态 |
| `/paste-image` | 从系统剪贴板读取图片，附加到下一条消息 |
| `/image <路径> [-- 提示词]` | 从本地路径附加图片；提供提示词时立即发送 |
| `/attachments` / `/attachments clear` | 查看或清空待发送图片 |
| `/attachments clear-session` | 经审批清理当前 Session 的全部历史图片和磁盘附件，保留文本对话 |
| `/version` | 显示当前运行的 AgentLab 版本 |
| `/reset` | 清空当前会话的消息和任务 |
| `/resume [目标]` | 继续上一轮未完成的任务（失败任务重置为 pending） |
| `/model` | 列出所有配置的模型（同 `/model list`） |
| `/model list` | 列出所有配置的模型，标记当前使用的模型 |
| `/model current` | 显示当前模型的详细配置和使用统计 |
| `/model switch <profile>` | 切换到指定模型（新建 session 时生效） |
| `/context` | 显示上下文预算 + recent window + 压缩摘要状态 |
| `/context compact` | 立即压缩当前会话的可压缩历史 |
| `/context summary` | 查看当前生效的压缩摘要 |
| `/context disable-auto-compact` / `enable-auto-compact` | 关闭 / 开启本会话自动压缩 |
| `/session` | 显示当前 session 信息 |
| `/session list` | 列出所有活跃 session |
| `/session agents` | 列出 `config/agents.yaml` 里可用的 Agent |
| `/session new [agent_id] [标题]` | 新建并切换到一个 Agent 会话 |
| `/session switch <session_id>` | 切换到已有 session（可从 SQLite 恢复历史） |
| `/session rename <标题>` | 重命名当前 session |
| `/session archive` | 归档当前 session（软删除，数据保留、从列表隐藏，可日后 switch 回来） |
| `/session delete [session_id]` | 彻底删除 session 及其消息（硬删除，不可恢复；留空删当前） |
| `exit` / `quit` / Ctrl-D | 退出 |

不同 session 的消息历史、任务清单互相隔离，切换时不串。

### 图片输入

支持 PNG、JPEG、WEBP 和 GIF，单张最大 5MB、最大 2500 万像素。当前
`cloud_claude`、`cloud_claude_opus` 和已实测的 `crs_gpt` profile 声明了
`vision` 能力；`gpt_official` 默认模型是非视觉的 `gpt-3.5-turbo`，切换到
视觉模型后需在 profile capabilities 中显式加入 `vision`。其他 profile 发送图片
时会明确拒绝，避免图片被静默丢弃。

```text
# 直接在聊天输入框复制图片后按 Ctrl+V 或 Shift+Insert
# 输入框会出现 [AgentLab:image:...] 占位符，再输入问题并回车
# 注意：部分终端会拦截 Ctrl+V；此时必须使用 Shift+Insert 或 /paste-image

/image
# 裸 /image 只显示帮助，不会发送消息

/paste-image
请分析剪贴板里的报错截图

/image ./screenshots/error.png -- 分析错误原因并给出修复步骤

/image ./screenshots/a.png
/attachments
请比较刚才附加的图片
```

图片会复制到 `data/attachments/<session_id>/`，SQLite 仅保存路径、MIME、尺寸和
SHA-256 引用，不保存大段 base64。发送给云端模型前才读取编码；这意味着图片内容
会离开本机，请勿发送密钥、身份证件或其他敏感图片。读取 workspace 外的图片仍需
逐次审批。若终端拦截 Ctrl+V，可使用 Shift+Insert 或 `/paste-image`；终端本身
通常不会把图片二进制像普通文本一样直接传给应用，因此 `/paste-image` 是最稳定的
跨终端方式。`/image` 裸命令只显示帮助，`/image <路径>` 只加入待发送队列，只有
显式写 `/image <路径> -- <提示词>` 才会立即发送。

#### 清理图片

只清理当前 CLI 中尚未发送的待发送图片，不删除磁盘文件：

```text
/attachments clear
```

清理当前 Session 中已经发送/上传的所有图片，同时移除历史消息中的图片引用并
删除 `data/attachments/<session_id>/`，但 **不会删除 Session 本身**，也会保留
文字对话、Assistant 回复、任务、统计和其他历史数据：

```text
/attachments clear-session
```

该操作会先显示审批确认；Session 正在执行 run 时会拒绝清理，需先停止当前任务。
如果一条消息同时包含文字和图片，只移除图片、保留文字；如果一条消息只有图片，
清理后该空消息会被移除。清理完成后 Session ID 保持不变，仍可继续对话，且恢复
该 Session 时不会再尝试读取已经删除的图片。

彻底删除某个 Session 时，会同步清理该 Session 的消息和附件目录：

```text
/session delete <session_id>
```

清理本机所有 Session 的已上传/粘贴图片时，请先退出 AgentLab，再执行：

```bash
# macOS / Linux
rm -rf data/attachments
```

```powershell
# Windows PowerShell
Remove-Item -Recurse -Force .\data\attachments
```

`data/attachments/` 会在下次附加图片时自动创建。直接删除全部附件不会删除
SQLite 中的历史消息；旧消息仍保留图片路径引用，恢复该会话并再次发送上下文时
可能提示图片文件不存在。如果旧会话也不再需要，优先使用 `/session delete` 逐个
删除，使消息和附件一起清理。

**使用提示**：
- 所有斜杠命令支持 Tab 自动补全；`/model switch` 会补全 `config/models.yaml` 里的 profile 名
- `/model switch` 只改后续新建 session 的模型，当前会话不受影响；切完接 `/session new` 生效，或重启 `agentlab` 全局切换

### 方案确认选项

需要用户决定具体方案时，Agent 使用 `request_confirmation` 显示“同意”“拒绝”“修改方案”“完全托管自动执行”四个选项。选择修改方案会暂停当前轮，回到输入框补充要求；完全托管会开启当前会话自动执行。普通工具审批继续沿用已有授权规则。显式方案确认在自动执行模式下仍需用户选择，无交互终端时会停止等待人工处理，不会自动代答。

### 批量读取文件

`read_files` 一次接收最多 12 个 `{path, offset, limit}` 请求，可读取多个文件或同文件的多个行段。结果按请求顺序包含路径、行号、内容、`next_offset` 和 `truncated`；`pending_indices` 列出本次尚未返回的请求索引。所有文件共享 9000 字符的总输出预算，不会按每个文件分别放大上下文。分配预算不足以容纳下一行时，按提示改用单项 `read_file` 续读。批量入口只读取工作区内部路径，外部文件仍使用 `read_file` 单独审批。

同轮独立只读请求可受控并发，子 Agent 也支持，权限和调用预算在启动前预检与预留。`read_file` 续读缓存的是行位置而非文件内容，最多保留 128 份文件版本、每份 2048 个位置；文件版本变化后不复用旧位置。

### 批量修改文件

`apply_patch` 一次处理多文件、多处修改，集中展示 diff 和审批，减少逐文件调用和模型往返。支持 Codex 风格的 `*** Begin Patch` / `*** End Patch`、`Add File`、`Update File`、`Delete File`、`Move to` 以及多个 `@@` 上下文块。为兼容当前各模型适配器，工具参数使用 JSON 的 `patch` 字符串。

应用前校验整份补丁；先精确匹配，再尝试忽略行尾及两端空白。多个候选位置会拒绝修改，需补充上下文或锚点。保留上下文原文、原有换行和权限。仅支持工作区内 UTF-8 普通文件，不跟随符号链接，也不修改 `.git`。子 Agent 的每个补丁路径和移动目标仍受 `file_scope` 约束。写入中途失败会尝试恢复已完成部分，若出现并发修改或恢复失败会明确报告残留路径；这不是跨文件系统事务。结果报告预检及写入耗时。

## 内置 RTK Shell 输出压缩

AgentLab 内置了受 [RTK（Rust Token Killer）](https://github.com/rtk-ai/rtk)
启发的 Python 输出压缩引擎，**无需另外安装 `rtk`、Rust、Hook 或二进制文件**。
它在 `shell` 命令执行完成后压缩输出，原始命令、审批、cwd、timeout 和退出码仍由
AgentLab 控制。过滤器异常、命令不支持或节省不足时会自动返回原始输出。

默认支持的类别包括：

- `git status`、`git diff`、`git log`
- `pytest`、Cargo/Node/Go 测试
- `rg` / `grep`
- `ls` / `tree` / `find` 和长文件读取
- Ruff、Mypy、ESLint、TSC 等诊断输出
- Docker、Kubectl、OpenShift 表格输出

查看运行状态：

```text
/rtk
```

配置 `.env`：

```env
AGENTLAB_RTK_ENABLED=true
AGENTLAB_RTK_ULTRA_COMPACT=false
```

压缩后的 shell 结果会附带一行元数据，例如：

```text
[rtk: pytest, 32000->1200 bytes, -96%]
[exit code: 0]
```

需要完整原始输出时，可把 `AGENTLAB_RTK_ENABLED=false` 后重启 AgentLab；也可以
改用更精确的命令缩小输出。该功能是 AgentLab 面向自身工具、审批和审计架构实现的
独立 Python 适配版本。

## 多 Agent 与长期记忆

复制模板启用多 Agent：

```bash
cp config/agents.example.yaml config/agents.yaml
```

`config/agents.yaml` 每个条目定义一个 Agent：

```yaml
agents:
  coder:
    name: 代码助手
    model_profile: cloud_claude     # 用 config/models.yaml 里哪个模型
    system_prompt: |                # 可选,覆盖默认 system prompt
      你是专注代码的助手,优先用 code_search 定位,read_file 精读。
    memory_policy: read_write        # none / read / read_write
    max_steps: 24                    # 一次 run 的模型往返总预算
    max_task_steps: 10               # 单个子任务的模型往返上限
```

`memory_policy`：

- `none` — 不使用长期记忆（默认，最安全）
- `read` — 会话开始时检索相关记忆注入上下文，不写入
- `read_write` — 同 read，且会话结束时把对话摘要写入记忆，下次可检索

会话、消息、记忆、工具执行审计存在 `data/agentlab.db`（SQLite，已 gitignore）。

### 子 Agent 工作区

`spawn_agent` 默认 `workspace_mode="shared"`，继承父工作区，能读取父级未提交的修改。写任务必须声明不重叠的 `file_scope`；改动立即出现在父工作区，状态为 `applied_unverified`，仍需父级调用 `integrate_agent` 提供验证命令后确认交付。运行中或取消中的写任务继续占用文件范围，父级不能同时改写该范围。关闭任务不会回滚已有文件改动。

独立实验可显式设置 `workspace_mode="worktree"`。它创建 detached HEAD 工作区，以私有 Git 快照带入暂存、未暂存和未忽略的未跟踪文件，不要求父工作区干净，不创建命名分支，也不改变父分支或索引。交付时只应用子任务相对快照的新增补丁，父级相关文件或索引发生变化会阻止回收。忽略文件不自动复制。两种工作区模式均可用于只读任务；是否只读仍单独由 `read_only` 控制。

## 浏览器控制（Playwright MCP）

默认关闭。启用后模型可打开网页、截图、读 DOM、点击、输入。

```bash
# macOS / Linux
cp config/mcp_servers.example.yaml config/mcp_servers.yaml
python -m app
```

```powershell
# Windows PowerShell
Copy-Item config\mcp_servers.example.yaml config\mcp_servers.yaml
python -m app
```

启动前把 `config/mcp_servers.yaml` 中的 `playwright.enabled` 改成 `true`。
可以先运行 `node --version` 和 `npx --version` 检查 Node.js 是否已加入
`PATH`；首次启动会下载 `@playwright/mcp` 和浏览器内核。

工作流：模型先 `browser_navigate` 打开页面 → `browser_snapshot` 拿元素 ref → 用 ref 做 `browser_click` / `browser_type`。点击/输入默认每次需审批，`browser_snapshot` 等只读工具免审批。

**两种 profile**（在 `config/mcp_servers.yaml` 的 args 配置）：

- **隔离 profile**（默认，最安全）：每次新开干净浏览器，不带任何登录态
- **named 持久化 profile**：加 `--user-data-dir data/browser-profiles/<名字>`，首次手动在弹出窗口登录，cookie 落盘后跨会话复用 —— 适合需要登录的内部 wiki / 文档站

> ⚠ **数据边界**：用云端模型 + 浏览器控制时，页面截图 / DOM / 表单内容会发送到云端模型用于推理。named profile 下登录态访问的真实数据同样会进上下文。CLI 启动时会显著提示。需严格隔离请用本地模型或隔离 profile。

> 注：Playwright 用的是自带的 Chromium，不接管你日常的 Chrome/Safari，也不读你日常浏览器的登录态。

## 配置

模型配置入口：`.env` + `config/models.yaml`。

`.env` 选 profile + 提供凭据：

```env
ACTIVE_PROFILE=cloud_claude         # 必填,对应 config/models.yaml 中的 profile

# Anthropic(代理)
ANTHROPIC_AUTH_TOKEN=cr_xxxx
ANTHROPIC_BASE_URL=https://your-proxy/api/

# 设置 Agent 默认工作范围和免审批边界(可选,默认项目根)
WORKSPACE_ROOT=/Users/you/some-project
```

`config/models.yaml` 内置 profile：

| Profile | 说明 | 需要的环境变量 |
|---|---|---|
| `cloud_claude` | Anthropic Claude Sonnet | `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY` |
| `cloud_claude_opus` | Anthropic Claude Opus | 同上 |
| `gpt_official` | OpenAI 官方 GPT（Responses API） | `OPENAI_API_KEY`（可选 `OPENAI_MODEL` 覆盖型号） |
| `siliconflow` | 硅基流动 SiliconFlow（OpenAI 兼容云端） | `SILICONFLOW_API_KEY`（可选 `SILICONFLOW_MODEL` 覆盖型号） |
| `local_qwen` | 本机 Ollama + Qwen2.5-Coder 7B | 无（需先 `ollama pull`） |
| `local_qwen3_14b` | 本机 Ollama + Qwen3 14B | 无（需先 `ollama pull qwen3:14b`） |
| `local_deepseek` | 本机 Ollama + DeepSeek-R1 7B | 无（不带工具能力） |
| `lan_qwen` | 局域网 GPU 主机 Ollama | 无（修改 profile 里的 `base_url`） |

切换 profile：改 `.env` 中 `ACTIVE_PROFILE`，或临时 `python -m app --profile xxx`。

配置文件分两类（`*.example.yaml` 入库作模板，去掉 `.example` 的真实配置 gitignore 不入库）：

| 模板 | 复制为 | 作用 |
|---|---|---|
| `config/models.yaml` | （直接用） | 模型 profile 注册表 |
| `config/agents.example.yaml` | `config/agents.yaml` | 多 Agent 定义 |
| `config/mcp_servers.example.yaml` | `config/mcp_servers.yaml` | MCP server（如 Playwright） |

### 压缩后的保留量

自动压缩的近期原文预算为 `min(输入预算的50%, 32768 tokens)`，不会随百万级窗口扩大到几十万 tokens。此预算为软上限：默认至少保留最近 6 条消息，且不切开工具调用与结果，最近用户请求和系统约束也受保护。压缩完成提示分别列出摘要、受保护内容、近期原文、system/工具定义的估算用量，各项之和是压缩后的总输入。`/context` 显示近期原文预算。

### 压缩失败后的重试

上下文压缩超时或未成功时保留原始历史。自动重试至少间隔 5 分钟，并要求预计输入量相对失败时变化至少 `max(1024, 输入预算的5%)` tokens；更新任务清单等小变化不会反复触发压缩。`/context` 显示失败原因和重试状态，`/context compact` 可立即手动重试。输入超过安全预算时仍会停止发送，提示手动重试、缩短上下文或切换模型。

### 上下文窗口来源

窗口按 `LLM_CONTEXT_SIZE` 环境变量 → profile 的 `params.context_size` → 提供方元数据 → 默认 `64000` tokens 的顺序解析，不再按模型名称猜测。

当前支持 Anthropic Models API 的 `max_input_tokens`，以及 Ollama `/api/ps` 返回的已加载模型实际 `context_length`。OpenAI 和其他兼容服务没有统一可用的窗口元数据时，使用显式配置或默认值。代理返回 HTML、接口缺失、字段非法或查询失败也会回退。默认值是未验证的兜底值，不代表模型的真实窗口；已知部署上限时应显式配置。

元数据按路由器实例隔离缓存：成功 10 分钟、缺失或失败 1 分钟。查询使用 3 秒超时并关闭自动重试；显式配置不会触发查询。`/context` 显示窗口来源：`config`、`provider` 或 `default`。

## 安全

- **路径审批**：workspace 是默认信任边界；文件读取、目录列举、代码搜索、写入和 Shell cwd 越界时使用独立审批，不能选择“本会话总是允许”；Shell 与交互式终端命令也必须逐次审批
- **写操作审批**：`write_file` / `shell` / 浏览器点击输入等默认弹方向键菜单确认；`-y` 表示用户主动自动批准所有工具，包括 workspace 外访问
- **分级风险与审计**：内置工具和 MCP 工具统一使用 ToolDescriptor 九级风险；执行成功、失败、拒绝和审批缺失都会把风险、目标、来源及脱敏摘要写入 SQLite `tool_executions`
- **凭据脱敏**：异常 traceback、工具输出、记忆写入前用正则脱敏 `Bearer xxx` / `sk-ant-xxx` / `cr_xxx` / `x-api-key=xxx` 等
- **MCP 隔离**：新 MCP server 默认禁用；启用前 CLI 展示 server / transport / 工具列表；只透传 `PATH` 给子进程
- **云端数据边界**：浏览器控制 + 云端模型时启动显著提示页面内容将离开本机
- **登录态保护**：浏览器默认用隔离 profile，不接管日常浏览器；用 named profile 复用登录态需显式配置

## 目录结构

```
AgentLab/
├── app/
│   ├── cli.py              # CLI 入口 + spinner + 任务面板 + SessionRouter 接入
│   ├── config/             # 配置加载（profile + .env）
│   ├── models/             # Anthropic / OpenAI Responses / OpenAI-compatible 三种 adapter
│   ├── agent/              # AgentSession 工具循环 + 编排(planner/executor/replanner)
│   │                       #   + 审批 + 取消 + 上下文压缩 + TaskStore + session_router
│   ├── tools/builtin/      # files / code_search / shell / interactive(终端) / todo_write
│   ├── mcp/                # MCP Client：config / manager(sync↔async 桥) / adapter
│   ├── storage/            # SQLite：sessions / messages / memories / tool_executions
│   ├── memory/             # 长期记忆策略 + 记忆注入
│   ├── skills/             # Skill 目录加载与注入
│   ├── workspace/          # workspace 根目录解析与路径校验
│   └── util/               # 方向键菜单 + 凭据脱敏
├── config/
│   ├── models.yaml                  # 模型 profile 注册表
│   ├── agents.example.yaml          # 多 Agent 定义模板
│   └── mcp_servers.example.yaml     # MCP server 模板（Playwright）
├── skills/                 # 内置 Skill（code-review / confluence-update 等）
├── scripts/
│   ├── install_local_model.sh   # macOS / Linux 一键安装 Ollama + 模型
│   ├── install_local_model.ps1  # Windows 同上
│   └── verify_local_model.sh    # 端到端 5 步验证
├── docs/
│   ├── technical_architecture.md      # 系统设计方案
│   ├── codex_runtime_learnings.md     # Codex Runtime 借鉴与 AgentLab 演进路线
│   ├── local_model_guide.md           # 本地模型落地指南
│   └── process.md                     # 开发进度与下一步计划
├── tests/                  # 离线测试（分层与运行方式见 tests/README.md）
│   ├── unit/               # 单模块规则与组件契约
│   ├── integration/        # 跨组件、SQLite 与真实 Git 工作流
│   └── support/            # 共享测试数据与模型替身
├── pyproject.toml          # Python 打包配置和 agentlab 全局命令
└── .env.example
```

## 开发

```bash
conda activate agentlab
pip install -e .
python -m pytest tests/ -q              # 完整离线回归
python -m pytest tests/unit/ -q         # 单元 / 组件测试
python -m pytest tests/integration/ -q  # 集成测试（需要 Git）
```

进度与路线见 [`docs/process.md`](docs/process.md)；总体设计方案见 [`docs/technical_architecture.md`](docs/technical_architecture.md)；Codex Runtime 的架构借鉴与 AgentLab 演进建议见 [`docs/codex_runtime_learnings.md`](docs/codex_runtime_learnings.md)。
