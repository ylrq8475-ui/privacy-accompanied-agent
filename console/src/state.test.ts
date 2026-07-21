import { describe, expect, it } from "vitest";

import {
  actionButtonsEnabled,
  initialState,
  latestEventOfTypes,
  mergeEvents,
  reducer,
} from "./state";
import type { AuditEvent, SessionSnapshot } from "./types";

function event(id: string, timestamp: string): AuditEvent {
  return {
    event_id: id,
    session_id: "session-1",
    timestamp,
    source_agent: "orchestrator",
    event_type: "STATE_TRANSITION",
    payload: {},
    confidence: null,
    privacy_level: "INTERNAL",
    network_scope: "LOCAL",
    action_id: null,
    latency_ms: null,
    status: "OK",
  };
}

const session = {
  active: true,
  state: "WAITING_MUSIC_AUTHORIZATION",
} as SessionSnapshot;

describe("console state", () => {
  it("deduplicates replayed events and orders them by timestamp", () => {
    const early = event("event-a", "2026-07-20T10:00:00Z");
    const late = event("event-b", "2026-07-20T10:00:01Z");
    expect(mergeEvents([late], [early, late]).map((item) => item.event_id)).toEqual([
      "event-a",
      "event-b",
    ]);
  });

  it("keeps server actions keyed by their independent action id", () => {
    const music = { action_id: "music-1" } as never;
    const ac = { action_id: "ac-1" } as never;
    const withMusic = reducer(initialState, { type: "action", value: music });
    const withBoth = reducer(withMusic, { type: "action", value: ac });
    expect(Object.keys(withBoth.actions).sort()).toEqual(["ac-1", "music-1"]);
  });

  it("enables only the action matching the server state and blocks inactive sessions", () => {
    expect(actionButtonsEnabled(session, "music")).toBe(true);
    expect(actionButtonsEnabled(session, "ac")).toBe(false);
    expect(actionButtonsEnabled({ ...session, active: false }, "music")).toBe(false);
  });

  it("selects the newest real or Mock internet audit event", () => {
    const mock = { ...event("event-mock", "2026-07-20T10:00:00Z"), event_type: "INTERNET_MOCK_REQUEST" };
    const real = { ...event("event-real", "2026-07-20T10:00:01Z"), event_type: "INTERNET_REQUEST" };
    const state = { ...initialState, events: [mock, real] };
    expect(latestEventOfTypes(state, ["INTERNET_REQUEST", "INTERNET_MOCK_REQUEST"])).toBe(real);
  });
});
