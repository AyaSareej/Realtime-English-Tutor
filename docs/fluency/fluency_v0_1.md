# Shared Fluency Measurement - `fluency-v0.1`

Status: MVP engineering baseline  
Applies to: controlled assessment, guided fixed-scenario practice, and free conversation  
Release: 0.7.0

## Construct and scope

This module measures **utterance delivery fluency**: the learner's ability to
maintain connected speech at a functional pace without excessive disruptive
pauses or repeated breakdowns. It deliberately does not measure grammar,
pronunciation accuracy, accent, vocabulary range, task achievement, or general
English proficiency.

The implementation is aligned with the CEFR fluency construct, but its numeric
thresholds and weights are provisional engineering settings. They are not
official CEFR cut scores and have not yet been calibrated against a representative
human-rated sample.

## Why the MVP uses rules rather than a trained model

The application does not yet have a sufficiently large, representative set of
human-rated spontaneous responses from its target users. Training a model before
those labels exist would either learn from a mismatched dataset or create a score
whose meaning could not be defended. `fluency-v0.1` therefore uses transparent
features and versioned rules while retaining the feature vectors needed for later
calibration.

After launch, a separate consented pilot can collect human ratings and, where
approved, encrypted recordings. A later `fluency-v0.2` may replace the rules with
an ordinal regression, gradient-boosted model, or another calibrator only if it
outperforms `v0.1` on speaker-disjoint human-rated validation data.

## Data flow

```text
Learner microphone
    -> Deepgram Flux
        -> committed transcript + word start/end timestamps
            -> shared feature extractor
                -> fluency-v0.1 rule scorer
                    -> per-turn observation
                        -> session aggregator
                            -> learner-safe API result
```

The response window begins at the first learner word and ends at the last learner
word. The scorer excludes tutor speech, LLM latency, TTS latency, network delay,
endpointing delay, and silence after the learner has finished.

When Flux commits one assessment answer in several fragments, the adapter places
each fragment on the original wall-clock response timeline. It does not insert a
synthetic fixed pause.

## Extracted features

Let `start_i` and `end_i` be the timestamp of word `i`. An inter-word gap is:

```text
gap_i = start_i - end_(i-1)
```

The default qualifying pause is greater than `0.50 s`; a long pause is greater
than `1.50 s`.

### Speed

- Speech rate: timed words divided by full response minutes.
- Articulation rate: timed words divided by response time after qualifying
  pauses are removed.
- Pace stability: variation between speech runs when enough words exist.

Speed has a functional plateau. Speaking faster than the plateau does not keep
raising the score, and extremely fast speech lowers the speed subscore.

### Breakdown

- Qualifying pause count and pauses per minute.
- Total qualifying pause duration.
- Pause ratio: qualifying pause duration divided by response duration.
- Phonation ratio: non-qualifying-pause time divided by response duration.
- Long-pause count, long pauses per minute, and maximum inter-word gap.

### Continuity

- Mean number of words between qualifying pauses.
- Longest run in words.
- Whether the learner completed the turn.
- Amount of explicit assistance used.

### Repair

- Conservative filler count (`uh`, `um`, `erm`, and similar forms).
- Immediate repeated words.
- Repeated two- to four-word phrases.
- Explicit self-correction markers such as `I mean` or `let me rephrase`.

The common content word `like` is not automatically treated as a filler.
Self-correction has a small penalty because a successful repair can show control.

## Score

Each subscore is bounded from 0 to 100. The turn index is:

```text
Fluency Index =
    0.30 * Speed
  + 0.40 * Breakdown
  + 0.20 * Continuity
  + 0.10 * Repair
```

The output is called an **index**, never a percentage or probability.

The piecewise functions behind the four subscores are implemented in
`services/fluency/scorer.py`. Their exact source code and the scorer version are
stored with every result. Changing any threshold, function, weight, or eligibility
rule requires a new scorer version.

## Evidence eligibility and confidence

A free-conversation or assessment turn is eligible only when it contains:

- word-level timestamps;
- at least five timed words; and
- at least 2.5 seconds from first to last timed word.

Short conversational replies such as "Yes, I agree" are retained as total turns
but do not receive an individual score and do not lower the session score.

Controlled assessment requires at least two eligible responses and 12 seconds of
learner speech. Free practice requires at least three eligible turns and either
five eligible turns total or 30 seconds of learner speech.

Guided practice contains short predetermined lines, so release 0.7.0 applies a
mode-specific gate: at least two timed words and 0.8 seconds per line. Its session
requires at least three eligible lines and either five eligible lines total or
eight seconds of eligible learner speech. This correction prevents a clear short
script line from being rejected merely because it is not long enough for a free
conversation turn. The admin debug report exposes every line's evidence and rejection reasons
under `result_debug`; the learner result does not.

High session confidence requires broad timestamped evidence. The default is eight
eligible turns and 60 seconds for conversation, or six eligible responses and 45
seconds for assessment, with at least 90% timestamp coverage. A scored session
below that amount receives medium confidence. An unscored session receives low
confidence and explicit insufficiency reasons.

ASR confidence is never used as learner proficiency evidence. It may be retained
separately for technical validity monitoring.

## Mode-specific output

| Mode | Learner output | CEFR fluency label |
|---|---|---|
| Controlled assessment | Index, confidence, evidence, feedback, four subscores | Yes, after session evidence is sufficient |
| Guided conversation | Speaking-flow score, Pace, Smoothness, Connected Speech, one strength, one next step | No |
| Free conversation | Rolling session index, confidence, evidence, feedback | No |

The assessment uses provisional absolute anchors of 30, 45, 60, and 75 for A1,
A2, B1, and B2 fluency. These anchors are internal versioned hypotheses, not
official CEFR thresholds. They must be calibrated after a human-rated pilot.

Within a target-level assessment task, the index is mapped to the existing 0-4
rubric: a score at the target anchor is 3 (meets the target); at least 12 points
above is 4; within 12 below is 2; within 24 below is 1; otherwise 0. The application
code, not the LLM, owns this fluency score when timestamp evidence is sufficient.

If the provider omits usable word timings, the system reports insufficient fluency
evidence. During the MVP only, the task evaluator's descriptor judgment may remain
as a clearly marked fallback contribution so the other assessment dimensions are
not discarded. The final feature-based fluency profile remains "Not determined"
until enough timed evidence exists.

## API contract

Free conversation submits each committed learner turn through:

```http
POST /v1/fluency/sessions/{session_id}/turns
Authorization: Bearer <service token>
Content-Type: application/json
```

```json
{
  "session_id": "room-123",
  "turn_id": "turn-7",
  "mode": "free",
  "transcript": "I chose the train because it is faster.",
  "words": [
    {"word": "I", "start": 0.0, "end": 0.18, "confidence": 0.97}
  ],
  "completed": true,
  "assistance_count": 0
}
```

The response contains both the per-turn observation and the rolling session result.
Guided practice uses the same extractor and scorer inside the deterministic
scenario service and exposes the aggregate at
`GET /v1/practice-sessions/{id}/result?mode=guided`; its browser does not submit
raw fluency observations directly.
Retrieve the authoritative rolling result with:

```http
GET /v1/fluency/sessions/{session_id}?mode=free
```

The browser should call the team's application backend, which maps this internal
snake-case contract into the learner-safe TypeScript view. Provider credentials
and the service token remain server-side.

## Privacy and future calibration

The `fluency_observations` table stores derived result JSON, session/turn IDs,
mode, and version. It does not store the submitted transcript or raw audio.

Training-data collection is a separate product decision. It requires:

- explicit informed consent;
- a defined retention and deletion policy;
- encrypted recording storage;
- two independent trained raters where feasible;
- adjudication of rating disagreements;
- speaker-disjoint train/validation/test splits; and
- representation of Arabic-speaking learners, accents, levels, tasks, devices,
  and realistic noise conditions.

The first development pilot may be small, but no formal validity or fairness claim
should be made until the sample is large and representative enough for that claim.

## Limitations

- Flux timestamps are ASR-derived approximations, not forced-alignment ground truth.
- Word-based rates differ from the syllable-based rates used in some research.
- Pause placement inside versus between syntactic units is not modeled in v0.1.
- Prosody, pitch, stress, and intonation are not scored.
- Task difficulty affects fluency; practice trends should compare similar task
  types and contexts whenever possible.
- Guided results compare oral-reading delivery and must not be presented as
  unrestricted speaking ability.
- Arabic accent is not a fluency error, but ASR errors can reduce usable timing
  evidence. Low confidence should lead to more evidence, not a lower learner score.
- The weights, piecewise bands, evidence gates, and CEFR anchors remain uncalibrated.

## Primary references

1. Council of Europe. *Common European Framework of Reference for Languages:
   Learning, Teaching, Assessment - Companion Volume* (2020).
   https://rm.coe.int/common-european-framework-of-reference-for-languages-learning-teaching/16809ea0d4
2. Tavakoli, P. "Assessment of second language fluency." *Language Teaching*,
   58(3), 312-328. https://doi.org/10.1017/S0261444824000417
3. Deepgram. *Getting Started with Flux* and *Migrating from Nova-3 to Flux*.
   https://developers.deepgram.com/docs/flux/quickstart
4. LiveKit. *Text and transcriptions* and *Pipeline nodes and hooks*.
   https://docs.livekit.io/agents/multimodality/text/
5. Zechner, K. et al. "Automated scoring of speaking items in an assessment for
   teachers of English as a foreign language." ACL Workshop, 2014.
   https://aclanthology.org/W14-1816/
