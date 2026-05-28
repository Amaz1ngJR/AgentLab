# AgentLab

运行在个人电脑上的本地 Agent 应用。支持对话、读写文件、调用工具，可在本地模型（Ollama）和云端模型（Claude、GPT）之间切换，只改配置不改代码。

## 功能

- **对话**：多轮上下文对话，支持中文
- **文件操作**：读文件、写文件、列目录
- **工具审批**：写操作执行前弹出 `[y]这次 / [a]总是 / [n]拒绝` 确认
- **模型切换**：通过 `--profile` 或 `.env` 切换本地/云端模型，不改代码
- **进度可见**：LLM 调用期间显示实时计时 spinner，每轮打印 token 用量与耗时

## 安装

```bash
conda activate myenv   # 或自建 venv
pip install -r requirements.txt
```

## 快速开始

### 1. 准备凭据

```bash
cp .env.example .env
```

编辑 `.env`，填入凭据。例如使用 Anthropic 代理：

```env
ACTIVE_PROFILE=cloud_claude
ANTHROPIC_AUTH_TOKEN=cr_xxxxxxxx
ANTHROPIC_BASE_URL=https://your-proxy/api/
```

或本地 Ollama（需先 `ollama pull qwen2.5-coder:7b-instruct && ollama serve`）：

```env
ACTIVE_PROFILE=local_qwen
```

### 2. 运行

```bash
python -m app
```

```
== AgentLab ==
provider : anthropic
model    : claude-sonnet-4-6
profile  : cloud_claude

you> 看一下 README.md 的开头几行
  ✻ thinking (1.4s)
  · tool read_file({"path": "README.md"})
    [ok] (1ms) # AgentLab ...
  ✻ thinking (1.2s)

README 是一份本地 Agent 开发环境的说明文档...
  [stats] turn 2.7s in=98 out=67 | session 2.7s in=98 out=67

you> 把 hello 写到 /tmp/x.txt
  ✻ thinking (0.9s)
  · tool write_file({"content": "hello", "path": "/tmp/x.txt"})
  ? 允许执行 write_file?  [y]这次  [a]本会话总是  [n]拒绝 > y
    [ok] (1ms) wrote 5 chars to /tmp/x.txt

you> /reset   # 清空会话历史
you> exit
```

## 命令行参数

```bash
python -m app                          # 交互式 REPL
python -m app -p "帮我看 README.md"    # 单次 prompt 后退出
python -m app -y                       # 自动放行所有工具（跳过审批）
python -m app --profile local_qwen     # 使用指定模型 profile
```

## 配置

唯一配置入口：`.env` + `config/models.yaml`。

`.env` 提供凭据和选择激活的 profile：

```env
ACTIVE_PROFILE=cloud_claude       # 必填，对应 config/models.yaml 中的 profile
ANTHROPIC_AUTH_TOKEN=cr_xxxx      # cloud_claude profile 需要
ANTHROPIC_BASE_URL=https://...    # 自建代理时填
```

`config/models.yaml` 定义所有 profile：

| Profile | 说明 | 需要的环境变量 |
|---|---|---|
| `cloud_claude` | Anthropic Claude Sonnet | `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY` |
| `cloud_claude_opus` | Anthropic Claude Opus | 同上 |
| `local_qwen` | 本机 Ollama + Qwen2.5-Coder 7B | 无 |
| `local_qwen14b` | 本机 Ollama + Qwen2.5-Coder 14B | 无 |
| `lan_qwen` | 局域网 GPU 主机 Ollama | 无（修改 profile 中的 `base_url`） |

切换 profile：改 `.env` 里 `ACTIVE_PROFILE`，或用 `--profile` 临时覆盖：

```bash
python -m app --profile local_qwen
```

## 目录结构

```
AgentLab/
├── app/
│   ├── cli.py              # CLI 入口
│   ├── config/             # 配置加载
│   ├── models/             # Provider adapters（Anthropic / OpenAI-compatible）
│   ├── agent/              # Agent 循环与审批策略
│   └── tools/builtin/      # 内置工具
├── config/
│   ├── models.yaml         # 模型 profile 注册表
│   └── app.example.yaml    # 应用配置模板
├── docs/
│   ├── technical_architecture.md  # 系统设计方案
│   └── process.md                 # 开发进度与下一步计划
├── tests/unit/             # 离线单元测试
└── .env.example            # 环境变量模板
```

## 开发

```bash
python -m pytest tests/unit/ -v
```
