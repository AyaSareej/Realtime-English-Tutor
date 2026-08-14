/**
 * Browser-side guided conversation contract.
 *
 * The browser obtains its LiveKit token and guided-session bootstrap data from
 * the application BFF. It never receives ASSESSMENT_SERVICE_TOKEN and never calls
 * the Python practice service directly.
 */

import { Room, RoomEvent, Track } from "livekit-client";

export type GuidedState =
  | "assistant_speaking"
  | "user_prompt_visible"
  | "awaiting_retry_decision"
  | "paused"
  | "completed"
  | "stopped";

export interface GuidedTurn {
  turn_id: string;
  turn_number: number;
  total_turns: number;
  assistant_display_text: string;
  assistant_spoken_text: string;
  learner_display_text: string;
  arabic_hint?: string | null;
}

export interface GuidedSessionView {
  session_id: string;
  status: "in_progress" | "completed" | "stopped";
  state: GuidedState;
  scenario_id: string;
  scenario_version: number;
  domain_id: string;
  domain_title: string;
  scenario_title: string;
  scenario_level: "A1" | "A2" | "B1" | "B2";
  current_turn: GuidedTurn | null;
  completed_turns: number;
  total_turns: number;
  retries: number;
  recording_consent: boolean;
  allowed_actions: string[];
}

export interface GuidedBootstrap {
  practice_session_id: string;
  mode: "guided";
  room_name: string;
  server_url: string;
  participant_token: string;
  result_url: string;
  guided_session: GuidedSessionView;
}

export interface GuidedLearnerSkill {
  key: "pace" | "smoothness" | "connected_speech";
  label: string;
  score: number;
  rating: "strong" | "good" | "keep_practising";
  message: string;
}

export interface GuidedLearnerResult {
  session_id: string;
  domain_title: string;
  scenario_title: string;
  scenario_level: "A1" | "A2" | "B1" | "B2";
  result_status: "ready" | "needs_more_speech" | "incomplete";
  headline: string;
  speaking_flow_score: number | null;
  completion: { completed_lines: number; total_lines: number; percent: number };
  skills: GuidedLearnerSkill[];
  strength: string | null;
  next_step: string;
  pronunciation_tips: string[];
  can_practise_again: boolean;
  replay_audio_url: string | null;
  practice_note: string;
}

export type GuidedEvent = {
  type: string;
  data: Record<string, unknown> | GuidedSessionView;
};

export interface GuidedRecognitionWord {
  text: string;
  recognition_confidence_percent: number | null;
  color_band: "red" | "orange" | "white";
}

export interface GuidedConversationTurnFeedback {
  attempt_id: string;
  turn_id: string;
  assistant_text: string;
  expected_learner_text: string;
  learner_transcript: string;
  words: GuidedRecognitionWord[];
  recognition_confidence_interpretation: string;
}

const encoder = new TextEncoder();
const decoder = new TextDecoder();

/** Connect after POST /api/guided-conversations/:scenarioId/start on your BFF. */
export async function connectGuidedRoom(
  room: Room,
  bootstrap: GuidedBootstrap,
  onEvent: (event: GuidedEvent) => void,
): Promise<void> {
  room.on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
    if (topic !== "guided.events") return;
    try {
      onEvent(JSON.parse(decoder.decode(payload)) as GuidedEvent);
    } catch {
      onEvent({ type: "guided.client_error", data: { code: "invalid_event" } });
    }
  });
  room.on(RoomEvent.TrackSubscribed, (track) => {
    if (track.kind === Track.Kind.Audio) {
      const audio = track.attach();
      audio.autoplay = true;
      audio.hidden = true;
      document.body.appendChild(audio);
    }
  });
  await room.connect(bootstrap.server_url, bootstrap.participant_token);
}

export async function sendGuidedCommand(
  room: Room,
  command:
    | "retry"
    | "continue"
    | "replay"
    | "replay_slow"
    | "pause"
    | "resume"
    | "stop",
): Promise<void> {
  if (command === "pause") {
    await room.localParticipant.setMicrophoneEnabled(false);
  }
  await room.localParticipant.publishData(
    encoder.encode(JSON.stringify({ command })),
    {
      reliable: true,
      topic: "guided.command",
    },
  );
  if (command === "resume") {
    await room.localParticipant.setMicrophoneEnabled(true);
  }
}

/** Play the Piper WAV returned by the team's authenticated BFF replay route. */
export async function playGuidedReplay(replayUrl: string, audio: HTMLAudioElement): Promise<void> {
  const response = await fetch(replayUrl, { credentials: "include" });
  if (!response.ok) throw new Error(`Guided replay failed with HTTP ${response.status}`);
  const objectUrl = URL.createObjectURL(await response.blob());
  audio.src = objectUrl;
  audio.onended = () => URL.revokeObjectURL(objectUrl);
  await audio.play();
}

// The team BFF may proxy the supplied service contracts as:
// GET  /api/guided-conversations/domains (each domain contains scenarios)
// GET  /api/guided-conversations/:scenarioId/preview
// POST /api/practice-sessions  body: { mode: "guided", scenario_id: ... }
// POST /api/guided-conversations/:sessionId/confidence
// GET  /api/practice-sessions/:sessionId/result?mode=guided
// GET  /api/guided-conversations/:sessionId/replay-audio (proxy Piper WAV)
// GET  /api/admin/guided-conversations/:sessionId/debug-report (staff only)
