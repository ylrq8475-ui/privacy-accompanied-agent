import { afterEach, describe, expect, it, vi } from "vitest";

import { APIError, api } from "./api";

describe("experimental text analysis API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts only the ephemeral text to the independent endpoint", async () => {
    const responseBody = {
      analysis_id: "analysis-synthetic",
      experimental: true,
      model_source: "STEP3",
      confidence_kind: "MODEL_SELF_REPORTED_UNCALIBRATED",
      primary_state: {
        label: "HAPPY",
        display_name: "开心",
        confidence: 0.82,
        evidence: ["表达积极情绪"],
      },
      candidates: [],
      reaction: {
        reply_text: "Synthetic reply",
        tone: "SUPPORTIVE",
        follow_up_question: null,
        reasons: ["Synthetic reason"],
        suggestions: [],
      },
      weather_context: {
        city_code: "310000",
        temperature_c: 22,
        condition: "clear",
        source: "FIXED_DEMO",
        fetched_at: "2026-07-20T00:00:00Z",
        provider: "FIXED_DEMO",
      },
      latency_ms: 19,
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.analyzeText("今天很开心", "310000")).resolves.toEqual(responseBody);
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/analysis/text",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ text: "今天很开心", city_code: "310000" }),
      }),
    );
  });

  it("continues a server-owned result without resending raw text", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ session_id: "session-1" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.continueTextAnalysis("analysis-synthetic");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/analysis/text/analysis-synthetic/sessions",
      expect.objectContaining({ method: "POST", body: "{}" }),
    );
    expect(JSON.stringify(fetchMock.mock.calls)).not.toContain("今天很开心");
  });

  it("surfaces 503 and does not fabricate a fallback result", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: "MODEL_UNAVAILABLE" }), {
          status: 503,
          headers: { "content-type": "application/json" },
        }),
      ),
    );

    await expect(api.analyzeText("synthetic")).rejects.toEqual(
      expect.objectContaining<Partial<APIError>>({ status: 503 }),
    );
  });

  it("confirms one of nine labels without resending raw text", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ session_id: "session-1" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.confirmTextState("session-1", "ANXIOUS");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/analysis/sessions/session-1/state-confirmation",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ label: "ANXIOUS" }),
      }),
    );
    expect(JSON.stringify(fetchMock.mock.calls)).not.toContain("private raw text");
  });

  it("uses the independent text action authorization route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ state: "COMPLETED" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.authorizeText("session-1", "music-1", false);
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/analysis/sessions/session-1/actions/music-1/authorization",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ approved: false }) }),
    );
  });
});

describe("synthetic visual perception API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("loads only the server-owned synthetic scene manifest", async () => {
    const scenes = [
      {
        scene_id: "indoor_person",
        label: "室内有人",
        image_url: "/v1/live/perception/scenes/indoor_person/image",
        synthetic: true,
      },
      {
        scene_id: "indoor_empty",
        label: "室内无人",
        image_url: "/v1/live/perception/scenes/indoor_empty/image",
        synthetic: true,
      },
    ];
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ scenes }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.listSyntheticScenes()).resolves.toEqual(scenes);
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/live/perception/scenes",
      expect.objectContaining({ headers: undefined }),
    );
  });

  it("submits only one allowlisted scene id for real Step3 perception", async () => {
    const response = {
      attempt_id: "perception-synthetic",
      observation: {
        attempt_id: "perception-synthetic",
        scene_id: "indoor_person",
        synthetic: true,
        perception_source: "SYNTHETIC_IMAGE",
        model_source: "STEP3",
        network_scope: "LOCAL",
        person_present: true,
        scene_type: "INDOOR",
        scene_summary: "合成客厅中有人。",
        confidence: 0.91,
        confidence_kind: "MODEL_SELF_REPORTED_UNCALIBRATED",
        evidence: ["人物轮廓"],
        latency_ms: 27,
        raw_request_persisted: false,
        raw_response_persisted: false,
      },
      session: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.analyzeSyntheticScene("indoor_person", "440300")).resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/live/perception/analyze",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ scene_id: "indoor_person", city_code: "440300" }),
      }),
    );
    expect(JSON.stringify(fetchMock.mock.calls)).not.toContain("data:image");
  });
});
