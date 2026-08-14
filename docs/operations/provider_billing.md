# Provider limits and pilot-testing cost

Prices and account rules change. The values below were checked on 2026-08-07;
verify the linked provider pages before production budgeting.

## Gemini evaluator

The 0.3.0 default is:

```env
EVALUATOR_PROVIDER=gemini
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_API_VERSION=v1beta
```

Google's paid Standard prices are:

- Gemini 2.5 Flash-Lite: USD 0.10 per one million text input tokens and
  USD 0.40 per one million output tokens.
- Gemini 2.5 Flash: USD 0.30 per one million text input tokens and
  USD 2.50 per one million output tokens.

Google currently requires at least USD 10 of prepaid credit when a new account
moves from Free Tier to Paid Tier. Actual free limits are assigned by project
and model; the authoritative limit for a failed request is the quota identifier
and value inside its HTTP 429 response.

This assessment normally makes one evaluator call for each scored main,
follow-up, or boundary response. Calibration is not sent to Gemini. A normal
A1-to-B2 ceiling run uses eight scoring calls; boundary tasks can increase that
to twelve. As an illustrative upper estimate, twelve calls with 4,000 input
tokens and 400 output tokens each cost about USD 0.0067 on Flash-Lite. Measure
real token usage before using that estimate in a budget.

Official references:

- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/billing
- https://ai.google.dev/gemini-api/docs/rate-limits

## Deepgram speech

Free conversation and assessment use Deepgram Flux English for streaming speech
recognition and Aura-2 for speech generation. Guided practice still uses Flux for
recognition, but release 0.7.0 uses the pre-downloaded local Piper voice for character
lines and replay. Current pay-as-you-go prices are:

- Flux English streaming STT: USD 0.0065 per audio minute.
- Aura-2 TTS: USD 0.030 per 1,000 input characters.

Piper has no per-character provider charge. Budget local CPU/memory and review the selected
voice model's license. LiveKit and Deepgram STT charges still apply to a live guided session.

Deepgram currently advertises USD 200 of free credit for new pay-as-you-go
accounts. The single `APITimeoutError` in the supplied log does not prove that
credits were exhausted; it is a transport/synthesis timeout. Release 0.3.0
raises the assessment-only TTS timeout to twenty seconds and allows four retry
attempts.

Official reference:

- https://deepgram.com/pricing

## LiveKit

Local `lk agent dev` testing does not require buying a LiveKit deployment plan.
If the agent is deployed to LiveKit Cloud, include agent session minutes,
WebRTC transport, inference, observability, and optional noise suppression in
the application budget. The Build plan currently starts at USD 0 and includes
an allowance; production plans and overages are listed on LiveKit's pricing
page.

Official reference:

- https://livekit.io/pricing
