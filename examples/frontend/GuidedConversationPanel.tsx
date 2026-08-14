import { useEffect, useMemo, useState } from "react";
import { Room, RoomEvent } from "livekit-client";

import {
  GuidedConversationTurnFeedback,
  GuidedEvent,
  GuidedRecognitionWord,
  GuidedSessionView,
  sendGuidedCommand,
} from "./guided-conversation";

interface Props {
  room: Room;
  initialSession: GuidedSessionView;
  onShowReport: (sessionId: string) => void;
  onReplayAll: (sessionId: string) => Promise<void>;
}

interface HistoryEntry {
  key: string;
  role: "assistant" | "learner";
  text: string;
  words?: GuidedRecognitionWord[];
}

function sessionFromEvent(event: GuidedEvent): GuidedSessionView | null {
  const data = event.data as Record<string, unknown>;
  const nested = data.session as GuidedSessionView | undefined;
  if (nested?.session_id) return nested;
  const direct = data as unknown as GuidedSessionView;
  return direct.session_id ? direct : null;
}

function assistantEntry(session: GuidedSessionView): HistoryEntry | null {
  const turn = session.current_turn;
  return turn
    ? {
        key: `assistant:${turn.turn_id}`,
        role: "assistant",
        text: turn.assistant_display_text,
      }
    : null;
}

function appendUnique(current: HistoryEntry[], additions: HistoryEntry[]): HistoryEntry[] {
  const keys = new Set(current.map((entry) => entry.key));
  return [...current, ...additions.filter((entry) => !keys.has(entry.key))];
}

function wordColor(word: GuidedRecognitionWord): string {
  if (word.color_band === "red") return "#ff4d5e";
  if (word.color_band === "orange") return "#ff9f43";
  return "#ffffff";
}

/** Reference UI only; copy its state contract into the team's existing frontend. */
export function GuidedConversationPanel({
  room,
  initialSession,
  onShowReport,
  onReplayAll,
}: Props) {
  const initialAssistant = assistantEntry(initialSession);
  const [session, setSession] = useState(initialSession);
  const [history, setHistory] = useState<HistoryEntry[]>(
    initialAssistant ? [initialAssistant] : [],
  );
  const [error, setError] = useState<string | null>(null);
  const turn = session.current_turn;

  useEffect(() => {
    const receive = (payload: Uint8Array, _participant: unknown, _kind: unknown, topic?: string) => {
      if (topic !== "guided.events") return;
      try {
        const event = JSON.parse(new TextDecoder().decode(payload)) as GuidedEvent;
        const next = sessionFromEvent(event);
        const data = event.data as Record<string, unknown>;
        const feedback = data.conversation_turn as GuidedConversationTurnFeedback | undefined;
        const additions: HistoryEntry[] = [];
        if (feedback?.attempt_id) {
          additions.push({
            key: `learner:${feedback.attempt_id}`,
            role: "learner",
            text: feedback.learner_transcript,
            words: feedback.words,
          });
        }
        const reply = String(data.assistant_reply || "").trim();
        const shouldAddReply =
          reply &&
          (event.type !== "guided.turn_evaluated" ||
            Boolean(data.retry_recommended) ||
            next?.status !== "in_progress");
        if (shouldAddReply) {
          additions.push({
            key: `assistant-reply:${event.type}:${feedback?.attempt_id ?? additions.length}`,
            role: "assistant",
            text: reply,
          });
        }
        if (next) {
          setSession(next);
          const nextAssistant = assistantEntry(next);
          if (nextAssistant) additions.push(nextAssistant);
        }
        setHistory((current) => appendUnique(current, additions));
        if (event.type === "guided.error") setError("The line was not saved. Please retry.");
        if (event.type === "guided.session_closed") {
          void room.localParticipant.setMicrophoneEnabled(false);
          void room.disconnect();
        }
      } catch {
        setError("The conversation state could not be refreshed.");
      }
    };
    room.on(RoomEvent.DataReceived, receive);
    return () => {
      room.off(RoomEvent.DataReceived, receive);
    };
  }, [room]);

  const can = useMemo(() => new Set(session.allowed_actions), [session.allowed_actions]);
  const command = async (
    value: "retry" | "continue" | "replay" | "replay_slow" | "pause" | "resume" | "stop",
  ) => {
    setError(null);
    await sendGuidedCommand(room, value);
  };

  const finished = session.status === "completed" || session.status === "stopped";
  return (
    <section aria-labelledby="guided-title">
      <header>
        <p>{session.domain_title}</p>
        <h2 id="guided-title">{session.scenario_title}</h2>
        <p>
          {session.completed_turns} of {session.total_turns} lines completed
        </p>
      </header>

      <div aria-label="Complete conversation history" aria-live="polite">
        {history.map((entry) => (
          <article key={entry.key} data-role={entry.role}>
            <strong>{entry.role === "assistant" ? "Tutor" : "You"}</strong>
            <p>
              {entry.words?.length
                ? entry.words.map((word, index) => (
                    <span
                      key={`${entry.key}:${index}`}
                      title={
                        word.recognition_confidence_percent === null
                          ? "Recognition confidence unavailable"
                          : `Recognition confidence: ${word.recognition_confidence_percent}%`
                      }
                      style={{ color: wordColor(word), marginRight: 4 }}
                    >
                      {word.text}
                    </span>
                  ))
                : entry.text}
            </p>
          </article>
        ))}
      </div>

      {turn && (
        <div aria-live="polite">
          <strong>Your next line</strong>
          <p>{turn.learner_display_text}</p>
          {turn.arabic_hint && <p lang="ar">{turn.arabic_hint}</p>}
        </div>
      )}

      {!finished && (
        <div aria-label="Conversation controls">
          <button type="button" disabled={!can.has("replay")} onClick={() => command("replay")}>
            Replay character
          </button>
          <button
            type="button"
            disabled={!can.has("replay_slow")}
            onClick={() => command("replay_slow")}
          >
            Play slowly
          </button>
          <button type="button" disabled={!can.has("retry")} onClick={() => command("retry")}>
            Retry my line
          </button>
          <button
            type="button"
            disabled={!can.has("continue")}
            onClick={() => command("continue")}
          >
            Continue
          </button>
          <button type="button" disabled={!can.has("pause")} onClick={() => command("pause")}>
            Pause
          </button>
          <button type="button" disabled={!can.has("resume")} onClick={() => command("resume")}>
            Resume
          </button>
          <button type="button" disabled={!can.has("stop")} onClick={() => command("stop")}>
            Stop conversation
          </button>
        </div>
      )}

      {finished && (
        <div>
          <button type="button" onClick={() => void onReplayAll(session.session_id)}>
            Replay full conversation
          </button>
          <button type="button" onClick={() => onShowReport(session.session_id)}>
            View result
          </button>
        </div>
      )}
      <p>Word colors show STT recognition confidence, not pronunciation accuracy.</p>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
