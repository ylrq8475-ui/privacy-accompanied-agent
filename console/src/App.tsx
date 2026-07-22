import { Fragment, useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { APIError, api, connectEventStream } from "./api";
import type { DemoText, MockScenario } from "./api";
import { buildConversationProjection } from "./conversation";
import type { ConversationACDecision } from "./conversation";
import {
  actionButtonsEnabled,
  initialState,
  latestEvent,
  latestEventOfTypes,
  reducer,
  transitionTo,
} from "./state";
import type {
  ActionProposal,
  AuditEvent,
  MemoryRecord,
  PersistedAction,
  SessionSnapshot,
  LiveHealth,
  MusicCatalogResponse,
  TextAnalysisResponse,
  TextStateLabel,
  UserPreferences,
  EmotionSummary,
  SyntheticScene,
  VisualPerceptionResponse,
  CityCode,
} from "./types";

const CITY_OPTIONS: Array<{ code: CityCode; name: string }> = [
  { code: "110000", name: "北京" },
  { code: "310000", name: "上海" },
  { code: "440100", name: "广州" },
  { code: "440300", name: "深圳" },
  { code: "510100", name: "成都" },
];

const CITY_NAMES: Record<CityCode, string> = Object.fromEntries(
  CITY_OPTIONS.map((city) => [city.code, city.name]),
) as Record<CityCode, string>;

const EMOTION_OPTIONS: TextStateLabel[] = [
  "PHYSICAL_FATIGUE",
  "EMOTIONAL_LOW",
  "HAPPY",
  "STRESSED",
  "ANXIOUS",
  "LONELY",
  "ANGRY",
  "CALM",
  "OTHER",
];

const SCENARIOS: Array<{ scenario: MockScenario; label: string; tone?: string }> = [
  { scenario: "success", label: "模拟人物回家", tone: "primary" },
  { scenario: "api_failed", label: "天气失败" },
  { scenario: "model_timeout", label: "Step3 超时" },
  { scenario: "asr_failed", label: "语音识别失败" },
  { scenario: "privacy_rejected", label: "隐私违规" },
];

const TEXT_OPTIONS: DemoText[] = ["主要是身体累", "主要是心情低落", "其他原因"];

const CLARIFICATION_BY_ANALYSIS: Record<string, DemoText> = {
  PHYSICAL_FATIGUE: "主要是身体累",
  EMOTIONAL_LOW: "主要是心情低落",
  STRESSED: "主要是心情低落",
  ANXIOUS: "主要是心情低落",
  LONELY: "主要是心情低落",
  ANGRY: "主要是心情低落",
  HAPPY: "其他原因",
  CALM: "其他原因",
  OTHER: "其他原因",
};

const STATE_COPY: Record<string, string> = {
  IDLE: "等待人物进入感知区域",
  PERSON_DETECTED: "已检测到人物，正在准备环境上下文",
  CONTEXT_READY: "环境上下文已就绪",
  LISTENING: "正在监听合成语音输入",
  MOOD_ANALYSIS: "Step3 正在生成结构化候选",
  CLARIFICATION_REQUIRED: "确定性策略要求用户澄清",
  USER_CLARIFIED: "已接收用户澄清，准备检索记忆",
  MEMORY_RETRIEVED: "已读取确认记忆",
  ACTION_PROPOSED: "已创建两个独立动作",
  WAITING_MUSIC_AUTHORIZATION: "等待音乐动作授权",
  MUSIC_AUTHORIZED: "音乐已授权，正在调用本机播放器",
  MUSIC_EXECUTED: "音乐执行完成，结果来源见动作卡片",
  WAITING_AC_AUTHORIZATION: "等待空调动作独立授权",
  AC_AUTHORIZED: "空调已授权，进入模拟执行",
  AC_EXECUTED: "空调模拟已完成，未发生物理动作",
  COMPLETED: "本轮演示已完成；空调仍为模拟",
  MODEL_TIMEOUT: "Step3 模拟超时，流程已安全停止",
  API_FAILED: "天气连接器模拟失败，流程已安全停止",
  CAMERA_FAILED: "视觉模拟失败，未检测到人物",
  ASR_FAILED: "语音识别模拟失败，可重新开始并使用文本输入",
  PRIVACY_REJECTED: "隐私网关已拒绝违规数据",
  ACTION_REJECTED: "用户拒绝当前动作",
  ACTION_EXPIRED: "动作授权已过期",
  ACTION_FAILED: "动作执行失败，未伪造成功",
};

const LABELS: Record<string, string> = {
  PHYSICAL_FATIGUE: "身体疲劳",
  EMOTIONAL_LOW: "情绪低落",
  HAPPY: "开心",
  STRESSED: "压力",
  ANXIOUS: "焦虑",
  LONELY: "孤独",
  ANGRY: "愤怒",
  CALM: "平静",
  OTHER: "其他",
  PLAY_MUSIC: "播放情绪匹配音乐",
  SET_AC: "设置空调",
  SUGGEST_MUSIC: "建议音乐",
};

const STATUS_LABELS: Record<string, string> = {
  READY: "就绪",
  ANALYSIS_READY: "分析完成",
  NO_SESSION: "尚无会话",
  TEXT_ANALYSIS: "文本分析",
  "TEXT FIRST": "文本优先",
  LIVE: "实时模式",
  IDLE: "空闲",
  PERSON_DETECTED: "检测到人物",
  CONTEXT_READY: "上下文就绪",
  LISTENING: "正在监听",
  MOOD_ANALYSIS: "状态分析中",
  CLARIFICATION_REQUIRED: "需要澄清",
  USER_CLARIFIED: "用户已澄清",
  MEMORY_RETRIEVED: "已读取记忆",
  ACTION_PROPOSED: "动作已提出",
  WAITING_MUSIC_AUTHORIZATION: "等待音乐授权",
  MUSIC_AUTHORIZED: "音乐已授权",
  MUSIC_EXECUTED: "音乐已执行",
  WAITING_AC_AUTHORIZATION: "等待空调授权",
  AC_AUTHORIZED: "空调已授权",
  AC_EXECUTED: "空调模拟已执行",
  MODEL_TIMEOUT: "模型超时",
  API_FAILED: "接口失败",
  CAMERA_FAILED: "摄像头失败",
  ASR_FAILED: "语音识别失败",
  PRIVACY_REJECTED: "隐私校验拒绝",
  ACTION_REJECTED: "动作已拒绝",
  ACTION_EXPIRED: "动作已过期",
  ACTION_FAILED: "动作失败",
  MOCK: "模拟",
  TEXT_ONLY: "纯文本",
  CAMERA: "摄像头",
  VIDEO: "演示视频",
  STATIC_SYNTHETIC: "合成静态场景",
  SYNTHETIC_IMAGE: "合成图片",
  DEMO_IMAGE: "演示照片",
  INDOOR: "室内",
  OUTDOOR: "室外",
  UNCERTAIN: "无法确定",
  NOT_USED: "未使用",
  MOCK_READY: "模拟就绪",
  NOT_APPLICABLE: "不适用",
  PERSON_LEFT: "人物已离开",
  STRUCTURED_OUTPUT: "结构化输出",
  NONE: "无",
  WAITING: "等待中",
  PENDING: "待确认",
  NOT_STARTED: "未开始",
  APPROVED: "已批准",
  AUTHORIZED: "已授权",
  EXECUTED: "已执行",
  COMPLETED: "已完成",
  REJECTED: "已拒绝",
  DENIED: "已拒绝",
  PASSED: "已通过",
  FAILED: "失败",
  EXPIRED: "已过期",
  OBSERVED: "已观测",
  HEALTHY: "正常",
  DEGRADED: "已降级",
  AVAILABLE: "可用",
  UNAVAILABLE: "不可用",
  REAL_API: "实时接口",
  CACHE: "缓存数据",
  FIXED_DEMO: "固定演示数据",
  SUPPORTIVE: "支持",
  CELEBRATORY: "庆祝",
  CALMING: "安抚",
  GROUNDING: "稳定",
  NEUTRAL: "中性",
  NOT_SUGGESTED: "未建议",
  SUCCEEDED: "已完成",
  CONFIGURED_NOT_CALLED: "已配置，尚未调用",
  READY_NOT_PLAYED: "就绪，尚未播放",
  NOT_CONFIGURED: "未配置",
  DGX_SPARK: "DGX Spark",
  SSH_LOOPBACK: "SSH 本机回环",
  clear: "晴朗",
  partly_cloudy: "多云",
  overcast: "阴天",
  fog: "有雾",
  drizzle: "毛毛雨",
  rain: "下雨",
  snow: "下雪",
  rain_showers: "阵雨",
  snow_showers: "阵雪",
  thunderstorm: "雷暴",
  "NOT INVOKED": "未调用",
  GENTLE: "温和",
  CONCISE: "简洁",
  DIRECT: "直接",
  EMOTION_MATCHED: "按情绪匹配",
  OTHER: "其他",
};

const SCOPE_LABELS: Record<string, string> = {
  LOCAL: "本机",
  LAN: "局域网",
  INTERNET: "互联网",
};

const COMPONENT_LABELS: Record<string, string> = {
  HPA: "服务入口",
  VISION: "视觉模型",
  STEPAUDIO: "语音模型",
  STEP3: "文本模型",
  WEATHER_EGRESS: "天气出口",
  LOCAL_MUSIC: "本机音乐",
  AUDIUS_MUSIC: "Audius 音乐",
  AUDIUS_CATALOG: "Audius 目录",
};

function displayStatus(value: string): string {
  return STATUS_LABELS[value] ?? LABELS[value] ?? value;
}

function displayComponent(value: string): string {
  return COMPONENT_LABELS[value] ?? value;
}

const MUSIC_PLAYLISTS: Record<string, { key: string; label: string }> = {
  calm_piano_01: { key: "RELAX", label: "放松" },
  emotion_relax_01: { key: "RELAX", label: "放松" },
  emotion_comfort_01: { key: "COMFORT", label: "安慰" },
  emotion_uplift_01: { key: "UPLIFT", label: "轻快" },
  emotion_cooldown_01: { key: "COOLDOWN", label: "降温" },
  emotion_neutral_01: { key: "NEUTRAL", label: "中性" },
};

const MUSIC_CATEGORY_LABELS: Record<string, string> = {
  RELAX: "放松",
  COMFORT: "安慰",
  UPLIFT: "轻快",
  COOLDOWN: "平复",
  NEUTRAL: "中性",
};

type ConsoleView = "chat" | "audit";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function pretty(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}

function formatMetric(value: unknown): string {
  const metric = Number(value);
  return Number.isFinite(metric) ? metric.toFixed(2) : "—";
}

function timeLabel(timestamp: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
  }).format(new Date(timestamp));
}

function humanError(error: unknown): string {
  if (error instanceof APIError) return `${error.status} · ${error.message}`;
  return error instanceof Error ? error.message : "未知错误";
}

function ScopeBadge({ scope }: { scope: string }) {
  return <span className={`scope scope-${scope.toLowerCase()}`}>{SCOPE_LABELS[scope] ?? scope}</span>;
}

function StatusPill({ value, muted = false }: { value: string; muted?: boolean }) {
  return <span className={`status-pill ${muted ? "muted" : ""}`}>{displayStatus(value)}</span>;
}

interface ActionCardProps {
  title: string;
  proposal: ActionProposal | null;
  persisted?: PersistedAction;
  session: SessionSnapshot | null;
  enabled: boolean;
  busy: boolean;
  onDecision: (approved: boolean) => void;
  testId: string;
  compact?: boolean;
  showTechnicalDetails?: boolean;
}

function ActionCard({
  title,
  proposal,
  persisted,
  session,
  enabled,
  busy,
  onDecision,
  testId,
  compact = false,
  showTechnicalDetails = true,
}: ActionCardProps) {
  const authorization = proposal ? session?.authorizations[proposal.action_id] : undefined;
  const result = proposal ? session?.results[proposal.action_id] : undefined;
  const authorizationStatus = persisted?.authorization_status ?? authorization?.authorization_status;
  const executionStatus = persisted?.execution_status ?? result?.execution_status ?? "NOT_STARTED";
  const resultData = persisted?.result ?? result?.result;
  const isMusic = proposal?.action_type === "PLAY_MUSIC";
  const logicalTrackId = typeof proposal?.payload.track_id === "string" ? proposal.payload.track_id : "";
  const proposedPlaylist = MUSIC_PLAYLISTS[logicalTrackId];
  const musicSource = typeof resultData?.source === "string" ? resultData.source : null;
  const browserDeliveryReady = isMusic && resultData?.delivery_ready === true;
  const resultTitle = browserDeliveryReady
    ? "音频已安全交付"
    : musicSource === "AUDIUS_PREVIEW"
    ? "Audius 预览播放"
    : musicSource === "LOCAL_FALLBACK"
      ? "本地钢琴曲降级播放"
      : isMusic && resultData?.mock === false
        ? "本机播放结果"
        : isMusic
          ? "模拟执行结果"
          : "空调模拟执行完成";
  const resultDetail = browserDeliveryReady
    ? "Spark 已准备一次性音频；以浏览器播放器状态为实际播放依据。"
    : isMusic && resultData?.playback_started === true
    ? "播放设备已启动；这不等于确认人耳听到。"
    : isMusic
      ? String(resultData?.message ?? "未执行物理动作。")
      : "未控制实体空调。";
  return (
    <article className={`action-card ${compact ? "chat-action-card" : ""}`}>
      <div className="card-heading">
        <div>
          <span className="eyebrow">独立授权对象</span>
          <h3>{title}</h3>
        </div>
        {isMusic ? (
          <span className="scope-chain"><ScopeBadge scope="INTERNET" /> → <ScopeBadge scope="LOCAL" /></span>
        ) : <ScopeBadge scope="LOCAL" />}
      </div>
      {proposal ? (
        <>
          <div className="status-row">
            <span>授权</span><StatusPill value={authorizationStatus ?? "PENDING"} />
            <span>执行</span><StatusPill value={executionStatus} />
          </div>
          {isMusic && (
            <div className="music-disclosure">
              <strong>{proposedPlaylist?.label ?? "情绪匹配歌单"}</strong>
              <span>批准后：互联网同步 → 本机目录 → 互联网预览 → 本机播放</span>
              <small>远程歌单或预览不可用时，同一授权只回退一次本地舒缓钢琴。</small>
            </div>
          )}
          {compact && showTechnicalDetails ? (
            <details className="chat-evidence">
              <summary>为什么需要确认</summary>
              <p>Step3 只能提出建议；确定性策略创建待授权对象后，仍需你对这个动作单独决定。</p>
              <code className="identifier" data-testid={testId}>{proposal.action_id}</code>
              <pre className="mini-json">{pretty(proposal.payload)}</pre>
            </details>
          ) : !compact ? (
            <details className="audit-technical">
              <summary>查看动作技术详情</summary>
              <code className="identifier" data-testid={testId}>{proposal.action_id}</code>
              <pre className="mini-json">{pretty(proposal.payload)}</pre>
            </details>
          ) : null}
          <div className="button-pair">
            <button disabled={!enabled || busy} onClick={() => onDecision(true)}>
              {compact ? `同意${isMusic ? "音乐" : "空调"}` : "批准此动作"}
            </button>
            <button className="secondary" disabled={!enabled || busy} onClick={() => onDecision(false)}>
              {compact ? `拒绝${isMusic ? "音乐" : "空调"}` : "拒绝"}
            </button>
          </div>
          {resultData && (
            <div className="mock-result">
              <strong>{resultTitle}</strong>
              <span>{resultDetail}</span>
              {isMusic && (
                <details className="audit-technical execution-technical">
                  <summary>查看执行技术详情</summary>
                  <pre className="mini-json">{pretty(resultData)}</pre>
                </details>
              )}
            </div>
          )}
        </>
      ) : (
        <p className="empty-copy">等待后端创建 Action。</p>
      )}
    </article>
  );
}

interface ACDecisionCardProps {
  decision: ConversationACDecision;
  cityName: string;
  proposal: ActionProposal | null;
  persisted?: PersistedAction;
  session: SessionSnapshot;
  enabled: boolean;
  busy: boolean;
  onDecision: (approved: boolean) => void;
}

function ACDecisionCard({
  decision,
  cityName,
  proposal,
  persisted,
  session,
  enabled,
  busy,
  onDecision,
}: ACDecisionCardProps) {
  const recommendsAction = decision.requiresAuthorization;
  const outdoorTemperature = Number.isFinite(decision.outdoor_temperature_c)
    ? `${Number(decision.outdoor_temperature_c.toFixed(1))}°C`
    : "不可用";

  return (
    <section
      className={`ac-decision-card decision-${decision.decision.toLowerCase()}`}
      data-testid="ac-decision"
      aria-label="空调判断"
    >
      <span className="machine-label">空调判断 · {decision.weatherLabel}</span>
      <h3>{decision.outcomeLabel}</h3>
      <div className="ac-decision-facts">
        <span>{cityName}</span>
        <span>室外 {outdoorTemperature}</span>
        <span>已确认 {decision.emotionLabel}</span>
      </div>
      {decision.calculationLabel && <p>{decision.calculationLabel}</p>}
      {decision.decision === "OFF" && <p>当前气温处于舒适范围，不创建空调动作。</p>}
      {decision.decision === "UNAVAILABLE" && (
        <p>没有可用于决策的实时或缓存天气，本轮不会创建空调动作。</p>
      )}
      {recommendsAction && proposal && (
        <ActionCard
          compact
          title="确认空调操作"
          proposal={proposal}
          persisted={persisted}
          session={session}
          enabled={enabled}
          busy={busy}
          onDecision={onDecision}
          testId="ac-action-id"
          showTechnicalDetails={false}
        />
      )}
      {recommendsAction && !proposal && (
        <p className="ac-decision-pending">空调动作尚未创建，当前不能执行。</p>
      )}
    </section>
  );
}

function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [consoleView, setConsoleView] = useState<ConsoleView>("chat");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [textInput, setTextInput] = useState<DemoText>(TEXT_OPTIONS[0]);
  const [livePolling, setLivePolling] = useState(false);
  const [liveHealth, setLiveHealth] = useState<LiveHealth | null>(null);
  const [musicCatalog, setMusicCatalog] = useState<MusicCatalogResponse | null>(null);
  const [selectedMusicCategory, setSelectedMusicCategory] = useState("RELAX");
  const [musicCatalogError, setMusicCatalogError] = useState<string | null>(null);
  const [musicPlaybackStatus, setMusicPlaybackStatus] = useState<string | null>(null);
  const [musicAudioUrl, setMusicAudioUrl] = useState<string | null>(null);
  const [ttsStatus, setTtsStatus] = useState<string | null>(null);
  const [analysisText, setAnalysisText] = useState("");
  const [selectedCityCode, setSelectedCityCode] = useState<CityCode>("310000");
  const [submittedText, setSubmittedText] = useState("");
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const [analysisResult, setAnalysisResult] = useState<TextAnalysisResponse | null>(null);
  const [continuedAnalysisId, setContinuedAnalysisId] = useState<string | null>(null);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const [selectedEmotion, setSelectedEmotion] = useState<TextStateLabel>("OTHER");
  const [preferences, setPreferences] = useState<UserPreferences | null>(null);
  const [summaries, setSummaries] = useState<EmotionSummary[]>([]);
  const [syntheticScenes, setSyntheticScenes] = useState<SyntheticScene[]>([]);
  const [selectedSceneId, setSelectedSceneId] = useState<SyntheticScene["scene_id"]>("indoor_person");
  const [visualBusy, setVisualBusy] = useState(false);
  const [visualResult, setVisualResult] = useState<VisualPerceptionResponse | null>(null);
  const [visualError, setVisualError] = useState<string | null>(null);
  const disconnectRef = useRef<(() => void) | null>(null);
  const liveSessionRef = useRef<string | null>(null);
  const musicPlayerRef = useRef<HTMLAudioElement | null>(null);
  const musicObjectUrlRef = useRef<string | null>(null);

  const clearMusicAudio = useCallback(() => {
    musicPlayerRef.current?.pause();
    if (musicObjectUrlRef.current) {
      URL.revokeObjectURL(musicObjectUrlRef.current);
      musicObjectUrlRef.current = null;
    }
    setMusicAudioUrl(null);
  }, []);

  const refreshMemories = useCallback(async () => {
    dispatch({ type: "memory", value: await api.listMemories() });
  }, []);

  const refreshPersonalization = useCallback(async () => {
    const [preferenceResult, summaryResult] = await Promise.all([
      api.getUserPreferences(),
      api.listEmotionSummaries(),
    ]);
    setPreferences(preferenceResult);
    setSummaries(summaryResult);
  }, []);

  const refreshActions = useCallback(async (snapshot: SessionSnapshot) => {
    const proposals = [snapshot.music_action, snapshot.ac_action].filter(
      (item): item is ActionProposal => Boolean(item),
    );
    await Promise.all(
      proposals.map(async (proposal) => {
        const action = await api.getAction(proposal.action_id);
        dispatch({ type: "action", value: action });
      }),
    );
  }, []);

  const openStream = useCallback((sessionId: string, mode: "mock" | "live" | "analysis" = "mock") => {
    disconnectRef.current?.();
    disconnectRef.current = connectEventStream(
      sessionId,
      (event) => dispatch({ type: "events", value: [event] }),
      (connection) => dispatch({ type: "connection", value: connection }),
      mode,
    );
  }, []);

  const hydrate = useCallback(
    async (snapshot: SessionSnapshot, connect = true) => {
      dispatch({ type: "session", value: snapshot });
      if (snapshot.visual_scene_id === "indoor_person" || snapshot.visual_scene_id === "indoor_empty") {
        setSelectedSceneId(snapshot.visual_scene_id);
      }
      const events = snapshot.runtime_mode === "LIVE"
        ? await api.getLiveEvents(snapshot.session_id)
        : snapshot.runtime_mode === "TEXT_ANALYSIS"
          ? await api.getTextEvents(snapshot.session_id)
          : await api.getEvents(snapshot.session_id);
      dispatch({ type: "events", value: events });
      await refreshActions(snapshot);
      if (connect && snapshot.active) {
        openStream(
          snapshot.session_id,
          snapshot.runtime_mode === "LIVE"
            ? "live"
            : snapshot.runtime_mode === "TEXT_ANALYSIS"
              ? "analysis"
              : "mock",
        );
      }
    },
    [openStream, refreshActions],
  );

  useEffect(() => {
    let cancelled = false;
    const restore = async () => {
      try {
        await refreshMemories();
        await refreshPersonalization();
        const sessionId = sessionStorage.getItem("spark-demo-session-id");
        if (!sessionId) return;
        const mode = sessionStorage.getItem("spark-demo-session-mode");
        const snapshot = mode === "live"
          ? await api.getLiveSession(sessionId)
          : mode === "analysis"
            ? await api.getTextSession(sessionId)
            : await api.getSession(sessionId);
        if (!cancelled) {
          await hydrate(snapshot);
          if (snapshot.runtime_mode === "LIVE" && snapshot.active) {
            liveSessionRef.current = snapshot.session_id;
            setLivePolling(true);
          }
        }
      } catch (restoreError) {
        sessionStorage.removeItem("spark-demo-session-id");
        if (!cancelled && !(restoreError instanceof APIError && restoreError.status === 404)) {
          setError(humanError(restoreError));
        }
      }
    };
    void restore();
    return () => {
      cancelled = true;
      disconnectRef.current?.();
    };
  }, [hydrate, refreshMemories, refreshPersonalization]);

  useEffect(() => {
    let cancelled = false;
    void api.listSyntheticScenes()
      .then((scenes) => {
        if (!cancelled) setSyntheticScenes(scenes);
      })
      .catch((loadError) => {
        if (!cancelled) setVisualError(humanError(loadError));
      });
    void api.liveHealth()
      .then((health) => {
        if (!cancelled) setLiveHealth(health);
      })
      .catch(() => {
        // Scene selection remains available even when a health probe is slow.
      });
    void api.musicCatalog()
      .then((catalog) => {
        if (!cancelled) {
          setMusicCatalog(catalog);
          setMusicCatalogError(null);
        }
      })
      .catch((loadError) => {
        if (!cancelled) setMusicCatalogError(humanError(loadError));
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => () => {
    musicPlayerRef.current?.pause();
    if (musicObjectUrlRef.current) {
      URL.revokeObjectURL(musicObjectUrlRef.current);
      musicObjectUrlRef.current = null;
    }
  }, []);

  const execute = useCallback(async (operation: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await operation();
    } catch (operationError) {
      setError(humanError(operationError));
    } finally {
      setBusy(false);
    }
  }, []);

  const startScenario = (scenario: MockScenario) =>
    execute(async () => {
      disconnectRef.current?.();
      dispatch({ type: "new-session" });
      setAnalysisResult(null);
      setContinuedAnalysisId(null);
      setAnalysisError(null);
      setSubmittedText("");
      setLivePolling(false);
      liveSessionRef.current = null;
      setVisualResult(null);
      setVisualError(null);
      const snapshot = await api.createSession(scenario, selectedCityCode);
      sessionStorage.setItem("spark-demo-session-id", snapshot.session_id);
      sessionStorage.setItem("spark-demo-session-mode", "mock");
      await hydrate(snapshot);
      await refreshMemories();
    });

  const submitText = () => {
    if (!state.session) return;
    void execute(async () => {
      const snapshot = state.session!.runtime_mode === "LIVE"
        ? state.session!.state === "LISTENING"
          ? await api.submitLiveTranscript(state.session!.session_id)
          : await api.clarifyLive(state.session!.session_id, textInput)
        : state.session!.runtime_mode === "TEXT_ANALYSIS"
          ? await api.confirmTextState(state.session!.session_id, selectedEmotion)
          : await api.submitText(state.session!.session_id, textInput);
      await hydrate(snapshot, false);
      await refreshPersonalization();
    });
  };

  const decide = (proposal: ActionProposal | null, approved: boolean) => {
    if (!state.session || !proposal) return;
    void execute(async () => {
      const snapshot = state.session!.runtime_mode === "LIVE"
        ? await api.authorizeLive(state.session!.session_id, proposal.action_id, approved)
        : state.session!.runtime_mode === "TEXT_ANALYSIS"
          ? await api.authorizeText(state.session!.session_id, proposal.action_id, approved)
          : await api.authorize(state.session!.session_id, proposal.action_id, approved);
      await hydrate(snapshot, false);
      const result = snapshot.results[proposal.action_id]?.result;
      if (
        approved &&
        proposal.action_type === "PLAY_MUSIC" &&
        result?.delivery_ready === true
      ) {
        setMusicPlaybackStatus("正在从 Spark 获取已授权音频…");
        const delivery = await api.consumeMusicAudio(
          snapshot.session_id,
          proposal.action_id,
        );
        clearMusicAudio();
        const url = URL.createObjectURL(delivery.audio);
        musicObjectUrlRef.current = url;
        setMusicAudioUrl(url);
        const player = musicPlayerRef.current;
        if (player) {
          player.src = url;
          player.load();
          try {
            await player.play();
            setMusicPlaybackStatus("浏览器已开始播放 Audius 预览");
          } catch {
            setMusicPlaybackStatus("音频已准备，请点击播放器中的播放按钮");
          }
        }
      } else if (!approved && proposal.action_type === "PLAY_MUSIC") {
        setMusicPlaybackStatus("已拒绝，本次未获取或播放音频");
      }
      await refreshPersonalization();
    });
  };

  const resetDemo = () => {
    if (!state.session) return;
    void execute(async () => {
      const snapshot = state.session!.runtime_mode === "LIVE"
        ? await api.resetLive(state.session!.session_id)
        : state.session!.runtime_mode === "TEXT_ANALYSIS"
          ? await api.resetText(state.session!.session_id)
          : await api.reset(state.session!.session_id);
      dispatch({ type: "session", value: snapshot });
      dispatch({
        type: "events",
        value: snapshot.runtime_mode === "LIVE"
          ? await api.getLiveEvents(snapshot.session_id)
          : snapshot.runtime_mode === "TEXT_ANALYSIS"
            ? await api.getTextEvents(snapshot.session_id)
            : await api.getEvents(snapshot.session_id),
      });
      sessionStorage.removeItem("spark-demo-session-id");
      sessionStorage.removeItem("spark-demo-session-mode");
      setAnalysisResult(null);
      setContinuedAnalysisId(null);
      setAnalysisError(null);
      setAnalysisText("");
      setSubmittedText("");
      setTtsStatus(null);
      clearMusicAudio();
      setMusicPlaybackStatus(null);
      setLivePolling(false);
      liveSessionRef.current = null;
      setVisualResult(null);
      setVisualError(null);
      disconnectRef.current?.();
      dispatch({ type: "connection", value: "offline" });
      await refreshPersonalization();
    });
  };

  const confirmMemory = () =>
    execute(async () => {
      await api.confirmMemory();
      await refreshMemories();
    });

  const deleteMemory = (memory: MemoryRecord) =>
    execute(async () => {
      await api.deleteMemory(memory.memory_id);
      await refreshMemories();
    });

  const confirmPreference = (
    key: "reply_style" | "music_preference",
    value: string,
  ) => execute(async () => {
    await api.confirmUserPreference(key, value);
    await refreshPersonalization();
  });

  const deletePreference = (key: "reply_style" | "music_preference") =>
    execute(async () => {
      await api.deleteUserPreference(key);
      await refreshPersonalization();
    });

  const clearSummaries = () => execute(async () => {
    await api.clearEmotionSummaries();
    await refreshPersonalization();
  });

  const analyzeSyntheticScene = async () => {
    setVisualBusy(true);
    setVisualError(null);
    setError(null);
    try {
      const result = await api.analyzeSyntheticScene(selectedSceneId, selectedCityCode);
      setVisualResult(result);
      setLivePolling(false);
      if (result.session) {
        liveSessionRef.current = result.session.active ? result.session.session_id : null;
        if (result.session.active) {
          sessionStorage.setItem("spark-demo-session-id", result.session.session_id);
          sessionStorage.setItem("spark-demo-session-mode", "live");
        } else {
          sessionStorage.removeItem("spark-demo-session-id");
          sessionStorage.removeItem("spark-demo-session-mode");
        }
        await hydrate(result.session, result.session.active);
      }
    } catch (visualFailure) {
      setVisualError(humanError(visualFailure));
    } finally {
      setVisualBusy(false);
    }
  };

  useEffect(() => {
    if (!livePolling) return;
    let cancelled = false;
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try {
        const result = await api.pollLive(selectedCityCode);
        if (cancelled || !result.session) return;
        const isNew = liveSessionRef.current !== result.session.session_id;
        liveSessionRef.current = result.session.session_id;
        sessionStorage.setItem("spark-demo-session-id", result.session.session_id);
        sessionStorage.setItem("spark-demo-session-mode", "live");
        await hydrate(result.session, isNew);
      } catch (pollError) {
        if (!cancelled) setError(humanError(pollError));
      } finally {
        running = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [hydrate, livePolling, selectedCityCode]);

  const requestTTS = () => {
    if (!state.session) return;
    void execute(async () => {
      const result = state.session!.runtime_mode === "TEXT_ANALYSIS"
        ? await api.synthesizeText(state.session!.session_id)
        : await api.synthesizeLive(state.session!.session_id);
      if ("audio" in result && result.audio) {
        const url = URL.createObjectURL(result.audio);
        const audio = new Audio(url);
        audio.addEventListener("ended", () => URL.revokeObjectURL(url), { once: true });
        await audio.play();
      }
      setTtsStatus(`${result.status} · ${result.latency_ms} ms`);
    });
  };

  const analyzeTextState = async () => {
    const text = analysisText.trim();
    if (!text) return;
    setSubmittedText(text);
    setAnalysisBusy(true);
    setAnalysisError(null);
    setAnalysisResult(null);
    setContinuedAnalysisId(null);
    try {
      setAnalysisResult(await api.analyzeText(text, selectedCityCode));
    } catch (analysisFailure) {
      setAnalysisError(humanError(analysisFailure));
    } finally {
      setAnalysisBusy(false);
    }
  };

  const continueTextAnalysis = async () => {
    if (!analysisResult) return;
    setAnalysisBusy(true);
    setAnalysisError(null);
    try {
      disconnectRef.current?.();
      dispatch({ type: "new-session" });
      const snapshot = await api.continueTextAnalysis(analysisResult.analysis_id);
      setContinuedAnalysisId(analysisResult.analysis_id);
      setSelectedEmotion(analysisResult.primary_state.label);
      setTextInput(
        CLARIFICATION_BY_ANALYSIS[analysisResult.primary_state.label] ?? "其他原因",
      );
      sessionStorage.setItem("spark-demo-session-id", snapshot.session_id);
      sessionStorage.setItem("spark-demo-session-mode", "analysis");
      await hydrate(snapshot);
      window.setTimeout(() => {
        const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        document.getElementById(consoleView === "chat" ? "chat-latest" : "decision-panel")
          ?.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "nearest" });
      }, 0);
    } catch (continuationFailure) {
      setAnalysisError(humanError(continuationFailure));
    } finally {
      setAnalysisBusy(false);
    }
  };

  const session = state.session;
  const visualPerception = visualResult?.observation ?? session?.visual_perception ?? null;
  const visualSceneId = visualResult?.observation.scene_id ?? session?.visual_scene_id ?? selectedSceneId;
  const selectedScene = syntheticScenes.find((item) => item.scene_id === visualSceneId)
    ?? syntheticScenes.find((item) => item.scene_id === selectedSceneId);
  const visualLatency = visualResult?.observation.latency_ms
    ?? session?.component_health.STEP3_VISION?.latency_ms;
  const displayedPersonPresent = visualPerception?.person_present ?? session?.person_present ?? false;
  const isVisualImageSession = session?.perception_source === "SYNTHETIC_IMAGE"
    || session?.perception_source === "DEMO_IMAGE";
  const stateText = session?.runtime_mode === "LIVE"
    && isVisualImageSession
    && session.state === "LISTENING"
    ? "视觉感知已完成，等待固定演示文本"
    : session ? STATE_COPY[session.state] ?? session.state : undefined;
  const weatherTransition = transitionTo(state, "CONTEXT_READY");
  const weather = asRecord(
    session?.weather_context ?? asRecord(asRecord(weatherTransition?.payload).details).weather,
  );
  const policyEvent = latestEvent(state, "POLICY_CLARIFICATION_DECISION");
  const policy = asRecord(policyEvent?.payload);
  const outboundEvent = latestEventOfTypes(state, [
    "INTERNET_REQUEST",
    "INTERNET_MOCK_REQUEST",
    "TEXT_WEATHER_CONTEXT",
  ]);
  const outbound = asRecord(asRecord(outboundEvent?.payload).outbound_payload);
  const rejectedInternetEvent = latestEvent(state, "INTERNET_REQUEST_REJECTED");
  const localActionEvent = latestEvent(state, "LOCAL_ACTION_REQUEST");
  const localOutbound = asRecord(asRecord(localActionEvent?.payload).outbound_payload);
  const privacyTransition = transitionTo(state, "PRIVACY_REJECTED");
  const privacyDetails = asRecord(asRecord(privacyTransition?.payload).details);
  const rejectedDetails = asRecord(rejectedInternetEvent?.payload);
  const rawViolationPaths = privacyDetails.violation_paths ?? rejectedDetails.violation_paths;
  const violationPaths = Array.isArray(rawViolationPaths)
    ? rawViolationPaths.map(String)
    : [];
  const musicPersistent = session?.music_action
    ? state.actions[session.music_action.action_id]
    : undefined;
  const acPersistent = session?.ac_action
    ? state.actions[session.ac_action.action_id]
    : undefined;
  const proposedMusicCategory = session?.music_action
    ? MUSIC_PLAYLISTS[String(session.music_action.payload.track_id)]?.key
    : undefined;
  const displayedMusicCategory = musicCatalog?.categories.find(
    (category) => category.key === (proposedMusicCategory ?? selectedMusicCategory),
  );
  const connectionLabel = {
    idle: "未连接",
    connecting: "连接中",
    live: "实时",
    offline: "离线",
  }[state.connection];
  const canSubmitText = Boolean(
    session?.active &&
      (session.state === "CLARIFICATION_REQUIRED" ||
        (session.runtime_mode === "LIVE" && session.state === "LISTENING")),
  );

  const orderedHypotheses = useMemo(
    () =>
      [...(
        session?.text_analysis?.state_hypotheses ??
        session?.step3_output?.state_hypotheses ??
        []
      )].sort(
        (left, right) => right.confidence - left.confidence,
      ),
    [session?.step3_output?.state_hypotheses, session?.text_analysis?.state_hypotheses],
  );

  const conversation = useMemo(
    () => buildConversationProjection({
      analysisResult,
      session,
      submittedText,
      stateText,
    }),
    [analysisResult, session, stateText, submittedText],
  );

  const activeReaction = session?.final_reaction ?? session?.provisional_reaction;
  const privacyStatus = violationPaths.length || rejectedInternetEvent
    ? "REJECTED"
    : outboundEvent
      ? "PASSED"
      : "WAITING";
  const hasConversationContent = Boolean(
    conversation.messages.length ||
      (analysisResult && analysisResult.analysis_id !== continuedAnalysisId) ||
      (conversation.needsClarification && session) ||
      (session?.runtime_mode === "LIVE" && session.state === "LISTENING") ||
      session?.music_action ||
      session?.ac_action ||
      session?.ac_decision ||
      (session && activeReaction),
  );

  const switchConsoleView = (nextView: ConsoleView) => {
    setConsoleView(nextView);
    window.requestAnimationFrame(() => {
      document.getElementById(`console-tab-${nextView}`)?.focus({ preventScroll: true });
    });
  };

  return (
    <main className={`app-shell cinder-shell view-${consoleView}`}>
      <nav className="cinder-nav" aria-label="控制台视图">
        <div
          className="view-tabs"
          role="tablist"
          aria-label="控制台模式"
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const nextView = event.key === "Home"
              ? "chat"
              : event.key === "End"
                ? "audit"
                : consoleView === "chat" ? "audit" : "chat";
            switchConsoleView(nextView);
          }}
        >
          <button
            id="console-tab-chat"
            className={consoleView === "chat" ? "is-active" : ""}
            role="tab"
            aria-selected={consoleView === "chat"}
            aria-controls="chat-panel"
            tabIndex={consoleView === "chat" ? 0 : -1}
            onClick={() => switchConsoleView("chat")}
          >
            对话
          </button>
          <button
            id="console-tab-audit"
            className={consoleView === "audit" ? "is-active" : ""}
            role="tab"
            aria-selected={consoleView === "audit"}
            aria-controls="audit-panel"
            tabIndex={consoleView === "audit" ? 0 : -1}
            onClick={() => switchConsoleView("audit")}
          >
            审计
          </button>
        </div>
        <span className={`cinder-connection connection-${state.connection}`}>
          <i aria-hidden="true" />
          <span>{connectionLabel}</span>
        </span>
      </nav>

      {consoleView === "chat" && (
        <section
          id="chat-panel"
          className="chat-layout"
          role="tabpanel"
          aria-labelledby="console-tab-chat"
        >
          <div className="chat-primary">
            <header className="chat-intro">
              <div className="cinder-apparatus" aria-hidden="true">
                <span className="apparatus-wall" />
                <span className="apparatus-electrode apparatus-electrode-a" />
                <span className="apparatus-electrode apparatus-electrode-b" />
                <span className="apparatus-filament" />
              </div>
              <div>
                <span className="machine-label">文本分析</span>
                <h1>输入一段演示文本</h1>
                <p>AI 负责解释状态，确定性策略负责澄清与授权。任何环境动作都由你最后决定。</p>
              </div>
              <div className="chat-state-readout" aria-live="polite">
                <span className="machine-label">编排状态</span>
                <strong data-testid="state">{displayStatus(conversation.stateLabel)}</strong>
              </div>
            </header>

            <section className="visual-demo" aria-labelledby="visual-demo-heading">
              <div className="visual-demo-copy">
                <span className="machine-label">视觉感知 · 演示照片</span>
                <h2 id="visual-demo-heading">让 DGX 上的 Step3-VL 看一眼</h2>
                <p>选择一张演示照片。照片会在 DGX 内网进行真实推理，不使用摄像头，也不会写入本地数据库。</p>
              </div>
              <div className="visual-scene-options" role="radiogroup" aria-label="演示照片">
                {syntheticScenes.map((scene) => (
                  <button
                    key={scene.scene_id}
                    className={`${selectedSceneId === scene.scene_id ? "is-selected" : ""} ${scene.synthetic ? "is-synthetic" : "is-photo"}`}
                    role="radio"
                    aria-checked={selectedSceneId === scene.scene_id}
                    disabled={visualBusy}
                    onClick={() => {
                      setSelectedSceneId(scene.scene_id);
                      setVisualResult(null);
                      setVisualError(null);
                    }}
                  >
                    <img src={scene.image_url} alt="" />
                    <span>{scene.label}</span>
                  </button>
                ))}
              </div>
              {visualPerception && (
                <div className="visual-result" data-testid="visual-result">
                  <strong>{visualPerception.person_present ? "检测到人物" : "没有检测到人物"}</strong>
                  <span>{displayStatus(visualPerception.scene_type)} · {visualPerception.scene_summary}</span>
                  <small>
                    模型自报置信度 {Math.round(visualPerception.confidence * 100)}%
                    {visualLatency === undefined ? "" : ` · ${visualLatency} 毫秒`}
                  </small>
                </div>
              )}
              {visualError && (
                <div className="visual-error" role="alert">
                  视觉模型本次未完成：{visualError}。没有使用预设答案触发会话。
                </div>
              )}
              <div className="visual-demo-action">
                <button
                  className="primary live-button"
                  data-testid="analyze-visual-scene"
                  data-state={visualBusy ? "loading" : visualError ? "error" : visualPerception ? "success" : "default"}
                  disabled={visualBusy || syntheticScenes.length === 0}
                  onClick={() => void analyzeSyntheticScene()}
                >
                  {visualBusy ? "Step3-VL 识别中…" : visualError ? "重新识别" : "分析所选照片"}
                </button>
                <small>识别到人物后只建立会话并等待固定文本，不会自动执行任何动作。</small>
              </div>
            </section>

            {(error || analysisError) && (
              <div className="chat-error" role="alert" data-state="error">
                <strong>本次请求没有完成。</strong>
                <span>{analysisError ? `模型分析不可用：${analysisError}。未使用规则兜底。` : error}</span>
              </div>
            )}

            <section
              className="chat-thread"
              aria-label="AI 对话"
              aria-live="polite"
              hidden={!hasConversationContent}
            >
              {conversation.messages.map((message, index) => (
                <Fragment key={message.id}>
                  <article
                    className={`chat-message role-${message.role} phase-${message.phase}`}
                    id={index === conversation.messages.length - 1 ? "chat-latest" : undefined}
                  >
                  <span className="machine-label">{message.label}</span>
                  <p data-testid={
                    index === conversation.messages.length - 1 &&
                    (message.phase === "final" || message.phase === "result")
                      ? "system-response"
                      : undefined
                  }>
                    {message.text}
                  </p>
                  {message.question && <blockquote>{message.question}</blockquote>}
                  {message.privacyPlaceholder && (
                    <small>隐私保护：原始文本没有进入会话快照或 SQLite。</small>
                  )}
                  {(message.meta?.length || message.candidates?.length || message.reasons?.length || message.suggestions?.length) ? (
                    <details className="chat-evidence">
                      <summary>查看判断依据</summary>
                      {message.meta?.length ? (
                        <div className="evidence-meta">
                          {message.meta.map((item) => <code key={item}>{item}</code>)}
                        </div>
                      ) : null}
                      {message.candidates?.map((candidate) => (
                        <div className="evidence-candidate" key={candidate.label}>
                          <span>{candidate.displayName}</span>
                          <strong>{Math.round(candidate.confidence * 100)}%</strong>
                          <small>{candidate.evidence.join(" · ") || "模型未提供可展示证据"}</small>
                        </div>
                      ))}
                      {message.reasons?.map((reason) => <p key={reason}>{reason}</p>)}
                      {message.suggestions?.length ? <code>{message.suggestions.join(" + ")}</code> : null}
                    </details>
                  ) : null}
                  </article>
                  {message.phase === "final" && conversation.acDecision && session && (
                    <ACDecisionCard
                      decision={conversation.acDecision}
                      cityName={CITY_NAMES[session.weather_context?.city_code ?? selectedCityCode]}
                      proposal={session.ac_action}
                      persisted={acPersistent}
                      session={session}
                      enabled={actionButtonsEnabled(session, "ac")}
                      busy={busy}
                      onDecision={(approved) => decide(session.ac_action, approved)}
                    />
                  )}
                </Fragment>
              ))}

              {analysisResult && analysisResult.analysis_id !== continuedAnalysisId && (
                <div className="chat-inline-action">
                  <button
                    className="primary"
                    data-state={analysisBusy ? "loading" : "default"}
                    disabled={analysisBusy}
                    onClick={() => void continueTextAnalysis()}
                  >
                    {analysisBusy ? "正在建立会话…" : "继续这次对话"}
                  </button>
                  <small>继续时只使用 analysis_id，不重新发送原始文本。</small>
                </div>
              )}

              {conversation.needsClarification && session && (
                <article className="chat-clarification" aria-labelledby="clarification-heading">
                  <span className="machine-label">确定性策略 · 请你确认</span>
                  <h2 id="clarification-heading">哪一种更接近你现在的状态？</h2>
                  <div className="clarification-options">
                    {session.runtime_mode === "TEXT_ANALYSIS"
                      ? EMOTION_OPTIONS.map((option) => (
                        <button
                          key={option}
                          className={selectedEmotion === option ? "is-selected" : ""}
                          aria-pressed={selectedEmotion === option}
                          disabled={busy}
                          onClick={() => setSelectedEmotion(option)}
                        >
                          {LABELS[option] ?? option}
                        </button>
                      ))
                      : TEXT_OPTIONS.map((option) => (
                        <button
                          key={option}
                          className={textInput === option ? "is-selected" : ""}
                          aria-pressed={textInput === option}
                          disabled={busy}
                          onClick={() => setTextInput(option)}
                        >
                          {option}
                        </button>
                      ))}
                  </div>
                  <button
                    className="primary confirm-state"
                    data-state={busy ? "loading" : "default"}
                    disabled={!canSubmitText || busy}
                    onClick={submitText}
                  >
                    {busy ? "正在确认…" : "确认状态"}
                  </button>
                  <small>确认后，确定性策略才能创建音乐和空调的待授权动作。</small>
                </article>
              )}

              {session?.runtime_mode === "LIVE" && session.state === "LISTENING" && (
                <article className="chat-clarification">
                  <span className="machine-label">
                    {isVisualImageSession ? "视觉感知 · 文本入口" : "语音服务 · 降级入口"}
                  </span>
                  <h2>{isVisualImageSession ? "视觉感知已确认有人" : "继续真实链路"}</h2>
                  <p>可提交固定演示文本，不进行自由文本规则推理。</p>
                  <button className="primary" disabled={!canSubmitText || busy} onClick={submitText}>
                    {busy ? "正在提交…" : "使用固定文字继续"}
                  </button>
                </article>
              )}

              {session?.music_action && (
                <ActionCard
                  compact
                  title="音乐建议"
                  proposal={session.music_action}
                  persisted={musicPersistent}
                  session={session}
                  enabled={actionButtonsEnabled(session, "music")}
                  busy={busy}
                  onDecision={(approved) => decide(session.music_action, approved)}
                  testId="music-action-id"
                />
              )}
              {session?.ac_action && !conversation.acDecision && (
                <ActionCard
                  compact
                  title="空调建议"
                  proposal={session.ac_action}
                  persisted={acPersistent}
                  session={session}
                  enabled={actionButtonsEnabled(session, "ac")}
                  busy={busy}
                  onDecision={(approved) => decide(session.ac_action, approved)}
                  testId="ac-action-id"
                />
              )}

              {session && activeReaction && (
                <div className="chat-inline-action tts-inline">
                  <button className="secondary" disabled={busy} onClick={requestTTS}>
                    {busy ? "正在生成语音…" : "播放这段回复"}
                  </button>
                  <small>{ttsStatus ?? "语音不可用时保留文字，不伪造播放成功。"}</small>
                </div>
              )}
            </section>

            <section className="chat-composer" aria-labelledby="composer-label">
              <div className="city-selector">
                <label htmlFor="chat-weather-city">天气城市</label>
                <select
                  id="chat-weather-city"
                  value={selectedCityCode}
                  disabled={analysisBusy}
                  onChange={(event) => setSelectedCityCode(event.target.value as CityCode)}
                >
                  {CITY_OPTIONS.map((city) => (
                    <option key={city.code} value={city.code}>{city.name}</option>
                  ))}
                </select>
                <small>仅发送所选城市码，不获取精确位置。</small>
              </div>
              <label id="composer-label" htmlFor="chat-analysis-text">演示文本</label>
              <textarea
                id="chat-analysis-text"
                maxLength={500}
                rows={3}
                value={analysisText}
                placeholder="例如：今天项目终于完成了，我特别开心。"
                aria-describedby="composer-helper"
                aria-invalid={Boolean(analysisError)}
                disabled={analysisBusy}
                onChange={(event) => setAnalysisText(event.target.value)}
              />
              <div className="composer-footer">
                <small id="composer-helper">原文只用于本次分析，不写入 SQLite，也不会直接触发动作。{` ${analysisText.length}/500`}</small>
                <button
                  className="primary"
                  data-state={analysisBusy ? "loading" : analysisError ? "error" : analysisResult ? "success" : "default"}
                  disabled={analysisBusy || !analysisText.trim()}
                  onClick={() => void analyzeTextState()}
                >
                  {analysisBusy ? "Step3 分析中…" : analysisError ? "重新分析" : "分析并发送"}
                </button>
              </div>
            </section>

            <details className="scenario-drawer">
              <summary>演示场景</summary>
              <div className="scenario-drawer-content">
                <button disabled={busy} onClick={() => void startScenario("success")}>模拟人物回家</button>
                <button className="danger" disabled={!session?.active || busy} onClick={resetDemo}>重置本轮</button>
              </div>
            </details>
          </div>

          <aside className="chat-context" aria-label="本轮上下文">
            <section>
              <span className="machine-label">会话</span>
              <h2>本轮状态</h2>
              <dl>
                <div><dt>状态</dt><dd>{displayStatus(session?.state ?? "READY")}</dd></div>
                <div><dt>模式</dt><dd>{displayStatus(session?.runtime_mode ?? "TEXT FIRST")}</dd></div>
                <div><dt>会话</dt><dd><code data-testid="session-id">{session?.session_id ?? "尚未创建"}</code></dd></div>
              </dl>
            </section>
            <section>
              <span className="machine-label">环境上下文</span>
              <h2>环境</h2>
              <p className="context-temperature">
                {weather.temperature_c === undefined ? "—" : `${Number(weather.temperature_c).toFixed(1)}°`}
              </p>
              <p>{String(weather.condition ?? "等待天气上下文")}</p>
              <small>
                城市 {CITY_NAMES[(weather.city_code ?? selectedCityCode) as CityCode]}
              </small>
              <small>来源 {displayStatus(String(weather.source ?? "WAITING"))}</small>
            </section>
            <section>
              <span className="machine-label">隐私边界</span>
              <h2>隐私边界</h2>
              <div className="context-status"><StatusPill value={privacyStatus} /></div>
              <p>文本原文不写入 SQLite；所有互联网数据必须先通过隐私网关。</p>
              <div className="context-scopes">
                <ScopeBadge scope="LOCAL" />
                <ScopeBadge scope="LAN" />
                <ScopeBadge scope="INTERNET" />
              </div>
              {violationPaths.length > 0 && <code>{violationPaths.join(" · ")}</code>}
            </section>
            <section className="context-boundary">
              <span className="machine-label">职责边界</span>
              <p>Step3 建议<br />确定性策略决策<br />用户授权<br />执行结果</p>
            </section>
          </aside>
        </section>
      )}

      {consoleView === "audit" && (
        <div
          id="audit-panel"
          className="audit-view"
          role="tabpanel"
          aria-labelledby="console-tab-audit"
        >
      <header className="topbar">
        <div className="brand-lockup">
          <div>
            <span className="eyebrow">DGX Spark · 决策与隐私</span>
            <h1>决策与隐私控制台</h1>
          </div>
        </div>
        <div className="header-status">
          <span className="pure-mock">
            {session?.runtime_mode === "LIVE"
              ? "实时天气 + 本机音乐 · 空调模拟"
              : session?.runtime_mode === "TEXT_ANALYSIS"
                ? "Step3 文本分析 · 显式确认 · 动作模拟"
              : "纯模拟 · 无物理动作"}
          </span>
          <span className={`connection connection-${state.connection}`}>
            <i /> 实时连接 {connectionLabel}
          </span>
          <StatusPill value={session?.state ?? "NO_SESSION"} muted={!session} />
        </div>
      </header>

      <section className="analysis-panel" aria-labelledby="text-analysis-title">
        <div className="analysis-copy">
          <span className="eyebrow">实验性功能 · DGX Spark Step3</span>
          <h2 id="text-analysis-title">实验性文本状态分析</h2>
          <p>输入一段短文本，由 DGX Spark 上的 Step3 模型返回候选状态；确认结果后可显式进入陪伴演示的交互与判断区。</p>
          <strong>实验性结果，不构成心理或医疗判断。</strong>
        </div>
        <div className="analysis-workspace">
          <label htmlFor="analysis-weather-city">天气城市</label>
          <select
            id="analysis-weather-city"
            value={selectedCityCode}
            disabled={analysisBusy}
            onChange={(event) => setSelectedCityCode(event.target.value as CityCode)}
          >
            {CITY_OPTIONS.map((city) => (
              <option key={city.code} value={city.code}>{city.name}</option>
            ))}
          </select>
          <small>仅发送所选城市码，不获取精确位置。</small>
          <label htmlFor="analysis-text">用户文本</label>
          <textarea
            id="analysis-text"
            maxLength={500}
            rows={4}
            value={analysisText}
            placeholder="例如：今天项目终于完成了，我特别开心。"
            aria-invalid={Boolean(analysisError)}
            disabled={analysisBusy}
            onChange={(event) => setAnalysisText(event.target.value)}
          />
          <div className="analysis-controls">
            <small>{analysisText.length} / 500</small>
            <button
              className="primary"
              disabled={analysisBusy || !analysisText.trim()}
              onClick={() => void analyzeTextState()}
            >
              {analysisBusy ? "Step3 分析中…" : "分析状态"}
            </button>
          </div>
          {analysisError && (
            <div className="analysis-error" role="alert">
              模型分析不可用：{analysisError}。未使用规则兜底。
            </div>
          )}
        </div>
        <div className="analysis-results" aria-live="polite">
          {analysisResult ? (
            <>
              <div className="analysis-primary">
                <span>主要候选</span>
                <strong>{analysisResult.primary_state.display_name}</strong>
                <b>{Math.round(analysisResult.primary_state.confidence * 100)}%</b>
              </div>
              <div className="analysis-candidates">
                {analysisResult.candidates.map((candidate) => (
                  <article key={candidate.label}>
                    <div>
                      <strong>{candidate.display_name}</strong>
                      <span>{Math.round(candidate.confidence * 100)}%</span>
                    </div>
                    {candidate.evidence.length > 0 && (
                      <ul>{candidate.evidence.map((item) => <li key={item}>{item}</li>)}</ul>
                    )}
                  </article>
                ))}
              </div>
              <small>模型自报置信度（未校准） · {analysisResult.latency_ms} 毫秒 · 本机处理</small>
              <article className="reaction-card provisional-reaction">
                <span className="section-label">Step3 初步回复</span>
                <p>{analysisResult.reaction.reply_text}</p>
                {analysisResult.reaction.follow_up_question && (
                  <blockquote>{analysisResult.reaction.follow_up_question}</blockquote>
                )}
                <small>
                  语气 {displayStatus(analysisResult.reaction.tone)} · 室外天气 {displayStatus(analysisResult.weather_context.source)}
                  {` · ${analysisResult.weather_context.temperature_c.toFixed(1)}°C`}
                </small>
                <ul>
                  {analysisResult.reaction.reasons.map((reason) => <li key={reason}>{reason}</li>)}
                </ul>
                <span className="reaction-suggestions">
                  建议：{analysisResult.reaction.suggestions.map((item) => LABELS[item.type] ?? item.type).join("、") || "暂无"}
                </span>
              </article>
              <details className="audit-technical analysis-technical">
                <summary>查看模型技术详情</summary>
                <pre className="mini-json">{pretty({
                  primary_label: analysisResult.primary_state.label,
                  candidate_labels: analysisResult.candidates.map((candidate) => candidate.label),
                  suggestion_types: analysisResult.reaction.suggestions.map((item) => item.type),
                })}</pre>
              </details>
              <button
                className="primary analysis-continue"
                disabled={analysisBusy}
                onClick={() => void continueTextAnalysis()}
              >
                {analysisBusy ? "正在进入交互…" : "使用此结果进入交互与判断"}
              </button>
            </>
          ) : (
            <p className="analysis-placeholder">尚未分析。文本不会写入 SQLite，也不会触发动作。</p>
          )}
        </div>
      </section>

      <section className="command-bar" aria-label="演示场景控制">
        <div className="scenario-buttons">
          <button
            className="live-button"
            data-testid="audit-analyze-visual-scene"
            disabled={visualBusy || syntheticScenes.length === 0}
            onClick={() => void analyzeSyntheticScene()}
          >
            {visualBusy ? "视觉识别中…" : "分析所选照片"}
          </button>
          {SCENARIOS.map((item) => (
            <button
              key={item.scenario}
              className={item.tone === "primary" ? "primary" : "secondary"}
              disabled={busy}
              onClick={() => void startScenario(item.scenario)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="session-meta">
          <span>会话</span>
          <code data-testid="session-id">{session?.session_id ?? "—"}</code>
          <button className="danger" disabled={!session?.active || busy} onClick={resetDemo}>
            重置当前演示
          </button>
        </div>
      </section>

      {error && <div className="error-banner" role="alert">{error}</div>}

      <section className="dashboard-grid">
        <section className="panel perception-panel">
          <div className="panel-title">
            <div><span className="eyebrow">01 · 感知</span><h2>感知区</h2></div>
            <StatusPill value={visualResult?.observation.perception_source ?? session?.perception_source ?? "MOCK"} />
          </div>
          <div
            className={`mock-scene ${displayedPersonPresent ? "person-present" : ""} ${selectedScene ? "has-synthetic-image" : ""} ${selectedScene && !selectedScene.synthetic ? "has-photo" : ""}`}
            data-testid="mock-scene"
          >
            {selectedScene
              ? <img className="synthetic-scene-image" src={selectedScene.image_url} alt={selectedScene.label} />
              : <div className="scene-grid" />}
            <div className="room-label">
              {visualPerception
                ? `${selectedScene?.label ?? "内置场景"} · 合成图片`
                : session?.perception_source === "TEXT_ONLY"
                ? "文本输入 · 未使用摄像头或原始媒体"
                : session?.perception_source === "CAMERA"
                  ? "摄像头元数据 · 不保存原始画面"
                  : session?.perception_source === "VIDEO"
                    ? "已批准演示视频 · 不保存原始画面"
                    : "客厅 · 合成静态场景"}
            </div>
            {!selectedScene && <div className="person-shape"><span /><i /></div>}
            {visualPerception?.person_present && (
              <div className="detection-box">
                <span>人物 · 模型 {Math.round(visualPerception.confidence * 100)}%</span><i className="corner c1" /><i className="corner c2" />
                <i className="corner c3" /><i className="corner c4" />
              </div>
            )}
            <div className="scene-footer">
              <span>{displayedPersonPresent ? "● 检测到人物" : "○ 未检测到人物"}</span>
              <span>
                {visualPerception
                  ? `Step3-VL · DGX 本地推理${visualLatency === undefined ? "" : ` · ${visualLatency} 毫秒`}`
                  : session?.runtime_mode === "TEXT_ANALYSIS"
                  ? "仅文本 · 不声称检测到人物"
                  : session?.runtime_mode === "LIVE"
                    ? `${displayStatus(session.perception_source)} · 本地适配器`
                    : "摄像头离线 · 模拟已启用"}
              </span>
            </div>
          </div>
          <div className="agent-list">
            <div><span>视觉代理</span><strong>{visualPerception ? "识别完成" : displayStatus(session?.perception_source === "TEXT_ONLY" ? "NOT_USED" : session ? "MOCK_READY" : "IDLE")}</strong></div>
            <div><span>人物事件</span><strong>{displayStatus(session?.perception_source === "TEXT_ONLY" ? "NOT_APPLICABLE" : displayedPersonPresent ? "PERSON_DETECTED" : session && !session.active ? "PERSON_LEFT" : "NONE")}</strong></div>
            <div><span>Step3-VL</span><strong>{visualPerception ? "真实结构化输出" : displayStatus(session?.model_source ?? (session?.step3_output ? "STRUCTURED_OUTPUT" : "IDLE"))}</strong></div>
            <div><span>交互来源</span><strong>{displayStatus(session?.interaction_source ?? "NONE")}</strong></div>
          </div>
          {(session?.runtime_mode === "LIVE" || liveHealth) && (
            <div className="live-health" data-testid="live-health">
              <span className="section-label">适配器健康状态 · 健康检查不访问互联网</span>
              {liveHealth?.deployment && (
                <div>
                  <strong>部署</strong>
                  <span>
                    {displayStatus(liveHealth.deployment.backend)} · {displayStatus(liveHealth.deployment.console_access)}
                  </span>
                </div>
              )}
              {Object.entries(session?.component_health ?? {}).map(([component, value]) => (
                <div key={component}>
                  <strong>{displayComponent(component)}</strong>
                  <span>{displayStatus(value.status)} · {value.latency_ms} 毫秒</span>
                </div>
              ))}
              {liveHealth?.components.map((component) => (
                <div key={component.component}>
                  <strong>{displayComponent(component.component)}</strong>
                  <span>{displayStatus(component.status)} · {component.latency_ms} 毫秒</span>
                </div>
              ))}
              {session?.degraded_reasons.map((reason) => <code key={reason}>{reason}</code>)}
            </div>
          )}
        </section>

        <section className="panel decision-panel" id="decision-panel">
          <div className="panel-title">
            <div><span className="eyebrow">02 · 判断</span><h2>交互与判断</h2></div>
            <span className="state-code" data-testid="state">{displayStatus(session?.state ?? "NO_SESSION")}</span>
          </div>

          <div className="transcript-block">
            <span className="section-label">
              {session?.runtime_mode === "TEXT_ANALYSIS" ? "文本 · 原文未保存" : "语音识别 · 模拟转写"}
            </span>
            <p>
              {session?.runtime_mode === "TEXT_ANALYSIS"
                ? "原始分析文本未写入会话快照或 SQLite。"
                : session?.transcript ?? "等待合成输入。"}
            </p>
            {session?.last_text_input && <blockquote>文本澄清：{session.last_text_input}</blockquote>}
          </div>

          <div className="clarification-control">
            <label htmlFor="clarification-text">文本输入备用入口</label>
            {session?.step3_output?.clarification_candidates?.[0] && (
              <p className="clarification-question">
                {session.step3_output.clarification_candidates[0].question}
              </p>
            )}
            <div className="input-row">
              <select
                id="clarification-text"
                value={session?.runtime_mode === "TEXT_ANALYSIS" ? selectedEmotion : textInput}
                disabled={session?.state !== "CLARIFICATION_REQUIRED" || busy}
                onChange={(event) => {
                  if (session?.runtime_mode === "TEXT_ANALYSIS") {
                    setSelectedEmotion(event.target.value as TextStateLabel);
                  } else {
                    setTextInput(event.target.value as DemoText);
                  }
                }}
              >
                {session?.runtime_mode === "TEXT_ANALYSIS"
                  ? EMOTION_OPTIONS.map((option) => (
                      <option key={option} value={option}>{LABELS[option] ?? option}</option>
                    ))
                  : TEXT_OPTIONS.map((option) => <option key={option}>{option}</option>)}
              </select>
              <button
                disabled={!canSubmitText || busy}
                onClick={submitText}
              >
                {session?.runtime_mode === "LIVE" && session.state === "LISTENING"
                  ? "使用固定文字继续"
                  : "提交澄清"}
              </button>
            </div>
            <small>
              {session?.runtime_mode === "TEXT_ANALYSIS"
                ? "分析候选已带入；必须使用固定选项显式确认后，确定性策略才能创建待授权动作。"
                : "仅接受固定演示选项；不进行自由文本模型推理。"}
            </small>
          </div>

          <div className="reasoning-stages">
            <article>
              <span className="stage-number">A</span>
              <div className="stage-content">
                <div className="stage-title"><strong>Step3 结构化输出</strong><span>模型不授权</span></div>
                {orderedHypotheses.length ? orderedHypotheses.map((item) => (
                  <div className="hypothesis" key={item.label}>
                    <div><span>{LABELS[item.label] ?? item.label}</span><strong>{Math.round(item.confidence * 100)}%</strong></div>
                    <div className="confidence-track"><i style={{ width: `${item.confidence * 100}%` }} /></div>
                  </div>
                )) : <p className="empty-copy">暂无候选状态。</p>}
                {session?.step3_output?.recommendation_reason?.length ? (
                  <div className="recommendation-reasons">
                    <span className="section-label">Step3 推荐理由 · 仅供确定性策略参考</span>
                    {session.step3_output.recommendation_reason.map((reason) => (
                      <p key={reason.code}>{reason.text}</p>
                    ))}
                    <details className="audit-technical">
                      <summary>查看理由代码</summary>
                      <pre className="mini-json">{pretty(session.step3_output.recommendation_reason)}</pre>
                    </details>
                  </div>
                ) : null}
              </div>
            </article>
            <article>
              <span className="stage-number policy">B</span>
              <div className="stage-content">
                <div className="stage-title"><strong>确定性策略决策</strong><span>确定性阈值</span></div>
                {policyEvent ? (
                  <div className="policy-grid">
                    <span>最高置信度 <b>{formatMetric(policy.top_confidence)}</b></span>
                    <span>候选差值 <b>{formatMetric(policy.confidence_gap)}</b></span>
                    <span>澄清状态 <b>{typeof policy.required === "boolean" ? (policy.required ? "需要澄清" : "无需澄清") : session?.clarification_answer ? "已完成" : "—"}</b></span>
                  </div>
                ) : <p className="empty-copy">等待策略事件。</p>}
              </div>
            </article>
            <article>
              <span className="stage-number user">C</span>
              <div className="stage-content">
                {session?.confirmed_emotion && (
                  <p>已确认状态：{LABELS[session.confirmed_emotion] ?? session.confirmed_emotion}</p>
                )}
                <div className="stage-title"><strong>用户授权</strong><span>动作编号逐一确认</span></div>
                <p>{session?.clarification_answer ? `澄清标签：${LABELS[session.clarification_answer] ?? session.clarification_answer}` : "尚未完成澄清。"}</p>
              </div>
            </article>
            <article>
              <span className="stage-number result">D</span>
              <div className="stage-content">
                <div className="stage-title"><strong>系统响应 / 执行结果</strong><span>后端状态派生</span></div>
                <p data-testid="system-response">{stateText ?? "请从上方启动一个场景。"}</p>
                {session?.runtime_mode === "TEXT_ANALYSIS" && (
                  <div className="reaction-card final-reaction">
                    <span className="section-label">
                      {session.final_reaction ? "最终回复" : "初步回复"}
                    </span>
                    <p>{(session.final_reaction ?? session.provisional_reaction)?.reply_text}</p>
                    <small>语气 {displayStatus((session.final_reaction ?? session.provisional_reaction)?.tone ?? "OTHER")}</small>
                    {(session.final_reaction ?? session.provisional_reaction)?.reasons.map((reason) => (
                      <p key={reason}>{reason}</p>
                    ))}
                    <div className="decision-summary">
                      {session.policy_suggestion_decisions.map((decision) => (
                        <span key={decision.suggestion_type}>
                          {LABELS[decision.suggestion_type] ?? decision.suggestion_type}：{decision.accepted ? "已纳入建议" : "未纳入建议"}
                        </span>
                      ))}
                      {conversation.acDecision && (
                        <span>空调判断：{conversation.acDecision.outcomeLabel}</span>
                      )}
                    </div>
                    <details className="audit-technical">
                      <summary>查看策略技术详情</summary>
                      <pre className="mini-json">{pretty({
                        suggestion_decisions: session.policy_suggestion_decisions,
                        ac_decision: session.ac_decision,
                      })}</pre>
                    </details>
                  </div>
                )}
                {((session?.runtime_mode === "LIVE" && session.step3_output) ||
                  (session?.runtime_mode === "TEXT_ANALYSIS" && session.final_reaction)) && (
                  <div className="tts-control">
                    <button className="secondary" disabled={busy} onClick={requestTTS}>
                      生成语音
                    </button>
                    <small>{ttsStatus ?? "文本已先显示；音频不可用时保持纯文本。"}</small>
                  </div>
                )}
              </div>
            </article>
          </div>
        </section>

        <aside className="right-column">
          <section className="panel compact-panel">
            <div className="panel-title"><div><span className="eyebrow">03 · 上下文</span><h2>环境与记忆</h2></div></div>
            <div className="weather-card">
              <span className="weather-icon">
                {weather.temperature_c === undefined ? "—" : `${Number(weather.temperature_c).toFixed(1)}°`}
              </span>
              <div>
                <strong>{displayStatus(String(weather.condition ?? "等待天气"))}</strong>
                <small>
                  城市 {CITY_NAMES[(weather.city_code ?? selectedCityCode) as CityCode]} · 来源 {displayStatus(String(weather.source ?? "WAITING"))}
                </small>
                {Boolean(weather.fetched_at) && <small>{String(weather.fetched_at)}</small>}
                {weather.provider === "OPEN_METEO" && <small>天气数据由 Open-Meteo.com 提供</small>}
              </div>
              <ScopeBadge scope={outboundEvent?.network_scope ?? "INTERNET"} />
            </div>
            <div className="memory-header">
              <span className="section-label">已确认 SQLite 记忆</span>
              <button className="text-button" disabled={busy} onClick={() => void confirmMemory()}>确认示例偏好</button>
            </div>
            <div className="memory-list">
              {state.memories.length ? state.memories.map((memory) => (
                <div className="memory-item" key={memory.memory_id}>
                  <div><strong>工作日晚间 · 舒缓钢琴</strong><code>{memory.memory_id}</code></div>
                  <button className="icon-button" aria-label="删除记忆" disabled={busy} onClick={() => void deleteMemory(memory)}>×</button>
                </div>
              )) : <p className="empty-copy">当前没有已确认记忆。</p>}
            </div>
          </section>

          <section className="panel compact-panel personalization-panel">
            <div className="panel-title">
              <div><span className="eyebrow">仅保存已确认内容</span><h2>用户偏好</h2></div>
            </div>
            <div className="preference-controls">
              <div className="preference-group">
                <div className="preference-heading">
                  <span>
                    回复风格：{displayStatus(preferences?.reply_style.value ?? "GENTLE")}
                    {preferences?.reply_style.confirmed ? " · 已确认" : " · 本轮默认"}
                  </span>
                  {preferences?.reply_style.confirmed && (
                    <button className="text-button" disabled={busy} onClick={() => void deletePreference("reply_style")}>清除选择</button>
                  )}
                </div>
                <div className="preference-options options-three">
                  <button disabled={busy} onClick={() => void confirmPreference("reply_style", "GENTLE")}>温和</button>
                  <button disabled={busy} onClick={() => void confirmPreference("reply_style", "CONCISE")}>简洁</button>
                  <button disabled={busy} onClick={() => void confirmPreference("reply_style", "DIRECT")}>直接</button>
                </div>
              </div>
              <div className="preference-group">
                <div className="preference-heading">
                  <span>音乐偏好：{preferences?.music_preference.value ? displayStatus(preferences.music_preference.value) : "未确认"}</span>
                  {preferences?.music_preference.confirmed && (
                    <button className="text-button" disabled={busy} onClick={() => void deletePreference("music_preference")}>清除选择</button>
                  )}
                </div>
                <div className="preference-options options-two">
                  <button disabled={busy} onClick={() => void confirmPreference("music_preference", "EMOTION_MATCHED")}>按情绪匹配音乐</button>
                  <button disabled={busy} onClick={() => void confirmPreference("music_preference", "NONE")}>不播放音乐</button>
                </div>
              </div>
            </div>
            <div className="memory-header">
              <span className="section-label">情绪摘要 · {summaries.length}/50</span>
              <button className="text-button" disabled={busy || summaries.length === 0} onClick={() => void clearSummaries()}>全部清除</button>
            </div>
            <div className="summary-list">
              {summaries.slice(0, 5).map((summary) => (
                <div className="memory-item" key={summary.summary_id}>
                  <div>
                    <strong>{LABELS[summary.final_emotion] ?? summary.final_emotion} · {displayStatus(summary.reaction_tone)}</strong>
                    <span>音乐：{displayStatus(summary.music_outcome)} · 空调：{displayStatus(summary.ac_outcome)}</span>
                  </div>
                </div>
              ))}
              {summaries.length === 0 && <p className="empty-copy">暂无经过隐私处理的摘要。</p>}
            </div>
          </section>

          <section className="panel compact-panel music-catalog-panel" data-testid="music-catalog-panel">
            <div className="panel-title">
              <div><span className="eyebrow">本地目录 · Audius 预览</span><h2>音乐推荐</h2></div>
              <StatusPill value={displayedMusicCategory?.status ?? (musicCatalogError ? "UNAVAILABLE" : "WAITING")} />
            </div>
            <p className="music-privacy-copy">
              Agent 只选择情绪类别；曲目 ID 由本地目录轮转，原始对话和情绪历史不会发送给 Audius。
            </p>
            <div className="music-category-tabs" role="tablist" aria-label="音乐种子分类">
              {(musicCatalog?.categories ?? []).map((category) => (
                <button
                  key={category.key}
                  type="button"
                  className={(proposedMusicCategory ?? selectedMusicCategory) === category.key ? "active" : ""}
                  aria-selected={(proposedMusicCategory ?? selectedMusicCategory) === category.key}
                  disabled={Boolean(proposedMusicCategory)}
                  onClick={() => setSelectedMusicCategory(category.key)}
                >
                  {MUSIC_CATEGORY_LABELS[category.key] ?? category.key}
                  <small>{category.ready_count}/{category.track_count}</small>
                </button>
              ))}
            </div>
            {displayedMusicCategory ? (
              <div className="music-track-list">
                {displayedMusicCategory.tracks.slice(0, 4).map((track) => (
                  <div className="music-track" key={track.track_id}>
                    <div><strong>{track.title}</strong><span>{track.artist}</span></div>
                    <small>{track.genre} · {track.energy} · {track.vocal_type}</small>
                  </div>
                ))}
                {displayedMusicCategory.tracks.length > 4 && (
                  <small>另有 {displayedMusicCategory.tracks.length - 4} 首候选，播放时由本地目录轮转。</small>
                )}
              </div>
            ) : (
              <p className="empty-copy">
                {musicCatalogError ? `音乐目录不可用：${musicCatalogError}` : "正在读取本地种子目录。"}
              </p>
            )}
            {session?.music_action ? (
              <ActionCard
                compact
                title="确认播放推荐音乐"
                proposal={session.music_action}
                persisted={musicPersistent}
                session={session}
                enabled={actionButtonsEnabled(session, "music")}
                busy={busy}
                onDecision={(approved) => decide(session.music_action, approved)}
                testId="music-action-id"
              />
            ) : (
              <div className="music-action-placeholder">
                完成情绪确认后，确定性策略才会生成音乐动作；当前浏览分类不会触发联网或播放。
              </div>
            )}
            <div className="music-browser-player">
              <audio
                ref={musicPlayerRef}
                controls
                preload="none"
                src={musicAudioUrl ?? undefined}
                onPlay={() => setMusicPlaybackStatus("浏览器已开始播放")}
                onEnded={() => setMusicPlaybackStatus("播放完成，可再次播放本次预览")}
              />
              <small>{musicPlaybackStatus ?? "授权后音频只交付到当前浏览器，不写入长期记忆。"}</small>
            </div>
          </section>
          <ActionCard
            title="空调动作"
            proposal={session?.ac_action ?? null}
            persisted={acPersistent}
            session={session}
            enabled={actionButtonsEnabled(session, "ac")}
            busy={busy}
            onDecision={(approved) => decide(session?.ac_action ?? null, approved)}
            testId="ac-action-id"
          />

          <section className="panel compact-panel privacy-panel">
            <div className="card-heading">
              <div><span className="eyebrow">隐私网关</span><h3>{displayStatus(violationPaths.length || rejectedInternetEvent ? "REJECTED" : outboundEvent ? "PASSED" : "WAITING")}</h3></div>
              <ScopeBadge scope={outboundEvent?.network_scope ?? "LOCAL"} />
            </div>
            {violationPaths.length > 0 && (
              <div className="violation-list">{violationPaths.map((path) => <code key={path}>{path}</code>)}</div>
            )}
            <span className="section-label">实际外发 JSON</span>
            <pre className="outbound-json" data-testid="outbound-json">
              {outboundEvent ? pretty(outbound) : "// 请求未发送或尚未产生"}
            </pre>
            <small>{outboundEvent ? "已通过外部连接器发送" : violationPaths.length || rejectedInternetEvent ? "隐私网关已在发送边界前阻断" : "等待网络事件"}</small>
            <span className="section-label">本机播放器实际数据</span>
            <pre className="outbound-json" data-testid="local-outbound-json">
              {localActionEvent ? pretty(localOutbound) : "// 尚未调用本机播放器"}
            </pre>
            <div className="network-boundaries" data-testid="network-boundaries">
              <div><ScopeBadge scope="LOCAL" /><span>本地音乐 / 空调模拟</span><strong>{displayStatus(localActionEvent ? "OBSERVED" : "READY")}</strong></div>
              <div><ScopeBadge scope="LAN" /><span>未来实体空调接口</span><strong>{displayStatus("NOT INVOKED")}</strong></div>
              <div><ScopeBadge scope="INTERNET" /><span>天气 / Audius 外部连接器</span><strong>{displayStatus(outboundEvent ? outboundEvent.status : "WAITING")}</strong></div>
            </div>
          </section>
        </aside>
      </section>

      <section className="panel timeline-panel" data-testid="timeline">
        <div className="panel-title">
          <div><span className="eyebrow">04 · 实时审计</span><h2>代理事件时间线</h2></div>
          <span className="event-count">{state.events.length} 条真实后端事件</span>
        </div>
        <div className="timeline-table">
          <div className="timeline-head"><span>时间</span><span>代理 / 事件</span><span>网络</span><span>动作</span><span>延迟</span><span>状态</span></div>
          {state.events.length ? [...state.events].reverse().map((event: AuditEvent) => (
            <details className="timeline-row" key={event.event_id}>
              <summary>
                <time title={event.timestamp}>{timeLabel(event.timestamp)}</time>
                <span><strong>{event.source_agent}</strong><small>{event.event_type}</small></span>
                <ScopeBadge scope={event.network_scope} />
                <code>{event.action_id ?? "—"}</code>
                <span>{event.latency_ms === null ? "—" : `${event.latency_ms} ms`}</span>
                <StatusPill value={event.status} />
              </summary>
              <pre>{pretty(event.payload)}</pre>
            </details>
          )) : <div className="timeline-empty">启动场景后，真实后端 Event 将在这里回放并实时追加。</div>}
        </div>
      </section>

        </div>
      )}

      <footer className="cinder-footer">
        <span>Step3 建议 ≠ 确定性策略决策 ≠ 用户授权 ≠ 执行</span>
        <span data-testid="completion">空调物理控制未启用</span>
      </footer>
    </main>
  );
}

export default App;
