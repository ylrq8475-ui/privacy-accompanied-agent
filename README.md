# Spark Active Companion Demo

这是一个面向竞赛演示的主动陪伴 Demo，不是通用产品。系统通过确定性策略把模型分析、用户澄清、动作授权和执行严格分离。

## 当前能力

| 能力 | 状态 |
| --- | --- |
| Step3-VL | 推荐让后端与模型同在 DGX Spark 私网；本地浏览器通过 SSH 回环访问控制台；输出必须通过严格 JSON Schema |
| StepAudio | 健康接口可达；ASR 缺少明确的共享合成音频时使用文字降级；TTS 仅由用户点击触发，有效 WAV 临时播放，失败时明确降级为 `TEXT_ONLY` |
| 感知 | 摄像头不可用时使用明确标记的 `STATIC_SYNTHETIC` 降级输入 |
| 天气 | 通过唯一 INTERNET egress 调用 Open-Meteo，失败时按缓存、固定 Demo 数据降级 |
| 音乐 | 用户独立授权后优先由 `external-connector` 获取固定 Audius 预览到内存，再由后端所在 DGX 主机执行 LOCAL 播放；不可用时降级到仓库内钢琴素材 |
| AC | 始终是 Mock，不控制物理空调，结果标记 `physical_action_performed=false` |
| 记忆 | SQLite 只保存显式确认的 Demo 偏好、最多 50 条脱敏情绪摘要、精简动作和脱敏审计 |
| 恢复 | 重启后协调 Action 状态，但不会自动执行待处理动作 |

Step3 只提供状态假设、推荐和澄清候选，不能授权或执行动作。音乐与 AC 使用不同的 `action_id`，必须分别授权。

## 环境要求

- Windows PowerShell
- Python 3.11
- Node.js 与 npm
- 已在本机配置、可免交互使用的 DGX Spark SSH 主机别名

不要把真实 SSH 主机、IP、密码、私钥或令牌写入仓库。

## 推荐部署：后端与模型运行在 DGX Spark

本地只保留浏览器和 SSH 回环隧道。DGX 上的 Demo backend、Track
Catalog 和独立 `external-connector` 使用新的 Compose 项目运行；现有
Step3、StepAudio 容器保持不变。Demo 容器不发布任何 DGX 主机端口；
本地 SSH 隧道动态直连 Demo 私网中的 backend，仅监听本机回环地址。

```powershell
$SparkSshAlias = "your-existing-dgx-ssh-alias"
.\scripts\deploy_dgx_spark.ps1 -SshAlias $SparkSshAlias
$Tunnel = .\scripts\start_dgx_console_tunnel.ps1 -SshAlias $SparkSshAlias
```

浏览器打开 `http://127.0.0.1:8000/console/`。完整拓扑、验收、Audius
占位配置和回滚步骤见 [DGX Spark 部署说明](docs/DEPLOYMENT_DGX_SPARK.md)。

## 首次安装

在仓库根目录执行：

```powershell
cd D:\Workshop\Spark

python -m pip install -r requirements.txt

cd console
npm ci
npm run build
cd ..
```

以后前端依赖未变化时无需重复执行 `npm ci`。

## 本机后端开发模式：连接 DGX Spark 模型

模型服务仅位于 DGX Spark 的 Docker 私有网络中。下面的命令动态读取容器私网目标，并建立只绑定本机回环地址的临时 SSH 隧道；不会开放公网端口，也不会修改或重启模型容器。

先在当前 PowerShell 会话设置本机已有的 SSH 别名：

```powershell
$SparkSshAlias = "your-existing-dgx-ssh-alias"
```

确认 SSH 和模型容器状态：

```powershell
ssh -o BatchMode=yes $SparkSshAlias `
    "docker ps --format '{{.Names}}|{{.Status}}|{{.Networks}}' | grep -E 'stepaudio|step3'"
```

如果以下两个端口已经连通，不要重复建立隧道：

```powershell
Test-NetConnection 127.0.0.1 -Port 18010
Test-NetConnection 127.0.0.1 -Port 18000
```

需要建立隧道时执行：

```powershell
$StepAudioContainer = (
    ssh -o BatchMode=yes $SparkSshAlias `
        "docker ps --filter name=companion-stepaudio --format '{{.Names}}' | head -n 1"
).Trim()

$Step3Container = (
    ssh -o BatchMode=yes $SparkSshAlias `
        "docker ps --filter name=companion-step3-vl --format '{{.Names}}' | head -n 1"
).Trim()

if (-not $StepAudioContainer -or -not $Step3Container) {
    throw "DGX model container discovery failed"
}

$StepAudioTarget = (
    ssh -o BatchMode=yes $SparkSshAlias `
        "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $StepAudioContainer"
).Trim()

$Step3Target = (
    ssh -o BatchMode=yes $SparkSshAlias `
        "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $Step3Container"
).Trim()

$SparkTunnel = Start-Process ssh.exe `
    -ArgumentList @(
        "-N",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-L", "127.0.0.1:18010:${StepAudioTarget}:8010",
        "-L", "127.0.0.1:18000:${Step3Target}:8000",
        $SparkSshAlias
    ) `
    -WindowStyle Hidden `
    -PassThru

$SparkTunnel.Id
```

验证两个回环端口：

```powershell
Test-NetConnection 127.0.0.1 -Port 18010
Test-NetConnection 127.0.0.1 -Port 18000
```

两个结果的 `TcpTestSucceeded` 都应为 `True`。

## 启动后端和控制台

如果曾在聊天、终端历史、截图或前端代码中暴露过 Audius API Key 或 Bearer Token，必须先在 Audius 控制台吊销并轮换；不要继续使用旧凭据。本 Demo 不使用 OAuth 或 Redirect URI，也不会把凭据发送到浏览器。

Audius 是可选能力。LLM 只能建议“情绪匹配音乐”，不能搜索、选择曲目、歌单 URL 或类别。确定性 Policy 将九种确认状态映射到五个固定类别：`RELAX`、`COMFORT`、`UPLIFT`、`COOLDOWN`、`NEUTRAL`。歌单 URL 必须由人工核准，并保存在被 Git 忽略的 `data/audius_playlists.local.json`；目录进程只保存公开 Track ID，不保存凭据、URL 或音频。

先复制示例配置，并把需要启用的类别改成人工核准的标准 Audius 歌单 URL（可只配置部分或一次配置很多歌；每类最多收录前 500 个去重 Track ID）：

```powershell
Copy-Item data/audius_playlists.example.json data/audius_playlists.local.json
notepad data/audius_playlists.local.json
```

在启动 Uvicorn 的同一个 PowerShell 会话中使用占位符配置轮换后的凭据：

```powershell
$env:SPARK_AUDIUS_ENABLED = "true"
$env:SPARK_AUDIUS_API_KEY = "<ROTATED_AUDIUS_API_KEY>"
$env:SPARK_AUDIUS_BEARER_TOKEN = "<ROTATED_AUDIUS_BEARER_TOKEN>"
```

不要把这些值写入 `.env`、README、歌单 JSON、目录数据库、前端构建或命令输出。API Key 和 Bearer Token 只存在于后端 `external-connector` 进程；Track Catalog 不接收它们，也没有公网访问能力。本 Demo 不需要 `SPARK_AUDIUS_TRACK_ID`。

在第二个 PowerShell 窗口启动独立的本地目录进程：

```powershell
cd D:\Workshop\Spark
.\scripts\start_audius_catalog.ps1
```

目录固定监听 `127.0.0.1:8011`，使用自己的 `data/audius_catalog.sqlite3`，按类别确定性轮转并对同一 `action_id` 返回相同租约。目录超过 24 小时视为 `STALE`。只有音乐 Action 获批后，后端才会在目录为空或过期时让 `external-connector` 同步歌单；拒绝、过期或错误 Action 不会同步、获取或播放。

任一凭据缺失、类别 URL 缺失/非法或功能未启用时，对应类别为 `NOT_CONFIGURED`，同一个已批准音乐 Action 只回退一次本地钢琴。配置完整但尚未实际同步时为 `CONFIGURED_NOT_PROBED`；`/v1/live/health` 只访问本地目录，绝不会借健康检查访问公网。

Audius API Host 固定为 `https://api.audius.co`。预览获取超时 5 秒、不重试、最多 8 MiB，音频只保留在内存，不进入磁盘、SQLite 或缓存。401、403、429、服务错误、非法元数据、受限曲目、不安全内容节点、错误音频类型或解码失败都会在同一个已批准音乐 Action 内降级一次，不会创建第二个 Action，也不会授权 AC。Audius 预览可能受服务端限制；控制台只报告“播放设备已启动”，不把它表述为人耳确认听到。

在新的 PowerShell 窗口中执行：

```powershell
cd D:\Workshop\Spark

$env:SPARK_STEPAUDIO_URL = "http://127.0.0.1:18010"
$env:SPARK_STEP3_URL = "http://127.0.0.1:18000"

python -m uvicorn backend.app.api:app `
    --host 127.0.0.1 `
    --port 8000
```

不要设置 `SPARK_STEPAUDIO_FILENAME`，除非已经确认 StepAudio 容器中存在获准使用的合成测试音频。禁止使用真实用户音频。

浏览器打开：

```text
http://127.0.0.1:8000/console/
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health

Invoke-RestMethod http://127.0.0.1:8000/v1/live/health |
    ConvertTo-Json -Depth 6
```

DGX 隧道正常时，LIVE 健康信息应显示 `STEP3 / READY` 和 `STEPAUDIO / READY`。这只表示服务可达；ASR/TTS 是否完成仍以对应请求的真实结果为准。

## 最短路径：根据文本分析用户状态

完成 DGX 隧道和后端启动后，打开 `http://127.0.0.1:8000/console/`，在页面顶部的“实验性文本状态分析”区域输入 1–500 个字符的中文或英文短文本，然后点击“分析状态”。Step3 会先返回九类状态候选，再生成暂定共情回复；进入交互区后，用户必须从同一组九类状态中确认，Step3 才会重新生成最终回复。确定性 Policy 随后可能创建零、一个或两个待授权 Action，音乐和 AC 始终使用不同的 `action_id`。

也可以直接调用接口：

```powershell
$Body = @{ text = "今天项目终于完成了，我特别开心。" } |
    ConvertTo-Json

$Analysis = Invoke-RestMethod `
    -Method POST `
    -Uri http://127.0.0.1:8000/v1/analysis/text `
    -ContentType "application/json" `
    -Body $Body

$Session = Invoke-RestMethod `
    -Method POST `
    -Uri "http://127.0.0.1:8000/v1/analysis/text/$($Analysis.analysis_id)/sessions" `
    -ContentType "application/json" `
    -Body "{}"

$Final = Invoke-RestMethod `
    -Method POST `
    -Uri "http://127.0.0.1:8000/v1/analysis/sessions/$($Session.session_id)/state-confirmation" `
    -ContentType "application/json" `
    -Body (@{ label = "HAPPY" } | ConvertTo-Json)

$Final | ConvertTo-Json -Depth 8
```

初次接口返回 `analysis_id`、一个主要候选、最多五个候选、暂定 `reaction` 和上海室外 `weather_context`。类别限定为：身体疲劳、情绪低落、开心、压力、焦虑、孤独、愤怒、平静和其他。确认接口接受对应的 `PHYSICAL_FATIGUE`、`EMOTIONAL_LOW`、`HAPPY`、`STRESSED`、`ANXIOUS`、`LONELY`、`ANGRY`、`CALM` 或 `OTHER`。`confidence` 是模型自报且未校准的数值，不能当作真实概率或诊断依据。

这条链路有以下明确边界：

- 实验性结果，不构成心理或医疗判断，也不承诺分类准确率；
- 初次分析不创建 Session、记忆、审计或 Action；原始输入不写入 SQLite，后端只将结构化候选、暂定反应和规范化天气快照在内存保留最多 5 分钟；
- `analysis_id` 只能使用一次；继续后会创建文本分析 Session，但仍强制用户澄清；
- 用户确认后必须重新调用 Step3 生成最终反应；失败时返回 HTTP `503`，Session 保持待澄清且不创建 Action；
- LLM 只给建议；Policy 可能接受或拒绝每项建议，创建的 Action 必须分别授权，绝不会自动执行；
- 音乐只允许五个固定的情绪类别逻辑 ID，LLM 不能选择实际曲目；已确认“不喜欢音乐”时拒绝。AC 仅在 `REAL_API`/`CACHE` 天气下按 `<=12°C` 加热或 `>=30°C` 制冷规则考虑，`FIXED_DEMO` 不具备真实推荐资格；
- 模型不可用或输出不满足严格 Schema 时返回 HTTP `503`，不使用规则结果冒充模型输出。

最终回复生成后，用户可以点击“朗读回复”调用 `POST /v1/analysis/sessions/{session_id}/tts`。只有合法且不超过 2 MiB 的 `audio/wav` 会在浏览器中临时播放；超时、错误类型、无效或过大响应都会返回 `TEXT_ONLY`。合成音频不写入磁盘、SQLite 或审计 Payload。

## 使用 Step3-VL 进行演示照片感知

控制台聊天页固定提供“室内有人”和“室内无人”两个演示入口。默认使用仓库内的合成插画；DGX 部署可以通过 `SPARK_DEMO_SCENE_MEDIA=private` 使用已获得授权、经过本地压缩且不纳入 Git 的 JPG 副本。选择照片并点击“分析所选照片”后，浏览器只发送固定 `scene_id`；DGX 后端读取白名单图片，并通过 `LOCAL` 网络调用现有 `step3-vl`。原图和压缩图均不写入 SQLite 或审计事件。

Step3-VL 只能返回人物是否出现、室内/室外/不确定、简短场景摘要、自报未校准置信度和有限证据。模型不能识别身份、推断情绪或敏感属性，也不能授权、执行、调用工具或写入记忆。识别到人物后只建立 LIVE 会话并等待固定演示文本，不启动语音；识别为空房间时不创建会话，或结束当前视觉会话。

模型超时、不可用、返回非法 JSON、包含禁止字段或不符合 Schema 时，接口返回 HTTP `503`，当前会话保持不变。系统不会读取素材的预设标签来冒充模型结果。PNG、data URL、模型原始请求和原始响应不会写入 SQLite 或审计 Payload。

接口示例：

```powershell
$Scenes = Invoke-RestMethod http://127.0.0.1:8000/v1/live/perception/scenes

$Result = Invoke-RestMethod `
    -Method POST `
    -Uri http://127.0.0.1:8000/v1/live/perception/analyze `
    -ContentType "application/json" `
    -Body (@{ scene_id = "indoor_person" } | ConvertTo-Json)

$Result | ConvertTo-Json -Depth 8
```

## 演示流程

### 真实 Step3 / 安全降级链

1. 在视觉感知区选择一张演示照片并点击“分析所选照片”。
2. 控制台显示 `SYNTHETIC_IMAGE`，不得称为真实摄像头输入。
3. Step3-VL 确认有人后，使用控制台提供的固定文字输入。
4. 后端调用 DGX Spark 上的真实 Step3-VL，并对视觉与状态输出分别执行严格 Schema 校验。
5. 用户从九类状态中确认，Step3 重新生成最终回复。
6. 查看 LLM 建议以及确定性 Policy 的接受或拒绝理由。
7. 分别处理可能出现的音乐和 AC 独立授权；零建议时无需授权。
8. 批准音乐后，控制台显示 `INTERNET 同步 → LOCAL 目录 → INTERNET 预览 → LOCAL 播放`、固定类别、`AUDIUS_PREVIEW` 或 `LOCAL_FALLBACK`、预览标记及降级原因；播放设备启动不代表有人确认听到完整音频。
9. AC 授权只产生清晰标记的 Mock 结果，不发生物理动作。
10. 检查天气来源、Privacy Guard、实际 outbound payload 和审计时间线。
11. 完成后点击“重置当前 Demo”。

### 纯 Mock 演示

DGX 或公网不可用时，可以点击“模拟人物回家”运行稳定的纯 Mock 场景：

1. 选择澄清答案；
2. 分别授权或拒绝音乐和 AC；
3. 检查 Action ID、状态迁移和审计事件；
4. 确认最终状态。

纯 Mock 模式不会调用 DGX 模型，不播放真实音乐，也不控制物理设备。

## 记忆和数据

控制台“回应偏好与脱敏摘要”区域可以确认或删除固定 Demo 偏好，并查看或一键清除情绪摘要。回应风格限定为 `GENTLE`、`CONCISE`、`DIRECT`；音乐偏好限定为 `EMOTION_MATCHED`、`NONE`。旧数据库中的 `CALM_PIANO` 会在迁移时转换为 `EMOTION_MATCHED`。未确认时运行时默认使用 `GENTLE`，但接口会明确返回 `confirmed=false`。默认数据库为：

```text
data/demo.sqlite3
```

每个文本 Session 在完成或关闭时最多写入一次摘要，并只保留最近 50 条。摘要只包含最终情绪、确认标记、置信度档位、反应 tone、音乐/AC 结果和时间戳；LLM 只读取状态计数和最近五个状态标签。

数据库不保存原始文本、evidence、回复正文、推理理由、完整天气响应、模型原始输出、音频或视频。旧 `memories` API 仍保留；已有已确认的 `calm_piano` 记忆会幂等映射为新的音乐偏好，旧记录不会被删除。不要把运行时数据库加入 Git。

管理接口：

- `GET /v1/user-preferences`
- `POST /v1/user-preferences/confirm`
- `DELETE /v1/user-preferences/{reply_style|music_preference}`
- `GET /v1/emotion-summaries`
- `DELETE /v1/emotion-summaries`

## 停止

在运行 Uvicorn 的终端按 `Ctrl+C`。

如果 `$SparkTunnel` 仍存在于创建隧道的 PowerShell 会话中，可以只停止本次隧道：

```powershell
Stop-Process -Id $SparkTunnel.Id
```

不要批量停止 SSH、StepAudio、Step3 或其他共享模型进程。

## 测试

Python 全套测试：

```powershell
python -m unittest discover -s tests
```

当前实际结果为 `188/188` 通过（0 失败、0 错误、0 跳过）。其中 Audius 自动化测试为 `13/13`、Track Catalog 为 `7/7`、情绪反应为 `18/18`，覆盖配置、传输安全、真实 `miniaudio` 内存解码、九类到五类映射、目录轮转/幂等/过期、授权门禁、内存播放和本地降级；网络部分使用合成响应与 Mock 传输，不代表真实 Audius 或物理扬声器验收。

前端测试和生产构建：

```powershell
npm --prefix console run test:run
npm --prefix console run build
```

当前前端测试实际结果为 `9/9` 通过，生产构建成功。

本轮没有读取轮换后的 Audius 运行时凭据，也没有配置人工核准的公开歌单 URL，因此真实 Audius 预览获取为 `0/5`、人工点击实际播放为 `0/1`。不得把自动化 Mock 结果称为真实 Audius 获取或人耳听到。

本轮还使用配置的真实 Step3 连续执行了 5 次合成端到端尝试；当前环境均返回 `MODEL_UNAVAILABLE`，结果为 `0/5` 成功，且没有批准任何物理动作。因此不能把这 5 次记录为真实模型验收通过。

完整 Phase 5 验收：

```powershell
$env:SPARK_STEPAUDIO_URL = "http://127.0.0.1:18010"
$env:SPARK_STEP3_URL = "http://127.0.0.1:18000"

python -m scripts.phase5_acceptance --samples 20
```

完整验收会实际调用模型和公网、运行每接口至少 20 次，并启动本机音乐设备，可能发出声音且需要数分钟。报告写入：

- `reports/phase5/results.json`
- `reports/phase5/REPORT.md`

只有本次实际执行至少 20 次的接口才可以报告 P95。

## 常见问题

- `/console/` 返回 `CONSOLE_NOT_BUILT`：执行 `npm --prefix console run build`。
- Step3 显示 `MODEL_UNAVAILABLE`：检查 18000 端口、SSH 隧道和 `SPARK_STEP3_URL`。
- StepAudio 显示 `MODEL_UNAVAILABLE`：检查 18010 端口和 `SPARK_STEPAUDIO_URL`。
- TTS 显示 `TEXT_ONLY`：检查 StepAudio TTS HTTP 契约、响应类型、WAV 完整性、2 MiB 上限和 120 秒超时；等待期间文本与控制台仍可用，不得声称语音播放成功。
- 摄像头显示 `STATIC_SYNTHETIC`：这是合成输入，不是真实摄像头画面。
- AC 显示“模拟执行成功”：它始终是 Mock，未执行物理动作。
- 天气来源为 `CACHE` 或 `FIXED_DEMO`：不得称为真实天气 API 成功。

## 更多文档

- [控制台说明](console/README.md)
- [Windows 用户访问现有 DGX 控制台](docs/WINDOWS_CONSOLE_ACCESS.md)
- [Step3 与 StepAudio 适配](docs/PHASE_3_ADAPTERS.md)
- [网络边界](docs/NETWORK_BOUNDARIES.md)
- [天气与动作边界](docs/PHASE_4_NETWORK_ACTIONS.md)
- [Phase 5 验收方法](docs/PHASE_5_TESTING.md)
- [情绪反应、Policy 与隐私安全记忆](docs/EMOTION_REACTION_MEMORY.md)
