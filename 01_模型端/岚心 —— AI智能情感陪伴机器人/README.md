# 云端语音女友第一版

这是一个本地网页试听台：输入文字后，由 OpenAI 兼容中转站调用大语言模型生成回复，再交给豆包 TTS 2.0 合成女声并在浏览器播放；ESP32 上传的 16kHz WAV 可通过豆包流式 STT 识别。

现在也支持硬件机器人：ESP32-S3 用 INMP441 录音上传，屏幕显示识别文字和小岚回复，回复语音由**电脑扬声器**播放。屏幕会显示人物图片，并在录音或电脑播放回复时切换闭嘴、微张、张嘴三种口型。成品不使用功放和喇叭，外接按钮接 GPIO13，开发板 BOOT 键仍可备用。

当前版本不在 ESP32 上运行识别模型，录音由开发板采集后上传到本地服务，再由配置的 STT 提供商识别。人格和长期记忆采用本地 Markdown 工作区文件，风格接近 OpenClaw 的可读配置文件。

## 启动

在模型端项目目录运行（也可以直接双击 `启动小岚.bat`）：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app
```

浏览器打开 <http://127.0.0.1:8001>。

如果 PowerShell 不允许激活虚拟环境，可以直接运行：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m app
```

## 配置

真实密钥只放在项目根目录的 `.env`，不要放进网页代码，也不要把 `.env` 分享出去。至少需要填写：

```text
LLM_BASE_URL=https://你的中转站地址/v1
LLM_API_KEY=你的中转站密钥
LLM_MODEL=deepseek-v4-pro

DOUBAO_TTS_PROVIDER=http
DOUBAO_TTS_BASE_URL=https://openspeech.bytedance.com/api/v3/tts/unidirectional
DOUBAO_TTS_API_KEY=你的豆包TTS密钥
DOUBAO_TTS_RESOURCE_ID=seed-tts-2.0
DOUBAO_TTS_VOICE_ID=你的音色ID
DOUBAO_TTS_FORMAT=mp3
DOUBAO_TTS_SAMPLE_RATE=24000

# ESP32 麦克风语音识别（豆包流式 WebSocket）
STT_PROVIDER=doubao_streaming
STT_BASE_URL=wss://openspeech.bytedance.com/api/v3/sauc/bigmodel
STT_API_KEY=你的豆包新版 X-Api-Key
STT_APP_ID=旧版控制台 App ID（新版留空）
STT_ACCESS_KEY=旧版控制台 Access Key（新版留空）
STT_RESOURCE_ID=volc.seedasr.sauc.duration
STT_MODEL=bigmodel
```

改完 `.env` 后要重启服务，配置才会重新读取。浏览器和未来的 PCB 都不会直接接触这些密钥。

大模型的三项（`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`）可以暂时留空：语音识别和屏幕显示不依赖它，机器人会显示识别文字并提示“回复失败：……”，等接口恢复后再填上即可。

## 机器人：按住 BOOT 说话

固件在 `D:\Arduino\xiaolan_voice_robot\xiaolan_voice_robot.ino`，接线沿用已经确认过的那套：

| 模块 | 连线 |
|---|---|
| TFT `GND/VCC/SCL/SDA/RES/DC/CS/BL` | `GND / 3V3 / GPIO12 / GPIO11 / GPIO8 / GPIO9 / GPIO10 / 3V3` |
| INMP441 `SCK/WS/SD/L+R/VDD/GND` | `GPIO4 / GPIO5 / GPIO6 / GND / 3V3 / GND` |
| 外接说话键 | `GPIO13` 与 `GND`；BOOT 键（GPIO0）仍可备用 |

使用流程：

```text
按住外接按钮或 BOOT 说话（最长 15 秒）
  → 松开自动上传到电脑
  → 屏幕显示“你说：……”（识别文字）
  → 屏幕显示“小岚回复：……”
  → 回复语音从电脑扬声器播放
```

- 待机时屏幕显示闭嘴人物；录音时显示人物和电平条；电脑播放回复时切换口型。
- 人物图片资源在 `assets/` 中。录音开始时只刷新一次人物图，录音过程中不要反复刷新整张图片，否则会占用 CPU/SPI，可能导致 INMP441 丢采样、豆包识别失败。
- 显示回复时按一下 `BOOT` 可以跳到下一页或直接回到待机。
- 电脑需要和开发板连同一个 2.4GHz Wi-Fi，并保持 `启动小岚.bat` 运行。
- 固件里的 `SERVER_HOST` 要填电脑当前的局域网地址（`ipconfig` 看 WLAN 的 IPv4），换网络后需要重新烧录或修改这一行。
- 大模型接口暂时不可用时，屏幕仍会显示识别文字，并提示“回复失败：……”，麦克风这条路可以先单独验证。

烧录选项：`ESP32S3 Dev Module`、`Flash Size 16MB`、`PSRAM OPI PSRAM`、`Partition Scheme Huge APP`、`USB CDC On Boot Enabled`，端口选 CH340 对应的 COM 口。

如果机器人说话后电脑没声音，先单独验证电脑扬声器：

```powershell
.venv\Scripts\python.exe 测试扬声器.py
```

能听到 3 秒长音，说明服务播放没问题，再去看系统音量和默认输出设备。ESP32 本身不连接功放或喇叭。

## 本地人格与语音文件

## 屏幕人物图片

最终固件使用三张透明 PNG 口型图：`assets/闭嘴.png`、`assets/微张.png`、`assets/张嘴.png`。它们会被转换为 `xiaolan_faces.h` 并编译进 ESP32 固件，不需要 SD 卡。

- 待机：显示闭嘴图片。
- 录音：显示人物和麦克风电平条。
- 电脑播放回复：按较慢节奏切换三种口型。
- 录音过程中不要反复刷新整张人物图片。图片刷新会占用 CPU/SPI，可能导致 INMP441 丢采样，豆包语音识别返回 502 或没有文字。

服务第一次启动时，会在项目根目录自动创建下面这些 Markdown 文件。你直接用记事本、VS Code 或其他编辑器修改它们即可。每次聊天请求前都会重新读取，所以改完后不需要重启服务。

- `AGENTS.md`：工作区总规则、文件编辑原则和安全规则
- `SOUL.md`：核心人格、身份和稳定的相处底色
- `IDENTITY.md`：名字、类型、称呼、声音和真实性说明
- `USER.md`：你的基本资料，例如称呼、时区、职业和长期目标
- `MEMORY.md`：长期记忆，例如偏好、经历、重要约定和共同经历
- `RELATIONSHIP.md`：你们的关系设定、亲密程度和边界
- `STYLE.md`：说话口气、回复长度、常用表达和示例对话
- `VOICE.md`：语音情绪、停顿和豆包语音指令参考
- `TOOLS.md`：当前能力、不能做的事情和未来工具说明

你可以直接这样写：

```markdown
# 长期记忆

## 用户偏好
- 我喜欢晚上听轻音乐。
- 我不喜欢别人连续追问。

## 重要约定
- 我说“晚安”时，希望她简短地道晚安，不要展开新话题。
```

普通聊天不会自动改写这些文件。这样做是有意的：随口说的一句话不应该立刻变成永久记忆。你想让她学习什么，就手动写进对应文件；想让设定失效，就直接修改或删除那一行。

旧版本的 `soul.md` 和 `memory.json` 会在首次升级时迁移到大写 Markdown 文件。旧文件会被保存在 `_legacy` 文件夹中，之后服务不再读取它们。

## 运行行为

- 当前对话上下文：只保留服务进程中的最近若干轮，重启后清空。
- 本地工作区：每次请求实时读取，重启后仍然保留。
- 点击网页的“清空会话”：只清除短期对话，不会动八个本地文件。
- 这不是重新训练 DeepSeek，而是外部人格和记忆增强。等积累足够多高质量对话、评分和纠正记录后，再考虑导出微调数据。
- 每次回复会优先解析为 `reply`、`emotion`、`context` 三个内部字段。网页只显示 `reply`，后端把 `emotion` 转为豆包文档中的 `[#语音指令]` 前缀，因此情绪标签不会被直接念出来。
- `DOUBAO_TTS_EMOTION_MODE=prefix` 开启情绪指令；`DOUBAO_TTS_REFERENCE_MODE=prefix` 才会把引用上文指令传给 TTS，默认关闭以便先验证当前接口支持情况。

## 接口

- `GET /api/health`：查看模型、TTS、语音识别、电脑扬声器和本地工作区状态
- `POST /api/chat/text`：输入 `{ "text": "你好" }`，返回文字回复和音频
- `POST /api/chat/audio`：直接上传 `audio/wav` 原始音频，返回识别文字和小岚回复
- `POST /api/reset`：清空当前短期会话
- `GET /api/workspace`：查看八个工作区文件是否存在

`POST /api/chat/audio` 用请求头控制行为，方便不同客户端复用同一接口：

| 请求头 | 作用 |
|---|---|
| `X-Play-Audio: true` | 合成语音后用**电脑扬声器**播放，响应里不带音频数据（电脑扬声器测试用） |
| `X-Response-Audio: true` | 把音频以 base64 放进响应，由客户端自己播放（网页用） |
| `X-Allow-Partial: true` | 大模型失败时仍返回 200，带上识别文字和 `error`，方便单独验证麦克风 |

响应字段是 `transcript`、`reply`、`audio`、`audio_media_type`、`error`、`played`。

## 验证

```powershell
python -m compileall -q app tests
python -m unittest discover -s tests -v
```

测试使用模拟 Provider，不会调用真实 API，也不会产生上游费用。

## 后续演进

推荐顺序是：本地文件规则 -> 对话评价和纠正记录 -> 用户确认后的记忆提取 -> 可选的向量检索 -> 微调数据导出或模型微调。长期记忆应始终可查看、可修改、可删除，不能变成无法解释的黑箱。
