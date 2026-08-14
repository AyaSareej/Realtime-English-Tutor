# Debugging Guided Results

Release 0.7.0 separates the result shown to a learner from the evidence needed by a
developer. This prevents low-level ASR/timing details from being mistaken for a judgment about
the learner.

## Learner route

```http
GET /v1/practice-sessions/{session_id}/result?mode=guided
Authorization: Bearer <ASSESSMENT_SERVICE_TOKEN>
```

The normal route contains only the coaching DTO: speaking-flow score, three skills, strength,
next step, optional real pronunciation tips, completion, and replay/practise-again actions. It
never contains `result_debug`, evidence confidence, delivery internals, raw timings, versions,
or the replay script.

The BFF may proxy this route to the authenticated learner. It must keep the service token
server-side.

## Admin-only debug route

```http
GET /v1/admin/guided-conversations/sessions/{session_id}/debug-report
Authorization: Bearer <ASSESSMENT_ADMIN_TOKEN>
```

The middleware requires the separate admin token for every `/v1/admin` route. Do not expose this
credential or response to the browser. Use the route for support, calibration, and system
investigation.

From PowerShell:

```powershell
$adminToken = (Get-Content .env |
  Where-Object { $_ -like "ASSESSMENT_ADMIN_TOKEN=*" } |
  Select-Object -First 1).Split("=", 2)[1]

$sessionId = "guided-REPLACE-ME"
$debug = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8080/v1/admin/guided-conversations/sessions/$sessionId/debug-report" `
  -Headers @{ Authorization = "Bearer $adminToken" }

$debug.result_debug.summary
$debug.result_debug.thresholds
$debug.result_debug.lines |
  Select-Object line_number, eligible, timed_word_count,
    response_duration_seconds, timing_source, asr_confidence_percent,
    insufficiency_reasons |
  Format-Table -Wrap
```

## How to read the diagnostics

Release 0.7.0 preserves the guided short-line gates from 0.6.0:

- a line needs at least two timed words and 0.8 seconds of timed evidence;
- a session needs at least three eligible lines; and
- it then needs either five eligible lines or eight seconds of eligible learner speech.

Free conversation and placement-assessment thresholds are unchanged.

| Debug reason | Meaning | Check |
| --- | --- | --- |
| Word-level timestamps were unavailable | STT returned text but no usable word timing | Verify the worker uses Deepgram Flux and inspect provider events |
| Fewer than 2 timed words | Too few recognized words had timings | Check clipped audio or dropped words |
| Less than 0.8 seconds | First-to-last timed-word span was too short | Inspect word start/end values, not wall-clock turn time |
| Explicit audio issue | The turn was deliberately marked unusable | Inspect the stored audio-issue reason |

Evidence confidence describes sufficiency of timing evidence. It is not pronunciation confidence,
not a probability that the score is correct, and not useful learner-facing feedback. ASR word
colors likewise show recognition confidence and are not pronunciation grades.

## Piper diagnostics

```powershell
.\.venv\Scripts\python.exe tools\piper_preflight.py
```

The command loads the configured model and performs real synthesis. If the model or config is
missing, rerun `scripts\setup_piper.ps1` while online. With `PIPER_REQUIRED=true`, the missing
voice also appears in `/health/ready`.

The replay endpoint requires a completed scenario. HTTP 409 means the session is not complete;
HTTP 503 means Piper is unavailable or local synthesis failed. Worker logs use
`provider=piper-local` for guided speech. Free/assessment speech continues to use its configured
online voice.

## Verify completion shutdown

After the closing line, the expected order is:

1. `guided.completed`
2. final Piper assistant speech finishes
3. `guided.session_closed`
4. room disconnect / job shutdown

The per-room agent session must stop accepting speech. The `run_tutor.ps1` terminal remains
running because it is the shared worker for future learners.
