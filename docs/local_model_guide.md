# 本地模型落地指导方案

> 把项目从"用 Claude API 跑通"切到"完全离线本地运行"的实操手册。
>
> 适用硬件:Mac M3 16GB 统一内存,或 NVIDIA 5060Ti 16GB 显存 + 32GB 系统内存。

---

## 1. 决策:不内置模型权重

**结论:AgentLab 不应该把模型权重打包进项目。** 由 Ollama / LM Studio 等专门
工具管理权重,AgentLab 只存模型 profile。

理由:

| 维度 | 内置权重的问题 |
|---|---|
| 包体积 | 一个 7B Q4 模型 ≈ 4.7 GB,代码 ≈ 几百 KB。git / PyPI 不适合分发权重 |
| 许可证 | Qwen 用 Tongyi Qianwen License;Llama 用 Llama Community License;分发权重需要额外签发,法律风险大 |
| 平台差异 | Mac 用 Metal 加速;Windows/Linux 用 CUDA。权重格式 (gguf) 跨平台一致,但运行时优化由 Ollama / llama.cpp 处理,我们无法替它们做 |
| 更新成本 | 模型版本(Qwen2.5 → Qwen3)发布时,内置版本必须重打包并强制升级 |
| 设计原则 | `docs/technical_architecture.md` §4.3 明确 "AgentLab 只存模型 profile,不复制权重文件" |

**替代方案:**
1. 用现成推理框架(本指南推荐 Ollama)管理权重
2. AgentLab 提供一键脚本 + 清晰的连通性检测 + 错误引导
3. `config/models.yaml` 已经有几个 `local_*` profile,改 `.env` 切过去即可

---

## 2. 硬件评估

### 2.1 Mac M3 16GB(统一内存)

| 模型(GGUF Q4_K_M 量化) | 文件大小 | 实际 RAM 占用 | 推理速度 | 工具调用 |
|---|---|---|---|---|
| **Qwen2.5-Coder 7B Instruct** | 4.7 GB | ~6 GB | ~30 tok/s | ✅ 优秀 |
| Qwen2.5 7B Instruct | 4.7 GB | ~6 GB | ~30 tok/s | ✅ 良好 |
| Llama 3.1 8B Instruct | 4.9 GB | ~6 GB | ~25 tok/s | ✅ 良好 |
| Mistral 7B v0.3 Instruct | 4.4 GB | ~5.5 GB | ~30 tok/s | ✅ 良好 |
| DeepSeek-R1-Distill-Qwen 7B | 4.7 GB | ~6 GB | ~25 tok/s | ⚠️ 不稳(reasoning 模型) |
| Qwen2.5-Coder 14B Instruct | 9.0 GB | ~10 GB | ~12 tok/s | ✅ 优秀 |

**结论(Mac M3 16GB):**
- 推荐起步:**Qwen2.5-Coder 7B Instruct Q4_K_M** —— 工具调用最稳、编码强、剩 10GB 给系统
- 14B 能跑但紧张,留给"代码任务太复杂时偶尔用"的场景
- 系统建议保留 ≥ 6GB 给浏览器 / IDE / Spotlight 等

### 2.2 NVIDIA 5060Ti 16GB 显存

5060Ti 显存大,可以跑更大模型,且速度比 Mac 快 2-3 倍。

| 模型 | 量化 | 显存占用 | 速度 |
|---|---|---|---|
| **Qwen2.5-Coder 14B Instruct** | Q4_K_M | ~9 GB | ~50 tok/s |
| Qwen2.5-Coder 14B Instruct | Q5_K_M | ~10 GB | ~45 tok/s |
| Qwen2.5-Coder 32B Instruct | Q4_K_M | ~19 GB(超出,要分层 offload) | ~10 tok/s |
| Llama 3.1 8B Instruct | Q5_K_M | ~5.5 GB | ~70 tok/s |

**结论(5060Ti 16GB):**
- 推荐:**Qwen2.5-Coder 14B Instruct Q4_K_M** —— 编码能力比 7B 高一档,显存还有富余
- 32B 不建议(必须 offload 到内存,反而慢)

### 2.3 跨设备建议

如果你两台机器都有,**最佳实践是 Mac 当客户端 + 5060Ti 当推理服务器**:
- Mac 跑 AgentLab CLI(轻量),写代码不卡
- 5060Ti 主机跑 Ollama 服务,模型在 GPU 上跑得快
- 通过局域网调用:`config/models.yaml` 的 `lan_qwen` profile 已配好

---

## 3. 推理框架:Ollama(强推)

### 3.1 为什么 Ollama

| 框架 | 安装 | 工具调用 | 跨平台 | API |
|---|---|---|---|---|
| **Ollama** | 一键 | ✅ 原生 | Mac/Win/Linux | OpenAI-compatible |
| LM Studio | GUI | ✅ | Mac/Win/Linux | OpenAI-compatible(端口可配) |
| llama.cpp 直接 | 编译 | ⚠️ 需手写 server 配置 | 全平台 | 自带 server,需 OpenAI 兼容补丁 |
| vLLM | 复杂 | ✅ | Linux + NVIDIA | OpenAI-compatible(性能最好) |

**Ollama 优势:**
- macOS 一行命令装好;Windows 有 .exe 安装包
- 自动用 Metal(Mac) / CUDA(NVIDIA) 加速,无需手动配
- 模型管理像 docker:`ollama pull` / `ollama list` / `ollama rm`
- 暴露 OpenAI-compatible `/v1/chat/completions`,AgentLab 现成的 `OpenAICompatibleAdapter` 直接对接,代码零改

**何时不用 Ollama:**
- 5060Ti 极致追求 throughput → vLLM(P3 阶段评估)
- 已经用 LM Studio 习惯了 → 改 `config/models.yaml` 的 `base_url` 即可

---

## 4. 落地步骤

### 4.1 macOS(Mac M3 单机模式)

```bash
# 1. 安装 Ollama
brew install ollama
# 或者 https://ollama.com/download 下 .dmg 安装(双击拖进 Applications)

# 2. 启动 Ollama 服务(开机自启,后台运行)
ollama serve &
# 或者用 launchd 自动启动:macOS 上 Ollama.app 启动后会自动监听 11434

# 3. 拉模型(首次约 4.7GB,Mac 国内网络可能要 5-10 分钟)
ollama pull qwen2.5-coder:7b-instruct

# 4. 验证模型本身能跑
ollama run qwen2.5-coder:7b-instruct "用一句话介绍你自己"
# 看到回复就 OK

# 5. 切换 AgentLab 到本地 profile
# 编辑 .env,改一行:
echo "ACTIVE_PROFILE=local_qwen" > .env
# 注意:本地 profile 不需要 ANTHROPIC_AUTH_TOKEN 等

# 6. 启动 AgentLab
python -m app
```

### 4.2 Windows(5060Ti 主机)

```powershell
# 1. 下载安装 Ollama: https://ollama.com/download/windows
#    .exe 安装包,装完会自动启动服务并监听 11434

# 2. 验证服务运行
curl http://localhost:11434/api/tags

# 3. 拉模型(14B 约 9GB,GPU 16GB 充足)
ollama pull qwen2.5-coder:14b-instruct

# 4. 测试
ollama run qwen2.5-coder:14b-instruct "你好"

# 5. 切到 AgentLab 的 local_qwen14b profile
echo "ACTIVE_PROFILE=local_qwen14b" > .env
python -m app
```

### 4.3 Mac → 5060Ti 局域网模式(推荐)

5060Ti 比 Mac 快 2-3 倍,把它当推理服务器,Mac 当客户端:

```bash
# === 5060Ti 主机(Windows / Linux) ===
# 让 Ollama 监听所有网卡,不只是 localhost
# Windows PowerShell:
$env:OLLAMA_HOST="0.0.0.0:11434"
ollama serve
# 或者改系统环境变量永久生效

# 查 5060Ti 主机的局域网 IP(假设是 192.168.1.20)
ipconfig | findstr IPv4

# 拉好需要的模型
ollama pull qwen2.5-coder:14b-instruct

# === Mac 客户端 ===
# 编辑 config/models.yaml,把 lan_qwen 的 base_url 改成你的 5060Ti IP:
#   lan_qwen:
#     base_url: http://192.168.1.20:11434/v1
#
# 然后 .env:
echo "ACTIVE_PROFILE=lan_qwen" > .env
python -m app
```

⚠️ **安全注意**:`OLLAMA_HOST=0.0.0.0` 让局域网内任何机器都能调你的 Ollama。
只在受信家庭网络这么做;公网或公司网络应该用 VPN 或反向代理加鉴权。

---

## 5. 模型选择决策树

```
你主要用 AgentLab 做什么?
├── 写代码 / 改代码 / 看代码  →  Qwen2.5-Coder 系列
│   ├── 16GB 显存/内存          →  qwen2.5-coder:7b-instruct  (起步)
│   └── 32GB 内存或 16GB 显存   →  qwen2.5-coder:14b-instruct (更强)
│
├── 通用问答 / 中文对话         →  qwen2.5:7b-instruct
│
├── 推理 / 数学 / 复杂规划      →  deepseek-r1:7b
│   (注意:reasoning 模型工具调用不稳,Agent 场景可能需要降级到 Qwen)
│
└── 英文 / 国际化场景           →  llama3.1:8b 或 mistral:7b
```

**当前 `config/models.yaml` 已注册的本地 profile:**

| Profile | 模型 | 备注 |
|---|---|---|
| `local_qwen` | qwen2.5-coder:7b-instruct | 推荐起步 |
| `local_qwen14b` | qwen2.5-coder:14b-instruct | 更强但慢 |
| `local_deepseek` | deepseek-r1:7b | 推理任务,不带工具能力 |
| `lan_qwen` | qwen2.5-coder:7b-instruct | 局域网 GPU 主机版 |

要加新模型,只在 `config/models.yaml` 加一段就行,业务代码不用动。

---

## 6. 验证清单

切换后,按顺序跑这几步,确认全链路通畅:

```bash
# 1. Ollama 服务存活
curl -s http://localhost:11434/api/tags | head -50
# 期望:JSON 列出已下载的模型;空 list 说明还没拉模型

# 2. 模型本身能推理
ollama run qwen2.5-coder:7b-instruct "回复 OK"
# 期望:看到 "OK"

# 3. AgentLab 配置加载
python -c "from app.config.loader import load_config; print(load_config())"
# 期望:provider=openai_compatible / model=qwen2.5-coder:7b-instruct

# 4. AgentLab 一次性 prompt(简单对话,无工具)
python -m app -p "你好" -y
# 期望:模型回复 + [stats] 行;耗时通常 <5s

# 5. AgentLab 工具调用闭环
python -m app -p "用 list_dir 列出当前目录,告诉我有哪些文件" -y
# 期望:· tool list_dir({}) → [ok] 输出 → 模型总结

# 6. 任务清单
python -m app -p "用 todo_write 列 3 个虚构任务,然后总结" -y
# 期望:屏幕上显示 3 tasks (...) 任务面板
```

任何一步失败,看下面的故障排查。

---

## 7. 故障排查

### "Connection refused / Errno 61"
→ Ollama 服务没起。运行 `ollama serve` 或检查 macOS 任务栏 Ollama 图标。

### "model 'xxx' not found"
→ 模型没拉过。运行 `ollama list` 看现有的,`ollama pull <name>` 拉。

### "tool calls returned empty / 模型不调工具"
→ 选的模型不支持 tool calling。换 `qwen2.5-coder:7b-instruct`(明确支持),
   或者去 https://ollama.com/library 看模型卡片是否标 `tools` 能力。

### Mac 上 Ollama 推理卡顿 / 频繁掉速
→ 内存不够。关一些占用大的应用(Chrome / Docker / 大型 IDE);或换更小量化:
   `ollama pull qwen2.5-coder:7b-instruct-q3_K_M`(更省 1GB,精度略降)。

### 局域网模式连不上
→ 5060Ti 主机防火墙挡了 11434 端口。Windows 防火墙加入站规则放行;
   或 `OLLAMA_HOST` 没设到 `0.0.0.0`(设到 `127.0.0.1` 只本机能访问)。

### 字体 / 中文显示乱码
→ 终端 locale 不对。`export LANG=en_US.UTF-8` 或 `zh_CN.UTF-8`。

### Ollama API token 提示
→ Ollama 默认无鉴权;若用了反向代理加了鉴权,把 token 填到 `.env` 的
   `OLLAMA_API_KEY`(对应 profile 里的 `api_key_env`)。

---

## 8. 性能基准(参考)

实测数据(Qwen2.5-Coder 7B Instruct Q4_K_M,问 "用 Python 写一个二分查找"):

| 设备 | 首字延迟 | 速率 | 200 token 总耗时 |
|---|---|---|---|
| Mac M3 16GB | ~1.0s | ~30 tok/s | ~7.5s |
| 5060Ti(本地) | ~0.4s | ~75 tok/s | ~3.0s |
| 5060Ti(局域网→Mac) | ~0.5s | ~70 tok/s | ~3.3s |

参考标杆:Claude Sonnet 4.6(代理)~4-10s 出 200 tokens。
本地 7B 在简单任务上和云端体验相当;复杂代码理解 Claude 仍然占优。

---

## 9. 未来路线

- **更稳的 server**:vLLM 取代 Ollama(只在 5060Ti 上,throughput 高 2-3 倍),P3 阶段评估
- **多模型路由**:简单任务走本地,复杂任务路由到 Claude(成本 / 隐私权衡),技术方案 §16
- **Skill 加载**:本地模型 + 项目特定 SKILL.md 提示词,用 7B 模型实现接近 Claude 的工程效果(P2)

---

## 10. 相关文件

- 配置:[`config/models.yaml`](../config/models.yaml) 看 `local_*` profile
- 适配器:[`app/models/compatible_adapter.py`](../app/models/compatible_adapter.py) 实现 OpenAI-compatible 流式 + 工具循环
- 安装脚本(后续会加):`scripts/install_local_model.sh`

---

## 11. Ollama 官方参考

- 安装:https://ollama.com/download
- 模型库:https://ollama.com/library(看哪些模型支持 tools)
- OpenAI 兼容 API:https://docs.ollama.com/api/openai-compatibility
- 工具调用文档:https://docs.ollama.com/capabilities/tool-calling
