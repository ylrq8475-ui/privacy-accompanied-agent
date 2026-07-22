export type NetworkScope = "LOCAL" | "LAN" | "INTERNET";
export type ConnectionState = "idle" | "connecting" | "live" | "offline";

export type CityCode = "110000" | "310000" | "440100" | "440300" | "510100";

export type TextStateLabel =
  | "PHYSICAL_FATIGUE"
  | "EMOTIONAL_LOW"
  | "HAPPY"
  | "STRESSED"
  | "ANXIOUS"
  | "LONELY"
  | "ANGRY"
  | "CALM"
  | "OTHER";

export interface TextStateResult {
  label: TextStateLabel;
  display_name: string;
  confidence: number;
  evidence: string[];
}

export interface TextAnalysisResponse {
  analysis_id: string;
  experimental: true;
  model_source: "STEP3";
  confidence_kind: "MODEL_SELF_REPORTED_UNCALIBRATED";
  primary_state: TextStateResult;
  candidates: TextStateResult[];
  reaction: LLMReaction;
  weather_context: WeatherSnapshot;
  latency_ms: number;
}

export type ReactionTone =
  | "SUPPORTIVE"
  | "CELEBRATORY"
  | "CALMING"
  | "GROUNDING"
  | "NEUTRAL";

export type ReactionSuggestion =
  | { type: "EMOTION_MATCHED_MUSIC" }
  | { type: "AC"; direction: "WARMER" | "COOLER" };

export interface LLMReaction {
  reply_text: string;
  tone: ReactionTone;
  follow_up_question: string | null;
  reasons: string[];
  suggestions: ReactionSuggestion[];
}

export interface WeatherSnapshot {
  city_code: CityCode;
  temperature_c: number;
  condition: string;
  source: "REAL_API" | "CACHE" | "FIXED_DEMO";
  fetched_at: string;
  provider: "OPEN_METEO" | "FIXED_DEMO";
}

export interface StateHypothesis {
  label: string;
  confidence: number;
  evidence: string[];
}

export interface Step3Output {
  state_hypotheses: StateHypothesis[];
  recommended_action: { type: string; category: string | null };
  recommendation_reason: Array<{ code: string; text: string }>;
  clarification_candidates: Array<{
    question_id: string;
    question: string;
    target_labels: string[];
  }>;
}

export interface ActionProposal {
  action_id: string;
  session_id: string;
  action_type: "PLAY_MUSIC" | "SET_AC";
  payload: Record<string, unknown>;
  proposed_at: string;
  expires_at: string;
}

export interface Authorization {
  action_id: string;
  action_type: string;
  authorization_status: string;
  decided_at: string | null;
  expires_at: string;
}

export interface ActionResult {
  action_id: string;
  action_type: string;
  execution_status: string;
  result: Record<string, unknown>;
  completed_at: string;
}

export interface ACDecision {
  decision: "HEAT" | "COOL" | "OFF" | "UNAVAILABLE";
  outdoor_temperature_c: number;
  weather_source: WeatherSnapshot["source"];
  weather_fetched_at: string;
  confirmed_emotion: TextStateLabel;
  base_target_temperature: number | null;
  emotion_adjustment_c: -1 | 0 | 1;
  target_temperature: number | null;
  reason_code: string;
}

export interface SessionSnapshot {
  session_id: string;
  state: string;
  state_history: string[];
  transcript: string | null;
  step3_output: Step3Output | null;
  clarification_answer: string | null;
  music_action: ActionProposal | null;
  ac_action: ActionProposal | null;
  authorizations: Record<string, Authorization>;
  results: Record<string, ActionResult>;
  event_count: number;
  storage: string;
  active: boolean;
  person_present: boolean;
  perception_source: "MOCK" | "CAMERA" | "VIDEO" | "STATIC_SYNTHETIC" | "SYNTHETIC_IMAGE" | "DEMO_IMAGE" | "TEXT_ONLY";
  interaction_source:
    | "NONE"
    | "MOCK_ASR"
    | "TEXT"
    | "STEPAUDIO_ASR"
    | "TEXT_FALLBACK_PENDING"
    | "TEXT_FALLBACK"
    | "TEXT_ANALYSIS";
  last_text_input: string | null;
  runtime_mode: "MOCK" | "LIVE" | "TEXT_ANALYSIS";
  model_source: "MOCK_STEP3" | "NOT_CALLED" | "STEP3" | "RULE_FALLBACK";
  degraded_reasons: string[];
  component_health: Record<
    string,
    { available: boolean; status: string; latency_ms: number }
  >;
  text_analysis: {
    state_hypotheses: Array<{
      label: TextStateLabel;
      confidence: number;
      evidence: string[];
    }>;
  } | null;
  provisional_reaction: LLMReaction | null;
  final_reaction: LLMReaction | null;
  confirmed_emotion: TextStateLabel | null;
  weather_context: WeatherSnapshot | null;
  ac_decision: ACDecision | null;
  policy_suggestion_decisions: Array<{
    suggestion_type: "EMOTION_MATCHED_MUSIC" | "AC";
    accepted: boolean;
    reason_code: string;
  }>;
  summary_saved: boolean;
  visual_perception: VisualPerception | null;
  visual_scene_id: string | null;
}

export interface VisualPerception {
  person_present: boolean;
  scene_type: "INDOOR" | "OUTDOOR" | "UNCERTAIN";
  scene_summary: string;
  confidence: number;
  evidence: string[];
}

export interface SyntheticScene {
  scene_id: "indoor_person" | "indoor_empty";
  label: string;
  image_url: string;
  synthetic: boolean;
}

export interface VisualPerceptionObservation extends VisualPerception {
  attempt_id: string;
  scene_id: SyntheticScene["scene_id"];
  synthetic: boolean;
  perception_source: "SYNTHETIC_IMAGE" | "DEMO_IMAGE";
  model_source: "STEP3";
  network_scope: "LOCAL";
  confidence_kind: "MODEL_SELF_REPORTED_UNCALIBRATED";
  latency_ms: number;
  raw_request_persisted: false;
  raw_response_persisted: false;
}

export interface VisualPerceptionResponse {
  attempt_id: string;
  observation: VisualPerceptionObservation;
  session: SessionSnapshot | null;
}

export interface LiveHealth {
  mode: "LIVE_WITH_SAFE_FALLBACKS";
  deployment: {
    backend: string;
    console_access: string;
  };
  components: Array<{
    component: string;
    available: boolean;
    status: string;
    latency_ms: number;
  }>;
  raw_media_persisted: false;
  synthetic_scene_bundled: true;
  last_visual_attempt: Record<string, unknown> | null;
}

export interface MusicCatalogTrack {
  catalog_id: string;
  track_id: string;
  title: string;
  artist: string;
  genre: string;
  audius_mood: string;
  energy: string;
  vocal_type: string;
}

export interface MusicCatalogCategory {
  key: "RELAX" | "COMFORT" | "UPLIFT" | "COOLDOWN" | "NEUTRAL";
  status: string;
  track_count: number;
  ready_count: number;
  tracks: MusicCatalogTrack[];
}

export interface MusicCatalogResponse {
  source: "BUNDLED_SEED";
  local_only: true;
  provider_urls_exposed: false;
  credentials_exposed: false;
  categories: MusicCatalogCategory[];
}

export interface AuditEvent {
  event_id: string;
  session_id: string;
  timestamp: string;
  source_agent: string;
  event_type: string;
  payload: Record<string, unknown>;
  confidence: number | null;
  privacy_level: string;
  network_scope: NetworkScope;
  action_id: string | null;
  latency_ms: number | null;
  status: string;
}

export interface MemoryRecord {
  memory_id: string;
  context: "workday_evening";
  preference: "calm_piano";
  confirmed: true;
  created_at: string;
  updated_at: string;
}

export interface PersistedAction {
  action_id: string;
  session_id: string;
  action_type: string;
  authorization_status: string;
  execution_status: string;
  payload: Record<string, unknown>;
  created_at: string;
  expires_at: string;
  authorized_at: string | null;
  executed_at: string | null;
  result: Record<string, unknown> | null;
}

export interface UserPreferences {
  reply_style: { value: "GENTLE" | "CONCISE" | "DIRECT"; confirmed: boolean; runtime_default: boolean };
  music_preference: { value: "EMOTION_MATCHED" | "NONE" | null; confirmed: boolean; runtime_default: boolean };
}

export interface EmotionSummary {
  summary_id: string;
  session_id: string;
  final_emotion: TextStateLabel;
  user_confirmed: boolean;
  confidence_band: "LOW" | "MEDIUM" | "HIGH";
  reaction_tone: ReactionTone;
  music_outcome: string;
  ac_outcome: string;
  created_at: string;
}
