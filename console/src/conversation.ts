import type {
  ACDecision,
  LLMReaction,
  SessionSnapshot,
  TextAnalysisResponse,
  TextStateLabel,
} from "./types";

export type ConversationRole = "assistant" | "user" | "system";

export interface ConversationCandidate {
  label: string;
  displayName: string;
  confidence: number;
  evidence: string[];
}

export interface ConversationMessage {
  id: string;
  role: ConversationRole;
  phase: "input" | "analysis" | "clarification" | "confirmation" | "final" | "result";
  label: string;
  text: string;
  question?: string | null;
  meta?: string[];
  candidates?: ConversationCandidate[];
  reasons?: string[];
  suggestions?: string[];
  privacyPlaceholder?: boolean;
}

export interface ConversationProjection {
  messages: ConversationMessage[];
  needsClarification: boolean;
  hasPendingActions: boolean;
  stateLabel: string;
  acDecision: ConversationACDecision | null;
}

export interface ConversationACDecision extends ACDecision {
  outcomeLabel: string;
  emotionLabel: string;
  weatherLabel: string;
  calculationLabel: string | null;
  requiresAuthorization: boolean;
}

export interface ConversationProjectionInput {
  analysisResult: TextAnalysisResponse | null;
  session: SessionSnapshot | null;
  submittedText: string;
  stateText?: string;
}

const EMOTION_NAMES: Record<TextStateLabel, string> = {
  PHYSICAL_FATIGUE: "身体疲劳",
  EMOTIONAL_LOW: "情绪低落",
  HAPPY: "开心",
  STRESSED: "压力",
  ANXIOUS: "焦虑",
  LONELY: "孤独",
  ANGRY: "愤怒",
  CALM: "平静",
  OTHER: "其他",
};

const RESULT_STATES = new Set([
  "COMPLETED",
  "MODEL_TIMEOUT",
  "API_FAILED",
  "CAMERA_FAILED",
  "ASR_FAILED",
  "PRIVACY_REJECTED",
  "ACTION_REJECTED",
  "ACTION_EXPIRED",
  "ACTION_FAILED",
]);

function suggestionLabel(type: string): string {
  return type === "EMOTION_MATCHED_MUSIC" ? "音乐建议" : type === "AC" ? "空调建议" : type;
}

function formatTemperature(value: number | null): string {
  return value === null || !Number.isFinite(value) ? "—" : `${Number(value.toFixed(1))}°C`;
}

export function projectACDecision(decision: ACDecision | null | undefined): ConversationACDecision | null {
  if (!decision) return null;

  const outcomeLabel = decision.decision === "HEAT"
    ? `建议制热至 ${formatTemperature(decision.target_temperature)}`
    : decision.decision === "COOL"
      ? `建议制冷至 ${formatTemperature(decision.target_temperature)}`
      : decision.decision === "OFF"
        ? "当前无需开启空调"
        : "天气数据不足，暂不建议操作";
  const weatherLabel = decision.weather_source === "REAL_API"
    ? "实时天气"
    : decision.weather_source === "CACHE"
      ? "缓存天气"
      : "演示天气";
  const emotionLabel = EMOTION_NAMES[decision.confirmed_emotion] ?? "其他";
  const adjustment = decision.emotion_adjustment_c > 0
    ? `+${decision.emotion_adjustment_c}`
    : String(decision.emotion_adjustment_c);
  const calculationLabel = decision.decision === "HEAT" || decision.decision === "COOL"
    ? `基础 ${formatTemperature(decision.base_target_temperature)}，${emotionLabel}状态调整 ${adjustment}°C`
    : null;

  return {
    ...decision,
    outcomeLabel,
    emotionLabel,
    weatherLabel,
    calculationLabel,
    requiresAuthorization: decision.decision === "HEAT" || decision.decision === "COOL",
  };
}

function reactionMessage(
  id: string,
  phase: "analysis" | "final",
  label: string,
  reaction: LLMReaction,
  meta: string[],
  candidates: ConversationCandidate[] = [],
): ConversationMessage {
  return {
    id,
    role: "assistant",
    phase,
    label,
    text: reaction.reply_text,
    question: reaction.follow_up_question,
    meta,
    candidates,
    reasons: reaction.reasons,
    suggestions: reaction.suggestions.map((item) => suggestionLabel(item.type)),
  };
}

export function buildConversationProjection({
  analysisResult,
  session,
  submittedText,
  stateText,
}: ConversationProjectionInput): ConversationProjection {
  const messages: ConversationMessage[] = [];

  const trimmedText = submittedText.trim();
  if (trimmedText) {
    messages.push({
      id: analysisResult?.analysis_id ? `input-${analysisResult.analysis_id}` : "input-pending",
      role: "user",
      phase: "input",
      label: "你 · 仅限当前页面",
      text: trimmedText,
    });
  } else if (session) {
    const isPrivateTextSession = session.runtime_mode === "TEXT_ANALYSIS";
    const recoveredText = isPrivateTextSession
      ? "已提交一段文本（原文未保存）"
      : session.transcript ?? session.last_text_input;
    if (recoveredText) {
      messages.push({
        id: `input-${session.session_id}`,
        role: "user",
        phase: "input",
        label: isPrivateTextSession ? "你 · 原文未保存" : "你 · 合成输入",
        text: recoveredText,
        privacyPlaceholder: isPrivateTextSession,
      });
    }
  }

  if (analysisResult) {
    messages.push(reactionMessage(
      `analysis-${analysisResult.analysis_id}`,
      "analysis",
      "Step3 · 初步判断",
      analysisResult.reaction,
      [
        `${analysisResult.primary_state.display_name} ${Math.round(analysisResult.primary_state.confidence * 100)}%`,
        `${analysisResult.latency_ms} 毫秒`,
        "本机处理",
      ],
      analysisResult.candidates.map((candidate) => ({
        label: candidate.label,
        displayName: candidate.display_name,
        confidence: candidate.confidence,
        evidence: candidate.evidence,
      })),
    ));
  }

  const provisional = session?.provisional_reaction;
  if (session && provisional && provisional.reply_text !== analysisResult?.reaction.reply_text) {
    const hypotheses = session.text_analysis?.state_hypotheses ?? session.step3_output?.state_hypotheses ?? [];
    messages.push(reactionMessage(
      `provisional-${session.session_id}`,
      "analysis",
      "Step3 · 初步判断",
      provisional,
      [session.model_source, "模型不负责授权"],
      hypotheses.map((candidate) => ({
        label: candidate.label,
        displayName: EMOTION_NAMES[candidate.label as TextStateLabel] ?? candidate.label,
        confidence: candidate.confidence,
        evidence: candidate.evidence,
      })),
    ));
  }

  const clarificationQuestion = session?.step3_output?.clarification_candidates[0]?.question
    ?? analysisResult?.reaction.follow_up_question;
  if (clarificationQuestion && (session?.state === "CLARIFICATION_REQUIRED" || session?.clarification_answer)) {
    messages.push({
      id: `clarification-${session?.session_id ?? analysisResult?.analysis_id ?? "pending"}`,
      role: "assistant",
      phase: "clarification",
      label: "确定性策略 · 需要澄清",
      text: "当前候选还不足以创建待授权动作，请明确确认你的状态。",
      question: clarificationQuestion,
      meta: ["确定性策略", "尚未创建动作"],
    });
  }

  if (session?.clarification_answer) {
    messages.push({
      id: `confirmation-${session.session_id}`,
      role: "user",
      phase: "confirmation",
      label: "你 · 已确认",
      text: EMOTION_NAMES[session.clarification_answer as TextStateLabel] ?? session.clarification_answer,
    });
  }

  if (session?.final_reaction) {
    messages.push(reactionMessage(
      `final-${session.session_id}`,
      "final",
      "AI · 最终回复",
      session.final_reaction,
      [
        session.confirmed_emotion ? `已确认 ${EMOTION_NAMES[session.confirmed_emotion as TextStateLabel] ?? session.confirmed_emotion}` : "状态已确认",
        ...session.policy_suggestion_decisions.map(
          (decision) => `${suggestionLabel(decision.suggestion_type)} ${decision.accepted ? "已接受" : "已拒绝"}`,
        ),
      ],
    ));
  }

  if (session && RESULT_STATES.has(session.state)) {
    messages.push({
      id: `result-${session.session_id}-${session.state}`,
      role: "system",
      phase: "result",
      label: "系统 · 执行结果",
      text: stateText ?? session.state,
      meta: [session.state, session.active ? "会话进行中" : "会话已结束"],
    });
  }

  return {
    messages,
    needsClarification: session?.state === "CLARIFICATION_REQUIRED",
    hasPendingActions: Boolean(session?.music_action || session?.ac_action),
    stateLabel: session?.state ?? (analysisResult ? "ANALYSIS_READY" : "READY"),
    acDecision: projectACDecision(session?.ac_decision),
  };
}
