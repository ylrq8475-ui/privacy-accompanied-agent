import type {
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

export type MockScenario =
  | "success"
  | "camera_failed"
  | "asr_failed"
  | "model_timeout"
  | "api_failed"
  | "privacy_rejected";

export type DemoText = "主要是身体累" | "主要是心情低落" | "其他原因";

const LABEL_BY_TEXT: Record<DemoText, string> = {
  "主要是身体累": "PHYSICAL_FATIGUE",
  "主要是心情低落": "EMOTIONAL_LOW",
  "其他原因": "OTHER",
};

export class APIError extends Error {
  constructor(
    readonly status: number,
    readonly response: Record<string, unknown>,
  ) {
    super(String(response.detail ?? response.error ?? `HTTP ${status}`));
  }
}

async function jsonRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: init?.body
      ? { "content-type": "application/json", ...init.headers }
      : init?.headers,
  });
  const body = (await response.json()) as Record<string, unknown>;
  if (!response.ok) {
    throw new APIError(response.status, body);
  }
  return body as T;
}

export const api = {
  analyzeText: (text: string, cityCode: CityCode = "310000") =>
    jsonRequest<TextAnalysisResponse>("/v1/analysis/text", {
      method: "POST",
      body: JSON.stringify({ text, city_code: cityCode }),
    }),
  continueTextAnalysis: (analysisId: string) =>
    jsonRequest<SessionSnapshot>(`/v1/analysis/text/${analysisId}/sessions`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  confirmTextState: (sessionId: string, label: TextStateLabel) =>
    jsonRequest<SessionSnapshot>(
      `/v1/analysis/sessions/${sessionId}/state-confirmation`,
      { method: "POST", body: JSON.stringify({ label }) },
    ),
  getTextSession: (sessionId: string) =>
    jsonRequest<SessionSnapshot>(`/v1/analysis/sessions/${sessionId}`),
  getTextEvents: async (sessionId: string) => {
    const response = await jsonRequest<{ events: AuditEvent[] }>(
      `/v1/analysis/sessions/${sessionId}/events`,
    );
    return response.events;
  },
  authorizeText: (sessionId: string, actionId: string, approved: boolean) =>
    jsonRequest<SessionSnapshot>(
      `/v1/analysis/sessions/${sessionId}/actions/${actionId}/authorization`,
      { method: "POST", body: JSON.stringify({ approved }) },
    ),
  resetText: (sessionId: string) =>
    jsonRequest<SessionSnapshot>(`/v1/analysis/sessions/${sessionId}/reset`, {
      method: "POST",
    }),
  synthesizeText: async (sessionId: string) => {
    const response = await fetch(`/v1/analysis/sessions/${sessionId}/tts`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
    if (response.headers.get("content-type")?.startsWith("audio/wav")) {
      const latency = Number.parseInt(response.headers.get("x-model-latency-ms") ?? "0", 10);
      return {
        status: "READY" as const,
        audio: await response.blob(),
        error: null,
        latency_ms: Number.isFinite(latency) ? latency : 0,
      };
    }
    const body = (await response.json()) as Record<string, unknown>;
    if (!response.ok) throw new APIError(response.status, body);
    return { status: "TEXT_ONLY" as const, audio: null, error: String(body.error ?? "TTS_UNAVAILABLE"), latency_ms: 0 };
  },
  getUserPreferences: () => jsonRequest<UserPreferences>("/v1/user-preferences"),
  confirmUserPreference: (key: "reply_style" | "music_preference", value: string) =>
    jsonRequest("/v1/user-preferences/confirm", {
      method: "POST",
      body: JSON.stringify({ key, value }),
    }),
  deleteUserPreference: (key: "reply_style" | "music_preference") =>
    jsonRequest<{ deleted: true }>(`/v1/user-preferences/${key}`, { method: "DELETE" }),
  listEmotionSummaries: async () => {
    const response = await jsonRequest<{ summaries: EmotionSummary[] }>("/v1/emotion-summaries");
    return response.summaries;
  },
  clearEmotionSummaries: () =>
    jsonRequest<{ deleted_count: number }>("/v1/emotion-summaries", { method: "DELETE" }),
  createSession: (scenario: MockScenario, cityCode: CityCode = "310000") =>
    jsonRequest<SessionSnapshot>("/v1/mock/sessions", {
      method: "POST",
      body: JSON.stringify({ scenario, city_code: cityCode }),
    }),
  getSession: (sessionId: string) =>
    jsonRequest<SessionSnapshot>(`/v1/mock/sessions/${sessionId}`),
  getEvents: async (sessionId: string) => {
    const response = await jsonRequest<{ events: AuditEvent[] }>(
      `/v1/mock/sessions/${sessionId}/events`,
    );
    return response.events;
  },
  submitText: (sessionId: string, text: DemoText) =>
    jsonRequest<SessionSnapshot>(`/v1/mock/sessions/${sessionId}/text-input`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  authorize: (sessionId: string, actionId: string, approved: boolean) =>
    jsonRequest<SessionSnapshot>(
      `/v1/mock/sessions/${sessionId}/actions/${actionId}/authorization`,
      { method: "POST", body: JSON.stringify({ approved }) },
    ),
  reset: (sessionId: string) =>
    jsonRequest<SessionSnapshot>(`/v1/mock/sessions/${sessionId}/reset`, {
      method: "POST",
    }),
  listMemories: async () => {
    const response = await jsonRequest<{ memories: MemoryRecord[] }>("/v1/memories");
    return response.memories;
  },
  confirmMemory: () =>
    jsonRequest<MemoryRecord>("/v1/memories/confirm", {
      method: "POST",
      body: JSON.stringify({
        context: "workday_evening",
        preference: "calm_piano",
        confirmed: true,
      }),
    }),
  deleteMemory: (memoryId: string) =>
    jsonRequest<{ deleted: true }>(`/v1/memories/${memoryId}`, { method: "DELETE" }),
  getAction: (actionId: string) =>
    jsonRequest<PersistedAction>(`/v1/actions/${actionId}`),
  liveHealth: () => jsonRequest<LiveHealth>("/v1/live/health"),
  musicCatalog: () => jsonRequest<MusicCatalogResponse>("/v1/music/catalog"),
  consumeMusicAudio: async (sessionId: string, actionId: string) => {
    const response = await fetch(
      `/v1/music/sessions/${sessionId}/actions/${actionId}/audio`,
      { headers: { accept: "audio/*" } },
    );
    const contentType = response.headers.get("content-type") ?? "";
    if (response.ok && contentType.startsWith("audio/")) {
      return { audio: await response.blob(), contentType };
    }
    const body = (await response.json()) as Record<string, unknown>;
    throw new APIError(response.status, body);
  },
  listSyntheticScenes: async () => {
    const response = await jsonRequest<{ scenes: SyntheticScene[] }>(
      "/v1/live/perception/scenes",
    );
    return response.scenes;
  },
  analyzeSyntheticScene: (
    sceneId: SyntheticScene["scene_id"],
    cityCode: CityCode = "310000",
  ) =>
    jsonRequest<VisualPerceptionResponse>("/v1/live/perception/analyze", {
      method: "POST",
      body: JSON.stringify({ scene_id: sceneId, city_code: cityCode }),
    }),
  pollLive: (cityCode: CityCode = "310000") =>
    jsonRequest<{
      observation: {
        person_present: boolean;
        source: string;
        degraded: boolean;
        degraded_reasons: string[];
        latency_ms: number;
      };
      session: SessionSnapshot | null;
    }>("/v1/live/perception/poll", {
      method: "POST",
      body: JSON.stringify({ city_code: cityCode }),
    }),
  getLiveSession: (sessionId: string) =>
    jsonRequest<SessionSnapshot>(`/v1/live/sessions/${sessionId}`),
  getLiveEvents: async (sessionId: string) => {
    const response = await jsonRequest<{ events: AuditEvent[] }>(
      `/v1/live/sessions/${sessionId}/events`,
    );
    return response.events;
  },
  submitLiveTranscript: (sessionId: string) =>
    jsonRequest<SessionSnapshot>(`/v1/live/sessions/${sessionId}/transcript`, {
      method: "POST",
      body: JSON.stringify({ text: "今天有点累" }),
    }),
  clarifyLive: (sessionId: string, text: DemoText) =>
    jsonRequest<SessionSnapshot>(`/v1/live/sessions/${sessionId}/clarifications`, {
      method: "POST",
      body: JSON.stringify({ answer: LABEL_BY_TEXT[text] }),
    }),
  authorizeLive: (sessionId: string, actionId: string, approved: boolean) =>
    jsonRequest<SessionSnapshot>(
      `/v1/live/sessions/${sessionId}/actions/${actionId}/authorization`,
      { method: "POST", body: JSON.stringify({ approved }) },
    ),
  resetLive: (sessionId: string) =>
    jsonRequest<SessionSnapshot>(`/v1/live/sessions/${sessionId}/reset`, {
      method: "POST",
    }),
  synthesizeLive: (sessionId: string) =>
    jsonRequest<{
      status: "READY" | "TEXT_ONLY";
      source: string;
      audio_ref: string | null;
      latency_ms: number;
    }>(`/v1/live/sessions/${sessionId}/tts`, { method: "POST" }),
};

export function connectEventStream(
  sessionId: string,
  onEvent: (event: AuditEvent) => void,
  onState: (state: "connecting" | "live" | "offline") => void,
  mode: "mock" | "live" | "analysis" = "mock",
): () => void {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(
    `${protocol}//${window.location.host}/v1/${mode}/sessions/${sessionId}/events`,
  );
  onState("connecting");
  socket.onopen = () => onState("live");
  socket.onmessage = (message) => onEvent(JSON.parse(message.data) as AuditEvent);
  socket.onerror = () => onState("offline");
  socket.onclose = () => onState("offline");
  return () => socket.close(1000, "console switched session");
}
