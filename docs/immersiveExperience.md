# Parlor 沉浸感改进：虚拟形象 + 练习话题

## Context

Parlor 目前是一个纯本地的实时语音助手（FastAPI + llama.cpp/Gemma-4 + Kokoro TTS + smart-turn-v3 + 原生 JS 前端，单个 `/ws` WebSocket）。用它练口语时有两个体验缺口：

1. **视觉上没有对话对象。** 画面里只有用户自己的摄像头自拍（[index.html:23-28](src/parlor/web/index.html#L23-L28)），说话的一方是不可见的。
2. **不知道聊什么。** 代码里完全没有"话题/场景"的概念，系统提示只有一句泛化的 friendly assistant（[server.py:52-58](src/parlor/server.py#L52-L58)）；而且**服务端从不主动开口** —— 连上以后是一片沉默，等着用户先说话。

目标：给 Parlor 一张会说话的脸，和一套能让人直接开练的场景卡片 + 英语教练行为。约束是不能牺牲它的两个王牌 —— ~1s 首音延迟和 barge-in 打断。

**已确认的方案选择**（来自提问）：浏览器内 3D VRM 角色（口型由现有 WebAudio analyser 驱动，**服务端零额外延迟**），同时留好可插拔接口，将来把局域网上那台 4090 的真人数字人（MuseTalk / Ditto）接进来；话题四项能力全做：卡片选择器 + AI 主动说第一句 + 本地模型动态生成 + 英语教练分级纠错。

---

## Phase 0 — 打地基（无行为变化，测试必须全绿）

### 0.1 `build_system()` — 把系统提示拼装抽出来

[server.py:336-340](src/parlor/server.py#L336-L340) 现在是内联拼接。抽成模块级函数：

```python
def build_system(clock: str, delegation: bool,
                 topic: topics.Topic | None, level: coach.Level | None) -> str:
    """The per-connection system message. Stable clauses first, the ones a
    topic or level change rewrites LAST: llama-server's prefix cache is a
    longest-common-prefix match, so keeping the variable text at the tail
    costs nothing and saves the whole prefix in the case that matters —
    picking a topic before the first turn, when history is just this."""
```
拼接顺序：`SYSTEM_PROMPT` → `SESSION_CLOCK` → `CAPABILITY_NOTE` → `RESEARCH_NOTE?` → `SCENARIO_NOTE?` → `COACH_NOTE?`。现有每段常量都以空格开头，新常量保持这个约定，`"".join` 才对。

### 0.2 独立的 TTS analyser（口型正确性的关键）

现在 `ensureAudioCtx` 只建一个 analyser（[app.js:691-698](src/parlor/web/static/app.js#L691-L698)），`setState` 在 `listening` 时把麦克风接上去、离开时断开（[app.js:138-145](src/parlor/web/static/app.js#L138-L145)）。两路信号不会同时求和，**但仍然不能用来驱动嘴**：

- listening 期间它承载的是**用户自己的声音** —— 角色会把用户的话用嘴型学回去，这是最诡异的失败模式；
- barge-in 时 `stopPlayback()` → `goListening(false)` 会在一帧内把接线从 TTS 翻转到麦克风；
- 它是给 40 根装饰条调的：`fftSize 256`（~187Hz/bin，分不开 F1/F2）、`smoothingTimeConstant 0.75`（~200ms 滞后，肉眼可见的不同步）。

**做法**：在 `ensureAudioCtx` 里加第二个 `ttsAnalyser`（`fftSize 1024`、`smoothingTimeConstant 0.2`、`minDecibels -70` / `maxDecibels -20`），并在 `queueAudioChunk` 的 `source.connect(analyser)` 后面加一行 `source.connect(ttsAnalyser)`（[app.js:726](src/parlor/web/static/app.js#L726)）。
**不要动 `setState` 里的麦克风接线** —— 那段是波形动画和 barge-in 状态的承重墙。

`getByteFrequencyData` 是非破坏性拷贝，`drawWaveform` 和 `updateSpeakingGlow` 今天就在同帧读同一个节点，加一个节点不冲突。

### 0.3 Avatar 后端接缝（先接口，后实现）

在 `app.js` 里 `ensureAudioCtx` 之后加一段，**注释写清楚这是"插头，不是参与者"**（和 modes.py 一个精神：变化的部分是数据，藏在一个接口后面）：

```js
let avatarBackend = null;
window.setAvatarBackend = b => { … };
function safeAvatar(fn) { try { fn(); } catch (e) { console.warn('avatar:', e); } }
const avatar = { state: s => …, chunk: info => …, stop: () => … };
```

五个方法的契约：

| 方法 | 调用时机 | 参数 |
|---|---|---|
| `init({canvas, audioCtx, ttsAnalyser})` | AudioContext 创建后（可能晚于注册） | — |
| `setState(state)` | 每次 `setState` 末尾 | `loading\|listening\|processing\|speaking` |
| `onAudioChunk({index, startAt, duration, sampleRate, samples})` | `queueAudioChunk` 里 **`source.start()` 之后**（`startAt` 才是终值） | `startAt` 是 **AudioContext 时钟**，不是墙钟 |
| `stop()` | `stopPlayback()`（打断）+ 最后一个 source 结束 | — |
| `dispose()` | 切换后端 | — |

本地 VRM 后端**完全忽略 `onAudioChunk`**（它每帧读 analyser，天然同步）；远端后端完全忽略 analyser（只吃 `onAudioChunk`）。这个不对称正是接缝要同时有这两者的原因。后端抛异常绝不能带崩会话 —— 全部包 `safeAvatar`。

---

## Phase 1 — 话题内核（服务端）

### 新文件 `src/parlor/topics.py`

```python
@dataclass(frozen=True)
class Topic:
    id: str          # 稳定 slug，set_topic 走这个
    category: str
    title: str       # 卡片标题，2-4 词
    scene: str       # 一句话情境，插进 SCENARIO_NOTE
    role: str        # Parlor 演谁（"the barista taking their order"）
    user_role: str   # 用户演谁
    opening: str     # 手写的第一句 —— 只作为开场 turn 的 fallback，绝不下发给客户端
    generated: bool = False
```

- `CATEGORIES`：business / dining / sports / travel / daily / health / shopping / tech。
- `SEED`：**每类 3 张，共 24 张**手写卡片。例：
  `Topic("cafe_order", "dining", "Ordering coffee", "The user is at the counter of a busy neighbourhood café at morning rush.", "the barista taking their order", "a customer ordering coffee and something to eat", "Morning! What can I get started for you?")`
- `FREEFORM` 哨兵（"Just chat"）：`scene=""`，**不贡献任何 SCENARIO_NOTE** —— 空场景必须加 0 个 token，而不是一句空子句让模型去揣摩。
- 函数：`catalogue()` / `get(id)` / `as_card(t)`（**不含 `opening`**，下发会剧透第一句）。

### `server.py` 新提示常量（放在 `CAPABILITY_NOTE` 旁边，按仓库风格每条带 WHY 注释）

```python
SCENARIO_NOTE = (
    " Right now you are role-playing a scene with the user so they can "
    "practise speaking English. {scene} You play {role}; they play {user_role}. "
    "Stay in the scene: speak only your own lines, no narration, no stage "
    "directions, no asterisks, no describing what you are doing. You are "
    "still yourself underneath — if they ask for a timer, for translation, "
    "or for something looked up, step out of the scene, handle it in one "
    "sentence, and step straight back in."
)

TOPIC_OPENING_PROMPT = (
    "System note (not user audio): the scene starts now. Say the very first "
    "line as {role}, in character — one or two short sentences that open the "
    "situation naturally and end with a question they can answer out loud. "
    "Don't greet them as an AI, don't explain the exercise, don't set the "
    "scene: they can already see it."
)

FREEFORM_OPENING_PROMPT = (
    "System note (not user audio): the user just opened a session to "
    "practise speaking English, with no particular scene. Greet them in one "
    "short sentence and ask one easy opening question about their day."
)
```

关键 WHY（写进注释）：禁旁白那句是刚需 —— 否则模型会输出 `*wipes the counter*`，TTS 会把星号念出来；最后那句"仍然是你自己"是因为场景会和 `CAPABILITY_NOTE` 打架 —— 演咖啡师的模型会在角色里拒绝设闹钟。开场提示以 `System note (not user audio)` 开头，理由和 `DELIVER_PROMPT`（[server.py:156](src/parlor/server.py#L156)）完全一样：纯文本 turn 会被模型误读成自己回声，触发反回声规则。

### WS 协议新增

**`ws.accept()` 之后立刻下发** `session_ready`：`{categories, topics: [as_card…], levels, topic: null, level}`。已验证安全：`tests/util.py` 忽略未知帧，`benchmarks/bench.py:97` 的 switch 没有 else 分支。

**主循环新分支**，插在 `set_mode` 分支之后（[server.py:633](src/parlor/server.py#L633)）—— 放在循环里（而不是 HTTP 端点）正是因为这个单线程循环本身就保证了不会在 turn 流式输出时插一个请求到 llama-server：

```python
if msg.get("type") == "set_topic":
    picked = topics.get(str(msg.get("id", "")))
    if picked is None: … continue           # 未知 id 记日志忽略
    topic = picked
    # 换场景 = 换对话：把咖啡馆的对白带进餐厅，模型会引用一个不存在的
    # 咖啡师；而且重写 history[0] 本来就会让后面每条消息的 prefix cache
    # 失效，重置反而是免费的。
    history = [{"role": "system", "content": build_system(clock, delegation, topic, level)}]
    prompt_tokens["last"] = 0
    frame_image = None; speech_chunks = []; held_audio = []
    interrupted.clear(); playing_since["t"] = 0.0
    await send_json(ws, {"type": "topic_changed", "topic": topics.as_card(topic), …})
    if msg.get("speak_opening", True):
        await proactive_turn(prompt, topic.opening)   # 复用 server.py:494
    last_activity["t"] = time.time(); drain_ready(); continue
```

**开场白直接复用现成的 `proactive_turn()`**（[server.py:494-514](src/parlor/server.py#L494-L514)，timer 响铃和 research 投递已经在用），不需要新的 turn 形态。

**时序保证**（已推演）：
1. `set_topic` 和 turn 在同一个单线程循环里处理，重建 `history[0]` 和 `proactive_turn` 在同一个 `continue` 块里、中间没有会让出的 await —— 结构上不可能有 turn 抢在前面。
2. 会话中途选话题：receiver（[server.py:347-359](src/parlor/server.py#L347-L359)）只特判 `interrupt`，其余一律入队，所以 `set_topic` 会**自然排队**等当前 turn 结束，不需要任何取消机制（和 `set_mode` 同一套纪律）。要立刻生效就让客户端先发 `interrupt`。
3. prefix cache：`set_topic` 把 history 重置成 `[system]`，重写点后面什么都没有，失效成本为零。

---

## Phase 2 — 话题客户端

- `index.html`：`</body>` 前加 `#topicOverlay` 遮罩（level 行 + 分类 chip 行 + 卡片网格 + "More scenes" / "Just chat"）；`.controls`（[index.html:39-53](src/parlor/web/index.html#L39-L53)）里加 `#topicChip`，**用和 `#modeChip` 完全相同的标签结构**，CSS 就能共用 `.mode-chip` / `.mode-stop`。
- `app.js` 新增一节，照抄 `setSessionMode`（[app.js:317-322](src/parlor/web/static/app.js#L317-L322)）的服务端驱动模式。
- **选题前不进入监听**：保持 `state === 'loading'`。`ws.onopen` 已经尊重它（[app.js:185](src/parlor/web/static/app.js#L185)），`handleSpeechEnd` 在非 listening 时直接返回 —— 把 `init()` 末尾的 `setState('listening')`（[app.js:813](src/parlor/web/static/app.js#L813)）改成"已选题才触发"，复用现有状态机而不是加新门。
- `topic_changed` 到达时执行 `currentAssistantEl = null; goProcessing();` —— 和 `delegation_resolved`（[app.js:267-273](src/parlor/web/static/app.js#L267-L273)）交接地板给主动 turn 的三行完全一样，后续 `turn_final → msg.proactive` 分支无需改动。
- **重连不能重新打招呼**：`ws.onclose` 每 2s 重连（[app.js:198](src/parlor/web/static/app.js#L198)）。收到 `session_ready` 时如果本页已选过题，重发 `set_topic {speak_opening: false}` 静默恢复；`onclose` 里**不要**清 `activeTopic`（和第 195 行 `setSessionMode('conversation')` 的处理不同，加注释说明，否则读起来像 bug）。
- `localStorage` 记住上次选择用于**预选卡片**，但不跳过选择器。

---

## Phase 3 — 英语教练层

### 新文件 `src/parlor/coach.py`

`Level` frozen dataclass：`name` / `label` / `register`（一句话：词汇、语速、往哪儿推）/ `sentences`（长度预算）。三档 beginner / intermediate / advanced。`DEFAULT = os.environ.get("COACH_LEVEL", "intermediate")`（`off` 关闭）。

**为什么不做成 Mode**（写进模块 docstring）：Mode 决定一个 turn **是什么**（smart-turn 是否把关、要不要摄像头、research 能否落地、哪个提示替换 respond 指令），且同时只有一个生效。教练只改**措辞**，而且必须能和场景、和 mode **相乘** —— 咖啡馆场景里的 beginner 又要设个闹钟，那是一个会话不是三个。做成 Mode 就得有 `coach` / `coach_translate` / `coach_listen` 三个条目外加 `actions._MODE_CLAUSES` 里的配套子句，一张组合爆炸表，换来的只是系统提示里的一句话。

### `COACH_NOTE`

```python
COACH_NOTE = (
    " The user is practising spoken English at {level} level. {register} "
    "Keep each turn to {sentences}. When they say something a listener would "
    "genuinely misunderstand, don't announce the mistake — say it back the "
    "natural way inside your own reply and carry on. Let small slips go. End "
    "every turn with exactly one question, never two. These are conversation "
    "instructions: if a turn's own instruction asks you to translate or to "
    "only transcribe, that instruction wins."
)
```

WHY：纠错必须是 **recast（自然复述）而不是列清单** —— 清单没法念出来，而把错误原样读回去再改会让学习者变得防御、更不敢说，正好和目的相反。"exactly one question" 是让会话像聊天而不是面试的那句：没有它模型每轮堆两三个问题，学习者只答最后一个。长度预算刻意留在 `RESPOND_PROMPT` 的 "1-4 short sentences" 之内，两条指令不打架。最后一句是护栏，防止教练措辞渗进 translate / listen 模式。

### `set_level` WS 分支

原地重写 `history[0]`、**保留对话**（换难度不是换场景）。代价是下一轮一次完整 re-prefill —— 一次刻意的点击换来上下文不丢，值得，且写进注释而不是绕开。

---

## Phase 4 — 本地模型动态生成话题

`topics.py` 增加 `TOPIC_SCHEMA`（`{topics: [{title, scene, role, user_role, opening}]}`，1-4 条）+ `GENERATE_PROMPT` + `generate(category, n, avoid)` + `remember(new)`。

- 走 `llama.chat_blocking(msgs, max_tokens=512, temperature=0.9, json_schema=TOPIC_SCHEMA)` —— 和 action head（[actions.py:134](src/parlor/actions.py#L134)）同一套 grammar-forced JSON，形状保证可解析，唯一的失败模式是场景不好、绝不是崩溃。**温度是全项目唯一调高的一处**：按两次按钮就该得到两个不同场景。`avoid` 带上屏幕上已有的标题，否则模型会确定性地重复生成 "Ordering coffee"。
- 缓存：`~/.cache/parlor/topics.json`（尊重 `XDG_CACHE_HOME`，`PARLOR_TOPICS_CACHE` 可覆盖）。`{"version": 1, "topics": [...]}`，`tmp` + `os.replace` 原子写，模块级 `threading.Lock`（生成跑在 executor 线程），上限 60 条丢最旧。**文件损坏只记日志不致命** —— 选择器必须能打开。这是项目里的第一处持久化，要在 `docs/configuration.md` 单开一节说明。
- `generate_topics` WS 分支跑在 executor 里（`chat_blocking` 会阻塞事件循环）。
- **单 slot 争用（`-np 1`，[llama.py:146](src/parlor/llama.py#L146)）**：生成不会和 turn 重叠（同一个循环），但**会挤掉会话的 prefix cache**，下一轮要为 history + 所有存下来的音频付完整 prefill。缓解：客户端只在选择器打开时提供"More scenes"（会话开始时 history 就是 `[system]`，无损失）；服务端在 `len(history) > 1` 时拒绝生成，除非带 `force: true`。

---

## Phase 5 — Avatar 舞台 + 2D 兜底（先上零依赖版本）

**先做 Canvas 2D 脸、后做 VRM** 是刻意的：它同时是产品兜底和 D.2（GPU 争用）的缓解手段，而且能让布局、接缝、口型算法在引入 1.2MB CDN 依赖之前就全部验证完。

- **布局**：`#viewportWrap` 保持为**外层**元素不动 —— `setState` 的 `viewportWrap.className`、三层 glow、`updateSpeakingGlow` 全部继续工作，只换里面的内容：`<canvas id="avatar">` 成为主视觉，用户自拍降级成右下角 `.pip`（约 23% 宽）。`style.css` 里 `video` 规则改成 `.pip video`（保留 `scaleX(-1)` 镜像和 `object-fit: cover`）。`cameraToggle` 改成切 `.pip` 的透明度。
- **新文件 `src/parlor/web/static/avatar.js`**，先只实现 `CanvasFaceBackend`（~90 行：两个眼睛椭圆，眨眼 = 纵向缩放；嘴椭圆高度 = `open`、宽度 = `1 - 0.35*ratio`；颜色从 `getComputedStyle` 读 `var(--glow)`，自动跟随状态配色）。
- **口型算法**（两个后端共用，从 `audioCtx.sampleRate` 在 init 时算 bin 下标，**绝不硬编码** —— Firefox/Safari 是 44.1kHz）：
  ```
  bands: lo 250-900Hz (F1 → 张口度) | hi 900-2600Hz (F2 → 舌位前后) | sib 4000-8000Hz (擦音)
  level = clamp(rms / max(peak, 0.08), 0, 1)      peak 用 *0.995 自适应衰减，轻声也能动
  ratio = hi / (lo + hi)                          0 = 圆唇后元音，1 = 前元音
  open  = level * (1 - 0.75 * sibilance)          /s/ 几乎不张嘴
  ```
  非对称平滑 `ATTACK 0.45 / RELEASE 0.18`（嘴张开比闭合快，对称平滑会糊）；`level < 0.04` 持续 >120ms 触发静音门把嘴归零（否则句间会挂着半张的嘴）。
- **帧预算（强制）**：锁 30fps、`document.visibilityState !== 'visible'` 时完全跳过渲染、Uint8Array 在 init 里预分配（别抄现有代码的每帧 new）、avatar 的 RAF 循环和 `drawWaveform` 分开。

## Phase 6 — VRM 3D 角色

- `index.html` 加 importmap（three 自 r160 起只发 ESM，importmap 是无构建路径），指向 jsDelivr 的 `three` + `three/addons/` + `@pixiv/three-vrm`；`avatar.js` 改成 `<script type="module">`。module script 是 deferred 的，保证在 `app.js` 之后跑，`window.setAvatarBackend` 一定存在。版本号在 `server.py` 里定常量，通过现有的 `.replace("{{model}}", …)` 机制（[server.py:316-319](src/parlor/server.py#L316-L319)）注入。
- 场景：`WebGLRenderer({alpha: true})` 让 CSS glow 从边缘透出来；`PerspectiveCamera(28)` 半身肖像取景；`setPixelRatio(min(dpr, 1.5))`；`ResizeObserver` 驱动（照 `initWaveformCanvas` 的 DPR 模式）。
- 加载：`VRMLoaderPlugin` + **无条件 `VRMUtils.rotateVRM0(vrm)`**（对 VRM 1.0 是空操作，对大多数人下载到的 VRM 0.x 是必需的 —— 它们朝 +Z）+ `removeUnnecessaryVertices` / `combineSkeletons` 省帧时间。
- 表情：元音轮 `[['ou',0.10],['oh',0.33],['aa',0.50],['ih',0.68],['ee',0.90]]`，在 `ratio` 轴上在相邻两个锚点之间线性混合，嘴型永不跳变。`expressionManager.setValue()` 之后**必须最后调 `vrm.update(delta)`** —— three-vrm 在 update 里应用表情值，顺序反了是静默失效。
- 眨眼：下次在 `now + 2.4 + rand*3.8` 秒，持续 120ms，权重 `sin(π·p)`，15% 概率 180ms 后补一次；说话时不抑制（人说话时也眨眼）。
- 待机头部微动：两个不成比例的正弦叠加在 normalized neck bone 上，循环感不可察觉。状态偏置：listening 看镜头 + `relaxed 0.10`；processing 抬头偏视（思考）+ 幅度 0.7；speaking 看镜头 + 幅度 1.35 + `happy 0.15`。偏置用 0.06 lerp 过渡，状态切换不生硬。
- **.vrm 资产**：路径 `src/parlor/web/static/avatar/model.vrm`，加进 `.gitignore` 的 "Models (too large for git)" 段（一个 VRM 10-60MB）。`AVATAR_VRM_URL` 可指向 VRoid Hub 或局域网文件服务。**推荐用 VRoid Studio 免费导出 VRM 1.0**；README 写明"不自动下载任何模型，形象由你自己选"。加载失败（404 / 解析错）自动回落到 `CanvasFaceBackend` —— 这是一等路径不是补丁。`AVATAR_BACKEND=none|canvas|vrm` 控制。
- 整个 module body 包 try/catch：jsDelivr 挂了应该退化成今天的 UI（glow 还在，canvas 空着），而不是白屏。

## Phase 7 — 4090 远端真人数字人（本次只定接口）

4090 在局域网另一台机器上。**推荐拓扑 B：浏览器 ↔ 4090 直连**，Mac 只把 PCM + session token 转过去，浏览器再开第二个 WebSocket 到 `ws://4090.lan:9000/avatar?session=…` 收帧。理由是带宽：25fps × 384² JPEG ≈ 0.8-1.0 MB/s，经 Mac 中继要走两遍。页面是 `http://localhost`，明文 `ws://` 到局域网 IP 不触发 mixed content。Parlor 的 `/ws` 保持纯语音协议，客户端只是多一个实现同样五个方法的 `RemoteAvatarBackend`。

**不可动摇的不变量**：avatar 永远不能挡音频。`pipeline.py:tts_worker`（[pipeline.py:311-334](src/parlor/pipeline.py#L311-L334)）里的 PCM 交接必须在 `audio_chunk` 发送**之后**，`asyncio.create_task` 发射即忘、无人 await、打断时取消。

**音画同步**：`queueAudioChunk` 已经算出了权威答案却扔掉了 —— `const startAt = Math.max(streamNextTime, audioCtx.currentTime); source.start(startAt);`。`onAudioChunk({index, startAt, duration})` 把这个映射交给后端，后端在自己的 RAF 里按 `k = floor((audioCtx.currentTime - chunkStart[index]) * fps)` 取帧。由此推出的规则（写进模块注释）：**音频时钟是权威**，迟到的帧**丢弃**而不是排队（画面落后于声音比静止的脸更糟）；早到的帧等待，最后一帧保持（冻结不清空）；超过 `startAt + duration + 0.2` 判定过期丢弃。本地 VRM 后端是正确性参照 —— 远端方案如果打不过"读 analyser、因而结构上必然同步"，就不值得上。

候选模型：**MuseTalk 1.5**（实时唇部重绘，4090 上 256px 可跑 30fps+，需要一段循环参考视频）、Ditto-TalkingHead。SadTalker / Hallo / EMO 太慢，不适合实时。

---

## 验证

### 新测试（沿用现有 e2e WS 驱动模式）

先把 `assert_spoken_prose` 从 [tests/test_conversation.py:25-30](tests/test_conversation.py#L25-L30) 提到 `tests/util.py`（三个新文件都要用，复制三份必然漂移）。`tests/conftest.py` 的 env dict 加 `PARLOR_TOPICS_CACHE` 指向 tmp，别写进开发者真实缓存。`benchmarks/fixtures.py` 加三条：`topic_reply_coffee` / `learner_error`（"Yesterday I go to the market and buy two fish."）/ `topic_smalltalk`。

- **`tests/test_topics.py`**：`session_ready` 带全部 8 个分类且卡片**不含 `opening``；`set_topic` 后开场 turn 有音频、是 prose、以 `?` 结尾；`speak_opening: false` 时 6s 内无 `audio_start`（重连路径 —— 回归了就是每次网络抖动都重新打招呼）；话题带进后续对话；换话题重置场景（**断言服务端日志而不是模型记忆** —— 对 LLM 断言"忘了没"是掷硬币）；freeform 也开口；未知 id 被忽略且会话仍可用；`generate_topics` 返回可玩卡片；生成结果对**第二个 Session** 可见（缓存生效）。
- **`tests/test_coach.py`**：`set_level` 有回执；每轮 `text.count("?") <= 1`；recast 测试（回复含 "went"/"bought" **且不含** "mistake"/"incorrect"/"wrong"/"should say" —— 否定那半是稳的）；**教练措辞不渗进 translate 模式**（这条测试正是 `COACH_NOTE` 最后一句存在的理由；若失败，退路是在 `switch_mode` 时重建系统提示，进模式丢掉场景/教练段、退出时恢复）。
- **`tests/test_web_assets.py`**（新，顺带补上今天完全没有覆盖的 `{{model}}` 替换）：`GET /` 里 `"{{" not in html` 且含 `id="avatar"` / `id="topicOverlay"`；`GET /static/avatar.js` 返回 200。

Avatar 本身在 WS 测试里结构上够不着。仓库已有 Playwright，将来可加一个加载 `/`、stub `AudioContext`、断言 `window.setAvatarBackend` 被调用的冒烟测试 —— 但现在不值得为它引入 JS 工具链。

### 手工端到端

```bash
# 动手前先给王牌指标打基线
uv run parlor                                                   # terminal 1
uv run python benchmarks/bench.py --label before --out benchmarks/results/before.json
```

1. **话题**：刷新页面 → 选择器弹出、状态是 Loading、**此时说话不产生 user 气泡**。选 "Ordering coffee" → ~2s 内 Parlor 用咖啡师身份说一句以问号结尾的话，状态 Speaking → Listening，chip 显示 "Ordering coffee · change"。
2. **教练**：切 beginner，说 "Yesterday I go to the market and buy two fish." → 回复应把 "went to the market" 自然揉进自己的句子、不点名错误、1-2 句、**恰好一个问号**。切 advanced 重复：更长更快更地道，仍然一个问题。
3. **生成**：change → 选分类 → More scenes，~5s 出 3 张新卡，整页刷新后仍在（落在 `~/.cache/parlor/topics.json`）。
4. **重连**：场景激活时 kill 服务端并重启 → 浏览器 2s 后重连，chip 仍在，**不重新打招呼**。再整页刷新 → 选择器回来。
5. **模式组合**：场景中说 "translate everything I say" → chip 切换、输出是纯翻译、**没有附加的教练提问**；说 "stop translating" 回到咖啡馆。
6. **口型**：Parlor 说话时嘴跟音节动、句间闭合、/s/ 时接近闭合。然后**在它说话时插话** —— 你说话期间嘴必须**静止**（动了就是 0.2 的 `ttsAnalyser` 接线错了）。
7. **barge-in 无回归**：中途打断 → 播放立停、嘴闭上、状态回 Listening。和 `git stash` 过的版本对比手感 —— 这是最要命且无法自动化的回归。
8. **渲染负载下的 VAD 健康度**：DevTools Performance 录 20s 空闲监听。long task 应 <16ms；avatar 帧饿死主线程的话会看到 VAD 掉帧、转向检测变迟钝。
9. **复测**：`bench.py --label after` + `compare.py`，`ttfa_s` 必须持平。若变差，嫌疑人是服务端（话题/教练段拉长了系统提示 → 每会话一次性 prefill），**不是 avatar** —— 重点看第一轮。

---

## 风险与取舍（动手前先知道）

1. **avatar 的开销在浏览器主线程，不在服务端。**"零服务端延迟"是真的，但不是全部。`@ricky0123/vad-web` 在同一个主线程上跑 Silero WASM，60fps 的 three.js 会抢它的帧。症状不是"角色卡"，而是**转向检测变迟钝、barge-in 变不可靠** —— 恰好是 Parlor 真正擅长的两件事。30fps 上限、`min(dpr, 1.5)`、隐藏时停渲染、canvas 保持舞台尺寸（~640×360 而非 DPR 放大到 4K）**全部是强制项**，上线前用第 8 步验证。
2. **Mac 统一内存上的 GPU 争用。** llama.cpp（Metal）+ Kokoro MLX 已经在吃满 GPU 和内存带宽，持续渲染的 VRM 会再加一个浏览器 GPU 进程抢同一条带宽，预计解码慢 5-15%，直接落在首音延迟上 —— 这是王牌指标最可能被打回的路径。`AVATAR_BACKEND=canvas`（2D，近零 GPU）既是产品兜底也是这条风险的缓解，也是它必须**先于** three.js 落地的原因。
3. **两个新 CDN 依赖，压在一个主打 "fully on-device" 的产品上。** three + three-vrm ≈ 1.2MB。认真考虑把两个 min 构建 vendor 进 `static/vendor/` —— 代价是两个提交进仓的压缩文件，换来名副其实的离线运行。**这要主动决定，别默认发生**（现有的 ort / vad CDN script 本来就是同一个故事上的洞）。
4. **生成话题会挤掉 prompt cache**（`-np 1` 单 slot）。已用"只在选择器打开时提供"限制到无损场景。若将来想在热会话中生成，唯一的真答案是 `-np 2`，代价是每个 slot 上下文减半。
5. **重写 `history[0]` 是缓存失效的手雷。** `set_level` 是刻意拉环（换来保留对话），`set_topic` 靠重置 history 规避。**绝不要"中途不重置地加个话题"** —— 那是几秒钟的首轮。
6. **角色扮演 vs 能力层。** 演咖啡师的模型会在角色里拒绝设闹钟。`SCENARIO_NOTE` 最后一句是解药，但那是提示层的修复，是概率不是保证。上线第一周留意，考虑加一条 e2e（场景中设闹钟 → `timer_started` 仍触发）。
7. **"恰好一个问题" 和 flush 路径打架。** `FLUSH_PROMPT`（[server.py:222-229](src/parlor/server.py#L222-L229)）要求"一句温暖的鼓励继续" —— 常常就是个问题。目前两条指令碰巧一致（都要一个），但将来改 flush 措辞时要重新检查。
8. **项目第一处持久化。** `~/.cache/parlor/topics.json` 有版本号、原子写、容量上限、"损坏即忽略"规则，但仍是新一类 bug（权限、磁盘满、升级后 schema 过期）。在 `docs/configuration.md` 新开 **Practice topics** 一节，连同 `PARLOR_TOPICS_CACHE` / `COACH_LEVEL` / `AVATAR_BACKEND` / `AVATAR_VRM_URL` 一起写进 `.env.example`。
9. **选择器在第一句话之前加了一次点击。** Parlor 现在是加载完立刻监听，这是个实打实的好性质，正在被交易掉。"Just chat" 保持一键可达，`localStorage` 预选上次选择，并考虑加 `TOPIC_DEFAULT` 让想要旧行为的人完全跳过遮罩。

---

## 阶段表

| # | 阶段 | 主要文件 | 交付 |
|---|---|---|---|
| **0** | 接缝，无行为变化 | `server.py`(`build_system`)、`app.js`(`ttsAnalyser` + avatar 接缝) | 重构；全测试绿；`bench.py` 持平 |
| **1** | 话题内核（服务端） | 新 `topics.py`；`server.py` | 场景可选、开场白会说；`test_topics.py` |
| **2** | 话题客户端 | `index.html`、`style.css`、`app.js` | 选择器遮罩、话题 chip、重连恢复 |
| **3** | 教练层 | 新 `coach.py`；`server.py`；`app.js` | 分级 + 温和纠错 + 每轮一问；`test_coach.py` |
| **4** | 动态生成 | `topics.py`、`server.py`、`app.js` | 按需生成场景 + 本地缓存 |
| **5** | Avatar 舞台 + 2D 兜底 | `index.html`、`style.css`、`app.js`、新 `avatar.js` | 角色成主视觉、自拍变 PiP、**零新依赖**的可用口型；`test_web_assets.py` |
| **6** | VRM 后端 | `index.html`(importmap)、`avatar.js`、`server.py`、`.gitignore`、README | 3D 角色、元音口型、眨眼、待机微动 |
| **7** | 远端后端（以后） | 新 `avatar.py` 或 `RemoteAvatarBackend` | 接口与协议落定；4090 上的实现不在本次范围 |

Phase A（avatar）和 B（话题）除 Phase 0 外互不相干，可并行或换序。**建议话题先行** —— 它才是真正改善英语练习的那个。
