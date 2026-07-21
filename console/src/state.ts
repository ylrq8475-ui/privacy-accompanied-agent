import type {
  AuditEvent,
  ConnectionState,
  MemoryRecord,
  PersistedAction,
  SessionSnapshot,
} from "./types";

export interface DashboardState {
  session: SessionSnapshot | null;
  events: AuditEvent[];
  memories: MemoryRecord[];
  actions: Record<string, PersistedAction>;
  connection: ConnectionState;
}

export type DashboardAction =
  | { type: "session"; value: SessionSnapshot }
  | { type: "events"; value: AuditEvent[] }
  | { type: "memory"; value: MemoryRecord[] }
  | { type: "action"; value: PersistedAction }
  | { type: "connection"; value: ConnectionState }
  | { type: "new-session" }
  | { type: "clear-session" };

export const initialState: DashboardState = {
  session: null,
  events: [],
  memories: [],
  actions: {},
  connection: "idle",
};

export function mergeEvents(current: AuditEvent[], incoming: AuditEvent[]): AuditEvent[] {
  const byId = new Map(current.map((event) => [event.event_id, event]));
  incoming.forEach((event) => byId.set(event.event_id, event));
  return [...byId.values()].sort(
    (left, right) =>
      Date.parse(left.timestamp) - Date.parse(right.timestamp) ||
      left.event_id.localeCompare(right.event_id),
  );
}

export function reducer(state: DashboardState, action: DashboardAction): DashboardState {
  switch (action.type) {
    case "session":
      return { ...state, session: action.value };
    case "events":
      return { ...state, events: mergeEvents(state.events, action.value) };
    case "memory":
      return { ...state, memories: action.value };
    case "action":
      return {
        ...state,
        actions: { ...state.actions, [action.value.action_id]: action.value },
      };
    case "connection":
      return { ...state, connection: action.value };
    case "new-session":
      return { ...state, session: null, events: [], actions: {}, connection: "idle" };
    case "clear-session":
      return { ...initialState, memories: state.memories };
  }
}

export function latestEvent(state: DashboardState, eventType: string): AuditEvent | undefined {
  return [...state.events].reverse().find((event) => event.event_type === eventType);
}

export function latestEventOfTypes(
  state: DashboardState,
  eventTypes: readonly string[],
): AuditEvent | undefined {
  const allowed = new Set(eventTypes);
  return [...state.events].reverse().find((event) => allowed.has(event.event_type));
}

export function transitionTo(
  state: DashboardState,
  targetState: string,
): AuditEvent | undefined {
  return [...state.events].reverse().find(
    (event) =>
      event.event_type === "STATE_TRANSITION" &&
      event.payload.to_state === targetState,
  );
}

export function actionButtonsEnabled(
  session: SessionSnapshot | null,
  actionType: "music" | "ac",
): boolean {
  if (!session?.active) return false;
  return actionType === "music"
    ? session.state === "WAITING_MUSIC_AUTHORIZATION"
    : session.state === "WAITING_AC_AUTHORIZATION";
}
