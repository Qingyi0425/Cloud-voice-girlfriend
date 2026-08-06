# 云端语音陪伴

>Diego制作

这是一个本地网页试听台：输入文字后，由 OpenAI 兼容中转站调用大语言模型生成回复，再交给豆包 TTS 2.0 合成女声并在浏览器播放。

当前版本暂时不使用 STT、录音、数据库或模型微调。人格和长期记忆采用本地 Markdown 工作区文件，风格接近 OpenClaw 的可读配置文件。

## 启动

在项目根目录运行：

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
LLM_BASE_URL=你的中转站地址
LLM_API_KEY=你的中转站密钥
LLM_MODEL=你的模型名称

DOUBAO_TTS_PROVIDER=http
DOUBAO_TTS_BASE_URL=你的豆包TTS接口地址
DOUBAO_TTS_API_KEY=你的豆包TTS密钥
DOUBAO_TTS_RESOURCE_ID=seed-tts-2.0
DOUBAO_TTS_VOICE_ID=你的音色ID
DOUBAO_TTS_FORMAT=mp3
DOUBAO_TTS_SAMPLE_RATE=24000
```

改完 `.env` 后要重启服务，配置才会重新读取。浏览器和未来的 PCB 都不会直接接触这些密钥。

## 本地人格与语音文件

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

- `GET /api/health`：查看模型、TTS 和本地工作区状态
- `POST /api/chat/text`：输入 `{ "text": "你好" }`，返回文字回复和音频
- `POST /api/reset`：清空当前短期会话
- `GET /api/workspace`：查看八个工作区文件是否存在

## 验证

```powershell
python -m compileall -q app tests
python -m unittest discover -s tests -v
```

测试使用模拟 Provider，不会调用真实 API，也不会产生上游费用。

## 后续演进

推荐顺序是：本地文件规则 -> 对话评价和纠正记录 -> 用户确认后的记忆提取 -> 可选的向量检索 -> 微调数据导出或模型微调。长期记忆应始终可查看、可修改、可删除，不能变成无法解释的黑箱。
