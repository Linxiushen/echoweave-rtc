# EchoWeave-RTC

[![CI](https://github.com/Linxiushen/echoweave-rtc/actions/workflows/ci.yml/badge.svg)](https://github.com/Linxiushen/echoweave-rtc/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/Linxiushen/echoweave-rtc)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10--3.12-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-research%20preview-f59e0b)](docs/OPERATIONS.md)

EchoWeave-RTC 0.2 是一个“经本人授权、持续标识为 AI”的实时语音与数字头像
Agent 编排框架。它把端点检测、语音识别、流式回答、语音合成、口型视频、
打断、背压与可观测性统一在一条可测试的会话链路里。

> **授权与披露是系统边界，不是可选文案。** 本项目不支持秘密复制、冒充或
> 欺骗性再现真人。真人 persona 只能使用本人或合法权利人的明确授权素材，并
> 从带授权范围、有效期、素材哈希和服务端签名的清单加载。浏览器和生成视频
> 必须持续显示 AI / synthetic media 标识。

![EchoWeave-RTC 实时控制台](docs/assets/echoweave-console.jpg)

> 截图展示无需真人数据或模型密钥的虚构 `demo` 链路。完整模型组合需要按部署
> 文档单独配置 GPU worker，并以真实硬件基准结果为准。

## 0.2 能力概览

- 自研 `EW` v1 WebSocket 控制与二进制媒体协议，支持版本和能力协商。
- 每个服务端事件都有会话 ID、事件 ID、单调序号和服务端时间；错误带固定
  分类、可重试标记和安全关联 ID。
- VAD 端点状态机、短 pre-roll、550 ms 尾静音和 generation epoch 打断。
- 有界语音短句队列、单写者 WebSocket 发送泵、客户端缓冲保护和慢消费者失败
  隔离，避免无限排队。
- ASR、LLM 首 token/空闲、TTS、头像、整轮、发送、取消和资源关闭均有截止时间。
- 浏览器优先使用 AudioWorklet，把麦克风实时重采样为 16 kHz、20 ms PCM16 帧；
  不支持时降级到 ScriptProcessor。
- `turn.metrics` 返回首 token、文本完成和端到端耗时；网关提供有界 JSON 指标、
  liveness、readiness 和并发压测/故障注入工具。
- DeepSeek SSE、Qwen3-ASR、VoxCPM2、SoulX worker 均通过隔离适配器接入；
  HTTP 连接复用且响应大小、类型和顺序受限。
- Nuwa 只用于离线、经人工复核的 persona 提炼，不进入实时热路径，也不证明授权。
- 完全无需 GPU 或 API key 的安全演示模式，可验证端到端协议与 UI。
- 自适应实时控制台包含链路阶段、能力协商、网络 RTT、首字/整轮延迟、恢复动作、
  键盘与屏幕阅读器支持；移动端保留加密状态并避免输入聚焦缩放。

实时链路：

```text
AudioWorklet -> EW/PCM16 -> VAD -> Qwen ASR -> DeepSeek SSE
                                            -> semantic chunks -> VoxCPM2
                                                               -> SoulX
                                                               -> bounded playout
```

## 峰哥数字人训练研究线

仓库正在用“峰哥亡命天涯”公开研究线检验一条**授权门控、本地处理、不会把生物特征提交到 GitHub**的真人数字人工作流。这个案例不展示无法验证的“克隆完成”结论，而是把公开资料研究、授权准入、逐段人工审核、数据集导出、VoxCPM2 基线和训练硬件门禁串成一条可审计流程。

| 里程碑 | 当前结果 |
|---|---|
| 人工审核数据 | 22 段，309.42 秒；私有保存 |
| VoxCPM2 基线 | 已完成 48 kHz PCM16 零样本评测；不是 LoRA |
| 内容回译 | Qwen3-ASR 归一化匹配率 1.000000 |
| VoxCPM2 LoRA | 尚未执行；等待正式授权工件和合格 Linux GPU |
| 公开发布 | 未注册真人 persona，未提交声音、视频或适配器 |

完整实验记录、公开指标和隐私边界见[经授权门控的创作者数字分身实验：峰哥研究线](docs/FENGGE_DIGITAL_TWIN_LAB.md)。

## 真人数字分身需要什么输入

虚构的 `demo` persona 不需要真人信息。创建真人数字分身至少需要下列**经本人
授权**的输入，而且应只收集完成用途所需的最小数据：

1. 经人工复核的 persona 资料或 `SKILL.md`；
2. 明确列出对话、音色迁移、头像动画及第三方模型处理范围的授权记录；
3. 经授权的面部参考素材；
4. 经授权的干净声音参考和准确文本；
5. 有效期、撤回机制、素材 SHA-256、审核/身份验证记录和服务端签名。

实时会话只接受已注册的 `persona_id`，不接受访客临时上传陌生人的脸或声音。
详细流程见[授权与安全边界](docs/SECURITY.md)和
[Nuwa 离线工作流](docs/NUWA_WORKFLOW.md)。

## 六项组件的准确来源

| 作用 | 官方来源 |
|---|---|
| 用户说完检测 | [snakers4/silero-vad v5.1.2](https://github.com/snakers4/silero-vad/tree/v5.1.2) |
| ASR | [Qwen/Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) |
| 回答 | [DeepSeek API `deepseek-v4-flash`](https://api-docs.deepseek.com/) |
| TTS / 授权音色 | [openbmb/VoxCPM2](https://huggingface.co/openbmb/VoxCPM2) |
| Persona 方法论提炼 | [alchaincyf/nuwa-skill](https://github.com/alchaincyf/nuwa-skill) |
| 口型和表情 | [Soul-AILab/SoulX-FlashHead-1_3B](https://huggingface.co/Soul-AILab/SoulX-FlashHead-1_3B) |

Silero v5 在 Hugging Face 上没有官方同名仓库，第三方转换不作为默认来源；
SoulX 的准确仓库名使用 `1_3B`；Nuwa 是离线 Agent Skill，不是模型服务。

## 3 分钟运行

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\echoweave serve
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，确认 AI 身份披露后开始。
默认 demo 使用虚构角色、浏览器 TTS 和合成头像。文字输入可立即跑完整状态机；
允许麦克风后可验证 AudioWorklet、VAD、端点检测和 ASR 边界。

健康与观测端点：

| 路径 | 用途 |
|---|---|
| `/api/health`、`/api/health/live` | 进程 liveness；不代表模型依赖可用 |
| `/api/ready`、`/api/health/ready` | 运行时适配器构造 readiness；未就绪返回 503 |
| `/api/metrics` | 有界 JSON 指标；配置访问令牌后要求 Bearer 鉴权 |

运行检查：

```powershell
.\.venv\Scripts\python -m pytest -q
node --check src/echoweave/web/app.js
node --check src/echoweave/web/mic-worklet.js
node tests/js/test_app_lifecycle.mjs
node tests/js/test_frontend_contract.mjs
node tests/js/test_mic_worklet.mjs
```

提交和拉取请求会运行 [GitHub Actions CI](.github/workflows/ci.yml)，覆盖 Python
测试、Ruff、浏览器契约测试和可复现 wheel 校验。

## Docker Compose

Compose 的网关进程监听容器内所有接口，因此必须先生成访问令牌；宿主机端口
默认只发布到 `127.0.0.1`，公网部署应在前面使用 HTTPS/WSS 反向代理：

```powershell
Copy-Item .env.example .env
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"
(Get-Content .env) -replace '^ECHOWEAVE_ACCESS_TOKEN=.*$', "ECHOWEAVE_ACCESS_TOKEN=$token" |
  Set-Content .env -Encoding utf8
docker compose up --build
Write-Host "打开 http://127.0.0.1:8765/#token=$token"
```

Compose 的容器健康检查使用 `/api/ready`，因此只有运行时适配器成功构造后才会
进入 healthy。Compose 仅因宿主端口被硬绑定到 loopback，才显式开启私网明文
例外；修改端口发布范围前必须移除此例外，并配置原生 TLS 或精确的可信代理
IP/CIDR。应用仍会拒绝任何公网明文请求。`.dockerignore` 使用构建上下文白名单，
不会把 `.env`、真人素材、模型缓存或本地虚拟环境发送给 Docker builder。

## 完整模型模式

复制 `.env.example` 为 `.env`，按[部署文档](docs/DEPLOYMENT.md)将 Qwen、
VoxCPM2、SoulX 分别启动为隔离 GPU worker，再选择：

```text
ECHOWEAVE_VAD_BACKEND=silero_v5
ECHOWEAVE_ASR_BACKEND=qwen_http
ECHOWEAVE_LLM_BACKEND=deepseek
ECHOWEAVE_TTS_BACKEND=voxcpm_http
ECHOWEAVE_AVATAR_BACKEND=soulx_http
```

流式文本 LLM 也可切换到经部署方资格验证的 OpenAI-compatible HTTPS 网关；该模式不会
发送 DeepSeek 私有字段。它只代表文本对话协议兼容，不自动代表 ASR、TTS、图片、
视频或 SoulX 协议兼容。配置和独立验收要求见[部署文档](docs/DEPLOYMENT.md)。

临时公开虚构 `demo` 可使用[Cloudflare Quick Tunnel 安全运行手册](docs/CLOUDFLARE_DEMO.md)。
GitHub Pages 不能运行 Python、WebSocket 或 GPU worker，因此只承担源码与 CI。

所有秘密只通过服务端环境变量或秘密管理器注入。DeepSeek key 只从
`DEEPSEEK_API_KEY` 读取；任何曾出现在聊天、日志、工单或 Git 历史里的 key 都
必须先撤销并轮换，不能继续使用，也不能提交 `.env`。

启用真人 persona 时还要设置至少 32 字节的
`ECHOWEAVE_CONSENT_SIGNING_KEY`。授权 revision 与不可逆撤回墓碑写入
`ECHOWEAVE_CONSENT_STATE_PATH`，该路径应放在持久、受备份保护的私有卷上。
真人 persona 还强制要求独立的 `ECHOWEAVE_SESSION_SIGNING_KEY`；会话端只能
提交由它签发的短期、一次性、绑定主体/persona/能力范围的 session token。
共享 `ECHOWEAVE_ACCESS_TOKEN` 仅保留给 `demo`，不能访问任何真人 persona。
对外绑定还必须配置精确的 `ECHOWEAVE_ALLOWED_ORIGINS`，真人 persona 必须
显式加入 `ECHOWEAVE_ALLOWED_PERSONAS`。VoxCPM 与 SoulX 应分别使用
`VOXCPM_WORKER_TOKEN` 和 `SOULX_WORKER_TOKEN`，网关会为每次调用签发绑定授权
revision、scope、素材 SHA-256、audience、有效期和防重放 JTI 的 consent assertion。

## 基准测试与生产声明

`scripts/benchmark_realtime.py` 可并发测量连接就绪、首 token、文本完成和整轮
p50/p95/p99，并可在隔离预发布环境注入延迟、非法控制消息和断线。示例：

```powershell
$env:ECHOWEAVE_ACCESS_TOKEN = "<staging-token>"
.\.venv\Scripts\python scripts\benchmark_realtime.py `
  --url ws://127.0.0.1:8765/ws --workers 4 --turns 10 `
  --output runtime\benchmark-baseline.json
```

仓库中的 SLO 是初始目标，不是对任意硬件或模型组合的性能承诺。发布前应按
[运维与 SLO 指南](docs/OPERATIONS.md)在同型号 GPU、固定语料与真实 worker 上
建立至少七天的基线，并保留容量余量。

## 文档

- [Press kit 与中英文发布素材](docs/PRESS_KIT.md)
- [架构、背压与截止时间](docs/ARCHITECTURE.md)
- [媒体与控制协议](docs/PROTOCOL.md)
- [模型、许可证与硬件](docs/MODELS.md)
- [VoxCPM2 训练可行性审计](docs/VOXCPM2_TRAINING_AUDIT.md)
- [完整部署](docs/DEPLOYMENT.md)
- [远程 GPU 复现、预检与模型验收](gpu_worker_pack/README.md)
- [授权与安全边界](docs/SECURITY.md)
- [Nuwa 离线蒸馏与人工复核](docs/NUWA_WORKFLOW.md)
- [经授权门控的创作者数字分身实验：峰哥研究线](docs/FENGGE_DIGITAL_TWIN_LAB.md)
- [公开人物研究档案：峰哥亡命天涯](docs/research/fengge-wangmingtianya-public-profile.md)
- [运维、基准测试与 SLO](docs/OPERATIONS.md)
- [真人 persona 注册](personas/README.md)

## 开源协作

欢迎提交协议、实时性能、可观测性、适配器、安全和无障碍方面的改进。开始前请
阅读[贡献指南](CONTRIBUTING.md)和[行为准则](CODE_OF_CONDUCT.md)。漏洞、凭证
泄露或授权工件暴露请通过 [GitHub 私密安全报告](https://github.com/Linxiushen/echoweave-rtc/security/advisories/new)
提交，不要创建公开 issue。任何贡献都不得包含真人声音、视频、生物特征、
persona adapter、私有转写或密钥。

## 硬件

4 GB GPU 可以运行网关、测试和演示，但不能同时常驻 Qwen3-ASR-1.7B、
VoxCPM2 和 SoulX。完整本地实时链路建议让 ASR/TTS 使用一张较大显存 GPU，
并让 SoulX Lite 独占一张 RTX 4090 24 GB 级 GPU。具体显存需求取决于模型
revision、精度、并发和 worker 实现，必须实测。

## Private VoxCPM2 training

See [the private, reviewed VoxCPM2 training workflow](docs/VOXCPM2_PRIVATE_TRAINING.md)
for controlled acquisition of authorized public source media, Silero
segmentation, Qwen transcription, subtitle or audio-only evidence, human review,
trusted dataset-plan export and owned-GPU execution.

## License

EchoWeave-RTC code is Apache-2.0. Third-party models keep their own licenses and
acceptable-use requirements. A permissive software license never grants rights
to a person's voice, face, writings or identity.
