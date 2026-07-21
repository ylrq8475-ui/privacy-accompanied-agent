# DGX Spark 主动陪伴 Demo 实施计划

## 一、总体技术原则

### 1. Step3 负责语义理解，不负责授权

Step3 只允许输出：

* 结构化状态候选；
* 每个候选的置信度；
* 推荐候选；
* 推荐理由；
* 可选的澄清问题候选。

示例：

```json
{
  "state_hypotheses": [
    {
      "label": "physical_fatigue",
      "confidence": 0.55,
      "evidence": ["用户表示今天有点累"]
    },
    {
      "label": "emotional_low",
      "confidence": 0.35,
      "evidence": ["表达存在情绪含义歧义"]
    },
    {
      "label": "other",
      "confidence": 0.1,
      "evidence": []
    }
  ],
  "recommended_action": {
    "type": "suggest_music",
    "category": "calm_piano"
  },
  "recommendation_reason": [
    "用户已确认属于身体疲惫",
    "当前为工作日晚间",
    "已有舒缓钢琴偏好"
  ]
}
```

Step3 不得决定：

* 是否自动执行；
* 是否写入长期记忆；
* 是否允许联网；
* 是否播放音乐；
* 是否控制空调；
* 是否跳过用户确认。

这些必须由确定性策略模块决定：

```text
Policy Engine
Authorization Manager
Privacy Guard
Action Executor
```

---

### 2. `external-connector` 是唯一互联网出口

所有访问公网的请求必须经过：

```text
external-connector
```

包括：

* 天气 API；
* 未来可能接入的音乐服务；
* 其他第三方 HTTP API。

其他模块禁止直接访问互联网：

```text
Orchestrator        × 不直接联网
Step3 Adapter       × 不直接联网
StepAudio Adapter   × 不直接联网
Memory              × 不直接联网
Music Agent         × 不直接联网
AC Agent            × 不直接联网
Console             × 不直接调用第三方 API
```

调用关系：

```text
Orchestrator
    ↓
Privacy Guard
    ↓
external-connector
    ↓
Internet API
```

---

### 3. 网络边界分成三类

所有工具调用和审计日志必须标记网络类型：

```text
LOCAL
LAN
INTERNET
```

定义如下：

| 类型       | 示例                           | 是否经过 external-connector |
| -------- | ---------------------------- | ----------------------: |
| LOCAL    | SQLite、Step3、StepAudio、本地播放器 |                       否 |
| LAN      | MQTT、Home Assistant、局域网空调接口  |                       否 |
| INTERNET | 天气 API、外部音乐 API              |                       是 |

每次调用记录：

```json
{
  "network_scope": "INTERNET",
  "source_agent": "weather_agent",
  "destination": "weather_api",
  "privacy_check": "passed",
  "payload": {
    "city_code": "310000"
  }
}
```

注意：

> `external-connector` 是唯一互联网出口，但不是所有工具调用的统一出口。LOCAL 和 LAN 请求不应绕行公网连接器。

---

### 4. 音乐和空调使用独立动作对象

音乐与空调不能共享一个模糊的“已授权”状态。

每一个动作都有独立：

* `action_id`；
* `action_type`；
* `authorization_status`；
* `expires_at`；
* `execution_status`；
* `result`。

示例：

```json
{
  "action_id": "music-20260720-001",
  "action_type": "PLAY_MUSIC",
  "authorization_status": "PENDING",
  "execution_status": "NOT_STARTED",
  "payload": {
    "track_id": "calm_piano_01"
  }
}
```

空调动作：

```json
{
  "action_id": "ac-20260720-001",
  "action_type": "SET_AC",
  "authorization_status": "PENDING",
  "execution_status": "NOT_STARTED",
  "payload": {
    "device_id": "living_room_ac",
    "mode": "heat",
    "target_temperature": 24,
    "duration_minutes": 30
  }
}
```

授权规则：

```text
授权 music action_id
≠
授权 ac action_id
```

用户同意播放音乐，不能被解释为空调也已获授权。

---

# Phase 0：环境审计与 Demo 基线

## 目标

确认远端 DGX Spark 当前状态，不修改现有模型和容器。

## 任务

1. 检查仓库结构。
2. 检查运行中的容器。
3. 确认 StepAudio 接口：

   * 健康检查；
   * ASR 请求格式；
   * TTS 请求格式；
   * 内部网络地址。
4. 确认 Step3-VL 接口：

   * 健康检查；
   * 请求格式；
   * 内部网络地址。
5. 确认摄像头访问方式。
6. 确认可用于 Demo 的本地音乐。
7. 检查：

   * 内存；
   * 磁盘；
   * GPU；
   * Docker 网络；
   * 端口监听。
8. 创建：

   * `docs/DEMO_BASELINE.md`
   * `docs/DEMO_SERVICES.md`
   * `docs/NETWORK_BOUNDARIES.md`
   * `docs/DEMO_RISKS.md`

## 禁止事项

* 不停止现有模型容器；
* 不重启现有模型容器；
* 不更改模型参数；
* 不下载新模型；
* 不修改模型权重；
* 不开放新的公网端口。

## 验收

* Git 修改仅限文档；
* 原有容器状态不变；
* 文档不包含密钥和完整公网凭据；
* 明确列出 LOCAL、LAN、INTERNET 边界；
* 明确哪些服务属于 Demo，哪些属于已有模型基础设施。

完成后停止。

---

# Phase 1A：接口契约与隐私边界

## 目标

先冻结所有数据结构、隐私规则和网络出口，再实现业务链路。

## 实现内容

### 1. 统一事件模型

至少包含：

```text
event_id
session_id
timestamp
source_agent
event_type
payload
confidence
privacy_level
network_scope
action_id
latency_ms
status
```

### 2. Step3 结构化输出契约

定义：

```text
StateHypothesis
RecommendationCandidate
RecommendationReason
ClarificationCandidate
```

要求：

* 使用 Pydantic 严格校验；
* 禁止接收任意自由格式执行指令；
* Step3 输出不能直接进入执行器；
* 输出必须先经过 Policy Engine。

### 3. 独立 Action 模型

定义：

```text
ActionProposal
ActionAuthorization
ActionExecution
ActionResult
```

授权状态：

```text
PENDING
APPROVED
REJECTED
EXPIRED
REVOKED
```

执行状态：

```text
NOT_STARTED
RUNNING
SUCCEEDED
FAILED
CANCELLED
```

### 4. Privacy Guard

天气公网请求允许：

```json
{
  "city_code": "310000"
}
```

音乐公网请求允许：

```json
{
  "action": "play",
  "track_id": "calm_piano_01"
}
```

空调局域网请求允许：

```json
{
  "device_id": "living_room_ac",
  "mode": "heat",
  "target_temperature": 24,
  "duration_minutes": 30
}
```

禁止字段至少包括：

```text
raw_audio
raw_video
emotion
state_hypotheses
user_schedule
arrival_history
memory_content
recommendation_reason
conversation_history
```

### 5. `external-connector` 契约

必须实现：

* 目标服务白名单；
* Payload Schema 校验；
* 请求超时；
* 审计日志；
* 响应大小限制；
* 禁止自动重试敏感动作；
* 明确标记 `network_scope=INTERNET`。

## 测试要求

接口测试每类至少覆盖：

* 正常输入；
* 缺少字段；
* 多余字段；
* 非法类型；
* 隐私字段注入；
* 非法状态；
* 重复授权；
* 过期授权。

## 验收

* 所有 Schema 测试通过；
* Step3 输出不能直接触发动作；
* 非 `external-connector` 模块不能调用公网适配器；
* 音乐与空调动作拥有不同 `action_id`；
* Privacy Guard 可以拒绝嵌套在对象中的敏感字段；
* 测试结果写入 `docs/PHASE_1A_RESULTS.md`。

完成后停止。

---

# Phase 1B：纯 Mock 场景链路

## 目标

不调用真实模型、摄像头或公网 API，跑通完整 Demo 状态机。

## 实现内容

1. Orchestrator。
2. Session State。
3. 状态机。
4. Mock Vision。
5. Mock ASR。
6. Mock Step3 结构化输出。
7. Deterministic Policy Engine。
8. Clarification 流程。
9. Weather Mock。
10. Music Mock。
11. AC Mock。
12. Privacy Guard。
13. `external-connector` Mock。
14. HTTP API。
15. WebSocket 事件推送。

## 状态链

```text
IDLE
→ PERSON_DETECTED
→ CONTEXT_READY
→ LISTENING
→ MOOD_ANALYSIS
→ CLARIFICATION_REQUIRED
→ USER_CLARIFIED
→ MEMORY_RETRIEVED
→ ACTION_PROPOSED
→ WAITING_MUSIC_AUTHORIZATION
→ MUSIC_AUTHORIZED
→ MUSIC_EXECUTED
→ WAITING_AC_AUTHORIZATION
→ AC_AUTHORIZED
→ AC_EXECUTED
→ COMPLETED
```

异常状态：

```text
MODEL_TIMEOUT
API_FAILED
CAMERA_FAILED
ASR_FAILED
PRIVACY_REJECTED
ACTION_REJECTED
ACTION_EXPIRED
ACTION_FAILED
```

## 确定性澄清策略

示例规则：

```text
最高候选置信度 < 0.70
或
最高两项候选差值 < 0.25
→ 必须澄清
```

该规则由 Policy Engine 执行，而不是由 Step3 自由决定。

## 确定性授权策略

```text
音乐 action 未 APPROVED
→ 不允许播放

空调 action 未 APPROVED
→ 不允许执行

授权 action_id 与待执行 action_id 不一致
→ 拒绝执行

授权已过期
→ 拒绝执行
```

## 验收

* 固定 Mock 场景端到端必须达到 **5/5 成功**；
* 不能以“至少 4/5”作为通过条件；
* 每轮使用新的 `session_id`；
* 音乐和空调分别授权；
* 拒绝音乐后仍可继续询问空调；
* 授权空调不能触发音乐；
* Step3 输出恶意执行文字时不会执行；
* Privacy Guard 拒绝后请求不会发送；
* 所有状态变化都写入审计事件。

完成后停止。

---

# Phase 1C：SQLite 记忆与动作持久化

## 目标

增加 Demo 所需的真实持久化，但不实现通用用户系统。

## SQLite 保存内容

### 用户确认记忆

```text
memory_id
context
preference
confirmed
created_at
updated_at
```

### 动作记录

```text
action_id
session_id
action_type
authorization_status
execution_status
payload
created_at
authorized_at
executed_at
result
```

### 审计日志

```text
audit_id
session_id
event_type
network_scope
privacy_result
payload_digest
created_at
```

## 规则

* 未确认的偏好不能持久化；
* 删除记忆后不可被 Decision 再次检索；
* 不在数据库中保存原始音频；
* 不在数据库中保存原始视频；
* 默认不持久化完整 ASR 对话；
* 动作 Payload 仅保存 Demo 执行所需字段；
* 音乐和空调动作必须分别持久化。

## 验收

* 重启 Demo 后，已确认记忆仍存在；
* 删除记忆后重启仍保持删除；
* 未确认记忆重启后不存在；
* Action 状态可以正确恢复；
* `PENDING` 动作重启后不得自动执行；
* 已过期授权重启后不得恢复成 `APPROVED`；
* SQLite 测试全部通过；
* 数据库迁移和初始化可重复执行。

完成后停止。

---

# Phase 2：2D Demo 控制台

## 目标

让评委在一个页面内看懂完整决策和隐私链路。

## 左侧：感知区

显示：

* 摄像头或 Mock 画面；
* 人物检测框；
* Vision Agent 状态；
* 人物出现和离开事件；
* Step3-VL 调用状态；
* 输入源类型：Camera、Video、Mock。

## 中间：交互与判断区

显示：

* 用户语音；
* ASR 文本；
* Step3 状态候选；
* 每个候选的置信度；
* Policy Engine 的阈值判断；
* 澄清问题；
* 用户回答；
* 系统回复；
* 文字输入备用入口。

必须明确区分：

```text
Step3 输出
Policy Engine 决策
用户授权
执行结果
```

## 右侧：环境、记忆和隐私区

显示：

* 天气；
* 用户已确认记忆；
* 删除记忆按钮；
* Step3 推荐理由；
* 音乐 Action；
* 空调 Action；
* 两个 Action 的独立授权状态；
* Privacy Guard 结果；
* 实际外发 JSON；
* 网络范围：LOCAL、LAN、INTERNET。

## 底部：执行状态区

显示：

* Agent 时间线；
* 当前 Orchestrator 状态；
* `session_id`；
* `action_id`；
* 模型状态；
* 降级状态；
* 各阶段 `latency_ms`；
* 请求是否经过 `external-connector`。

## 控制按钮

至少提供：

* 模拟人物回家；
* 使用文字输入；
* 同意音乐；
* 拒绝音乐；
* 同意空调；
* 拒绝空调；
* 删除记忆；
* 模拟天气失败；
* 模拟 Step3 超时；
* 模拟 ASR 失败；
* 触发隐私违规请求；
* 重置当前 Demo。

## 验收

* 控制台可完整运行纯 Mock 场景；
* 页面显示真实后端事件；
* 外发 JSON 来自真实请求对象；
* 音乐和空调授权按钮绑定不同 `action_id`；
* 页面能够显示 LOCAL、LAN、INTERNET；
* Privacy Guard 拒绝后显示具体违规字段；
* 页面刷新不会误执行待授权动作。

完成后停止。

---

# Phase 3：真实摄像头、StepAudio 与 Step3

## 目标

保持状态机和授权规则不变，只替换真实输入 Adapter。

## 摄像头与 Vision

实现：

* 摄像头健康检查；
* 人物出现检测；
* 人物离开检测；
* 预录视频降级；
* 静态场景降级；
* 仅在关键事件调用 Step3-VL；
* 不持续高频调用重模型。

## StepAudio

实现：

* ASR Adapter；
* 健康检查；
* 请求超时；
* 延迟记录；
* ASR 失败切换文字输入；
* TTS 可使用实时生成或预生成音频；
* TTS 延迟时先显示文本。

## Step3

实现：

* 严格结构化输出；
* Schema 校验；
* 候选状态；
* 置信度；
* 推荐候选；
* 推荐理由；
* 非法输出降级到规则模板。

Step3 不得返回或决定：

```text
authorization_status = APPROVED
execute = true
skip_confirmation = true
write_memory = true
```

如果出现这些字段，必须被 Schema 或 Policy Engine 拒绝。

## 禁止事项

* 不修改现有模型容器；
* 不自动重启现有模型容器；
* 不新建第二套模型副本；
* 不让模型直接调用工具；
* 不让模型直接访问公网；
* 不把真实音视频提交到仓库。

## 验收

* 摄像头人物出现能启动流程；
* 用户说“今天有点累”后获得 ASR；
* Step3 返回结构化状态候选；
* Policy Engine 根据阈值进入澄清；
* ASR 失败后可以继续文字输入；
* Step3 超时后可以使用固定候选和规则继续；
* 模型错误不会越过授权；
* 所有模型请求记录延迟。

完成后停止。

---

# Phase 4：天气、音乐、空调 Mock 与网络审计

## 目标

完成真实天气、本地音乐和空调 Mock，严格落实网络边界。

## 天气

调用链：

```text
Weather Agent
→ Privacy Guard
→ external-connector
→ Weather API
```

只允许：

```json
{
  "city_code": "310000"
}
```

失败时：

```text
真实 API
→ 带时间戳缓存
→ 固定 Demo 天气
```

控制台必须标记当前数据来源。

## 音乐

Demo 默认使用本地播放器：

```text
network_scope = LOCAL
```

执行时只接收：

```json
{
  "action": "play",
  "track_id": "calm_piano_01"
}
```

播放前验证：

* `action_id` 匹配；
* 授权状态为 `APPROVED`；
* 授权未过期；
* 动作未重复执行。

## 空调

使用 AC Mock：

```text
network_scope = LOCAL
```

未来接入局域网设备时：

```text
network_scope = LAN
```

AC Mock 必须显示：

```text
模拟执行成功
```

不能显示：

```text
实体空调已成功开启
```

## 验收

* 所有公网调用仅通过 `external-connector`；
* 后端其他模块没有公网请求能力；
* 天气请求的实际 Payload 只包含 `city_code`；
* 用户拒绝音乐后不会播放；
* 用户批准音乐不会批准空调；
* 本地音乐实际发声；
* AC Mock 状态正确更新；
* 控制台准确区分 LOCAL、LAN、INTERNET；
* 隐私违规请求被阻止且没有到达连接器。

完成后停止。

---

# Phase 5：测试、性能和恢复能力

## 目标

证明 Demo 可重复运行，并解决“禁止重启”和“恢复测试”的矛盾。

## 重启规则

### 开发阶段禁止 Codex 自动重启

Codex 不得自动：

* 重启 StepAudio；
* 重启 Step3-VL；
* 重启已有模型 Compose；
* 重启宿主机；
* 停止共享基础设施。

### 允许进行恢复测试的对象

可以自动重启：

* `demo-backend`；
* `demo-console`；
* `external-connector`；
* Demo SQLite 服务或挂载；
* Music Mock；
* AC Mock。

### 模型恢复测试

模型容器恢复测试只能在以下条件下进行：

1. 人工明确授权；
2. 进入单独维护窗口；
3. 先记录当前模型状态；
4. 有明确回滚步骤；
5. 不影响共享用户；
6. 不由 Codex自行决定执行。

因此测试报告应区分：

```text
Demo-owned services restart recovery
Existing model services health degradation fallback
Authorized model restart recovery
```

默认必须完成前两项；第三项只有获授权后才做。

---

## 接口测试要求

每个关键接口至少执行 **20 次** 后，才能计算 P95。

关键接口包括：

* Mock Step3；
* 真实 Step3；
* Mock ASR；
* 真实 ASR；
* Weather Connector；
* SQLite Memory；
* Action Authorization；
* Privacy Guard；
* Music Executor；
* AC Mock。

统计：

```text
count
success_count
failure_count
success_rate
min
max
mean
P50
P95
```

20 次只是最低要求。条件允许时，建议执行 30～50 次。

---

## 端到端测试要求

完整 Demo 场景：

```text
5/5 成功
```

不能使用：

```text
4/5 即通过
```

每轮检查：

* 摄像头或降级输入；
* ASR 或文字降级；
* Step3 结构化输出；
* 澄清流程；
* Memory；
* Music Action 授权；
* AC Action 授权；
* Privacy Guard；
* 审计日志；
* 最终状态。

## 故障测试

必须覆盖：

* Camera 失败；
* ASR 失败；
* Step3 超时；
* Step3 非法结构化输出；
* Weather API 失败；
* `external-connector` 不可用；
* TTS 延迟；
* SQLite 失败；
* Privacy Guard 拒绝；
* 音乐播放失败；
* AC Mock 失败；
* Action 授权过期；
* Action ID 不匹配；
* 重复执行同一动作。

## 验收

* 固定端到端场景 5/5 成功；
* 每个关键接口至少运行 20 次；
* P50/P95 基于真实测试样本计算；
* Demo 服务重启后能够恢复；
* 待授权动作在重启后不会自动执行；
* 模型服务不可用时可以降级完成 Demo；
* 测试结果保存为机器可读 JSON 和 Markdown 报告。

完成后停止。

---

# Phase 6：比赛提交与公开材料

## 目标

把能够运行的 Demo 转化为完整初赛提交作品。

## 6.1 Demo 视频

制作：

* 完整主流程视频；
* 隐私边界特写；
* Privacy Guard 拒绝请求演示；
* 模型或 API 失败降级演示；
* DGX Spark 运行证据；
* 本地模型和容器状态画面。

视频脚本至少包括：

```text
问题背景
DGX Spark 的价值
人物回家
模糊状态表达
主动澄清
读取已确认记忆
独立音乐授权
独立空调授权
实际动作执行
隐私审计
故障降级
```

## 6.2 公开仓库

必须清理：

* SSH 信息；
* API Token；
* `.env`；
* 真实音视频；
* 私有 SQLite 数据库；
* 模型权重；
* 私有 IP；
* 用户记忆；
* 内部服务凭据。

仓库至少包含：

```text
README.md
LICENSE
.env.example
docker-compose.demo.yml
AGENTS.md

backend/
console/
external-connector/
tests/
scripts/
docs/
assets/
```

## 6.3 许可证

明确：

* 项目源代码许可证；
* 本地音乐素材许可证；
* 图片和图标来源；
* 使用模型的许可证；
* 第三方依赖许可证。

不要把“开源代码”与“模型权重允许重新分发”混为一谈。

## 6.4 团队材料

准备：

* 团队名称；
* 成员介绍；
* 分工；
* 团队合影；
* 开发过程截图；
* DGX Spark 使用截图；
* Codex 协作开发说明。

## 6.5 初赛提交材料

至少完成：

* 600 字以上项目说明；
* 项目简介；
* 技术架构；
* DGX Spark 适配说明；
* 多智能体设计；
* 隐私设计；
* Demo 视频；
* 公开仓库；
* 部署文档；
* 测试报告；
* 评分标准对应说明；
* “黑客松十日谈”；
* 提交检查表。

## Phase 6 验收

* 仓库可由新环境按 README 启动；
* 公开仓库秘密扫描通过；
* Demo 视频与实际代码行为一致；
* 所有 Mock 都明确标记；
* 不宣称未实现的实体设备控制；
* 文档明确区分开发阶段云端工具和运行阶段本地模型；
* 提交材料中的性能数据可追溯到测试报告。

---

# 推荐仓库结构

```text
companion-demo/
├── AGENTS.md
├── README.md
├── LICENSE
├── .env.example
├── docker-compose.demo.yml
│
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── orchestrator.py
│   │   ├── schemas/
│   │   │   ├── events.py
│   │   │   ├── step3.py
│   │   │   ├── actions.py
│   │   │   └── network.py
│   │   ├── policy/
│   │   │   ├── clarification.py
│   │   │   ├── authorization.py
│   │   │   └── execution.py
│   │   ├── privacy_guard.py
│   │   ├── memory.py
│   │   └── adapters/
│   │       ├── step3.py
│   │       ├── audio.py
│   │       ├── vision.py
│   │       ├── music.py
│   │       └── ac_mock.py
│   └── tests/
│
├── external-connector/
│   ├── app/
│   └── tests/
│
├── console/
│   ├── src/
│   └── package.json
│
├── data/
│   ├── weather_cache.example.json
│   └── music/
│
├── scripts/
│   ├── start_demo.sh
│   ├── stop_demo.sh
│   ├── reset_demo.sh
│   ├── run_contract_tests.sh
│   ├── run_benchmark.sh
│   └── run_e2e.sh
│
├── tests/
│   ├── contracts/
│   ├── privacy/
│   ├── scenarios/
│   ├── recovery/
│   └── performance/
│
└── docs/
    ├── DEMO_BASELINE.md
    ├── NETWORK_BOUNDARIES.md
    ├── PRIVACY_ARCHITECTURE.md
    ├── DEMO_RUNBOOK.md
    ├── DEMO_SCRIPT.md
    ├── TEST_RESULTS.md
    ├── DEPLOYMENT_DGX_SPARK.md
    └── SUBMISSION_CHECKLIST.md
```

# 最终阶段顺序

```text
Phase 0
环境与接口审计
        ↓
Phase 1A
Schema、隐私、网络和动作契约
        ↓
Phase 1B
纯 Mock 端到端状态机
        ↓
Phase 1C
SQLite 记忆、动作和审计持久化
        ↓
Phase 2
2D 控制台
        ↓
Phase 3
真实摄像头、StepAudio、Step3
        ↓
Phase 4
天气、本地音乐、AC Mock、联网审计
        ↓
Phase 5
5/5 端到端、20+ 接口测试、P95、恢复测试
        ↓
Phase 6
视频、公开仓库、许可证、团队资料、初赛提交
```

每个 Phase 完成后，Codex 都必须停止并输出：

```text
1. 完成内容
2. 修改文件
3. 执行命令
4. 测试结果
5. 测试样本数量
6. 未解决问题
7. 风险
8. 回滚方式
9. 下一阶段建议
```

不得自动开始下一阶段。
