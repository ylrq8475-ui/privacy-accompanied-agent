import { describe, expect, it } from "vitest";

import { buildConversationProjection } from "./conversation";
import type { LLMReaction, SessionSnapshot, TextAnalysisResponse } from "./types";

const reaction: LLMReaction = {
  reply_text: "听起来你今天确实消耗了不少精力。",
  tone: "SUPPORTIVE",
  follow_up_question: "更接近身体疲劳，还是情绪低落？",
  reasons: ["用户描述了明显疲劳。"],
  suggestions: [{ type: "EMOTION_MATCHED_MUSIC" }],
};

const analysis = {
  analysis_id: "analysis-1",
  experimental: true,
  model_source: "STEP3",
  confidence_kind: "MODEL_SELF_REPORTED_UNCALIBRATED",
  primary_state: {
    label: "PHYSICAL_FATIGUE",
    display_name: "身体疲劳",
    confidence: 0.72,
    evidence: ["累"],
  },
  candidates: [
    {
      label: "PHYSICAL_FATIGUE",
      display_name: "身体疲劳",
      confidence: 0.72,
      evidence: ["累"],
    },
  ],
  reaction,
  weather_context: {
    city_code: "310000",
    temperature_c: 26,
    condition: "clear",
    source: "FIXED_DEMO",
    fetched_at: "2026-07-21T00:00:00Z",
    provider: "FIXED_DEMO",
  },
  latency_ms: 42,
} satisfies TextAnalysisResponse;

function session(overrides: Partial<SessionSnapshot> = {}): SessionSnapshot {
  return {
    session_id: "session-1",
    state: "CLARIFICATION_REQUIRED",
    state_history: [],
    transcript: null,
    step3_output: {
      state_hypotheses: [{ label: "PHYSICAL_FATIGUE", confidence: 0.72, evidence: ["累"] }],
      recommended_action: { type: "SUGGEST_MUSIC", category: "RELAX" },
      recommendation_reason: [{ code: "R1", text: "用户确认后再建议。" }],
      clarification_candidates: [{
        question_id: "question-1",
        question: "更接近身体疲劳，还是情绪低落？",
        target_labels: ["PHYSICAL_FATIGUE", "EMOTIONAL_LOW"],
      }],
    },
    clarification_answer: null,
    music_action: null,
    ac_action: null,
    authorizations: {},
    results: {},
    event_count: 0,
    storage: "sqlite",
    active: true,
    person_present: false,
    perception_source: "TEXT_ONLY",
    interaction_source: "TEXT_ANALYSIS",
    last_text_input: null,
    runtime_mode: "TEXT_ANALYSIS",
    model_source: "STEP3",
    degraded_reasons: [],
    component_health: {},
    text_analysis: {
      state_hypotheses: [{
        label: "PHYSICAL_FATIGUE",
        confidence: 0.72,
        evidence: ["累"],
      }],
    },
    provisional_reaction: reaction,
    final_reaction: null,
    confirmed_emotion: null,
    weather_context: null,
    ac_decision: null,
    policy_suggestion_decisions: [],
    summary_saved: false,
    visual_perception: null,
    visual_scene_id: null,
    ...overrides,
  };
}

describe("conversation projection", () => {
  it("starts ready without rendering a welcome message", () => {
    const result = buildConversationProjection({
      analysisResult: null,
      session: null,
      submittedText: "",
    });
    expect(result.messages).toHaveLength(0);
    expect(result.stateLabel).toBe("READY");
  });

  it("keeps the current raw text only in the in-memory projection", () => {
    const result = buildConversationProjection({
      analysisResult: analysis,
      session: null,
      submittedText: "今天很累",
    });
    expect(result.messages.find((item) => item.role === "user")?.text).toBe("今天很累");
    expect(result.messages.find((item) => item.phase === "analysis")?.meta).toContain("42 毫秒");
  });

  it("uses a privacy placeholder when a text session is restored", () => {
    const result = buildConversationProjection({
      analysisResult: null,
      session: session(),
      submittedText: "",
    });
    const input = result.messages.find((item) => item.phase === "input");
    expect(input?.text).toBe("已提交一段文本（原文未保存）");
    expect(input?.privacyPlaceholder).toBe(true);
  });

  it("shows deterministic clarification separately from model output", () => {
    const result = buildConversationProjection({
      analysisResult: analysis,
      session: session(),
      submittedText: "今天很累",
    });
    expect(result.needsClarification).toBe(true);
    expect(result.messages.find((item) => item.phase === "clarification")?.label).toBe(
      "确定性策略 · 需要澄清",
    );
  });

  it("orders explicit confirmation before the final reply", () => {
    const result = buildConversationProjection({
      analysisResult: analysis,
      session: session({
        state: "WAITING_MUSIC_AUTHORIZATION",
        clarification_answer: "PHYSICAL_FATIGUE",
        confirmed_emotion: "PHYSICAL_FATIGUE",
        final_reaction: { ...reaction, reply_text: "我会先陪你缓一缓。", follow_up_question: null },
      }),
      submittedText: "今天很累",
    });
    const phases = result.messages.map((item) => item.phase);
    expect(phases.indexOf("confirmation")).toBeLessThan(phases.indexOf("final"));
  });

  it("preserves independent music and AC action ids", () => {
    const result = buildConversationProjection({
      analysisResult: null,
      session: session({
        state: "WAITING_MUSIC_AUTHORIZATION",
        music_action: {
          action_id: "music-1",
          session_id: "session-1",
          action_type: "PLAY_MUSIC",
          payload: {},
          proposed_at: "2026-07-21T00:00:00Z",
          expires_at: "2026-07-21T00:05:00Z",
        },
        ac_action: {
          action_id: "ac-1",
          session_id: "session-1",
          action_type: "SET_AC",
          payload: {},
          proposed_at: "2026-07-21T00:00:00Z",
          expires_at: "2026-07-21T00:05:00Z",
        },
      }),
      submittedText: "",
    });
    expect(result.hasPendingActions).toBe(true);
  });

  it("projects an AC cooling decision as clear Chinese copy", () => {
    const result = buildConversationProjection({
      analysisResult: null,
      session: session({
        confirmed_emotion: "ANXIOUS",
        ac_decision: {
          decision: "COOL",
          outdoor_temperature_c: 31.2,
          weather_source: "REAL_API",
          weather_fetched_at: "2026-07-21T00:00:00Z",
          confirmed_emotion: "ANXIOUS",
          base_target_temperature: 26,
          emotion_adjustment_c: -1,
          target_temperature: 25,
          reason_code: "HOT_OUTDOOR",
        },
      }),
      submittedText: "",
    });

    expect(result.acDecision).toMatchObject({
      outcomeLabel: "建议制冷至 25°C",
      emotionLabel: "焦虑",
      weatherLabel: "实时天气",
      calculationLabel: "基础 26°C，焦虑状态调整 -1°C",
      requiresAuthorization: true,
    });
  });

  it.each([
    ["OFF", "当前无需开启空调"],
    ["UNAVAILABLE", "天气数据不足，暂不建议操作"],
  ] as const)("projects %s without a temperature calculation", (decision, outcomeLabel) => {
    const result = buildConversationProjection({
      analysisResult: null,
      session: session({
        confirmed_emotion: "CALM",
        ac_decision: {
          decision,
          outdoor_temperature_c: 22,
          weather_source: decision === "OFF" ? "CACHE" : "FIXED_DEMO",
          weather_fetched_at: "2026-07-21T00:00:00Z",
          confirmed_emotion: "CALM",
          base_target_temperature: null,
          emotion_adjustment_c: 0,
          target_temperature: null,
          reason_code: decision === "OFF" ? "COMFORTABLE" : "WEATHER_UNAVAILABLE",
        },
      }),
      submittedText: "",
    });

    expect(result.acDecision?.outcomeLabel).toBe(outcomeLabel);
    expect(result.acDecision?.calculationLabel).toBeNull();
    expect(result.acDecision?.requiresAuthorization).toBe(false);
  });

  it("adds an honest terminal result message", () => {
    const result = buildConversationProjection({
      analysisResult: null,
      session: session({ state: "COMPLETED", active: false }),
      submittedText: "",
      stateText: "本轮 Demo 已完成；空调仍为 Mock",
    });
    expect(result.messages.at(-1)).toMatchObject({ phase: "result", role: "system" });
    expect(result.messages.at(-1)?.text).toContain("空调仍为 Mock");
  });
});
