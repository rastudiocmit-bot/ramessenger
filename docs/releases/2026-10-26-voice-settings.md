# Dedicated voice settings and configurable three-minute dictation

Connections now has four work areas: history monitor, service settings, voice
input and AI agents. Voice input holds the existing Yandex Cloud and DeepSeek
credentials, enable switch, monthly budget and recording duration. Agent editing
has an exclusive Yandex/DeepSeek provider choice; it filters models and restores
the correct provider when editing or restoring an agent version. The selected
model remains the single server-side source of provider routing. Existing keys,
agents, prompts and primary-agent selection are retained.

Audio recognition uses Yandex SpeechKit; DeepSeek processes recognised text.
Selecting a DeepSeek model never invokes a Yandex text model. SpeechKit's
connection test now checks recognition only, so a SpeechKit-only service account
does not need text-model permissions. Agent tests check the selected text model.
The voice summary shows the recogniser, duration and main text agent.

Migration `0018_voice_duration` adds `ai_settings.voice_max_seconds`, default 180,
with an integer constraint from 15 to 180 seconds. Settings omission preserves
the existing value. The chat context publishes the duration without credentials;
the recorder snapshots it for each attempt and stops at the exact sample limit.
The server checks the current setting independently. PCM and base64 request caps,
diagnostics and browser encoding now accommodate 180 seconds.

SpeechKit synchronous requests still respect its 30-second/1-MB limit. Long mono
PCM is partitioned into contiguous pieces of at most 30 seconds, preferring a
quiet boundary near the limit. Two provider requests run concurrently per job;
their text is combined in source order and passed once to the selected agent.
No samples overlap or disappear. A failed part produces an error rather than an
apparently complete partial transcript. Successful part costs are counted even
when another part fails; each part is billed with its own 15-second rounding.
The text output budget is increased for longer dictation.

The current single-worker API hosts a bounded in-memory queue: two jobs execute,
up to eight jobs are retained, and at most two belong to one user/workspace.
New clients submit background transcription and poll short authenticated HTTP
requests, avoiding the proxy's 30-second response deadline. Each read/cancel
checks current chat access and the job's user/workspace ownership. Jobs expire,
and cancellation/release drops results. Audio never goes to disk or the database.
An API restart loses an outstanding job; the browser keeps the clip for retry.
Shutdown drains running work before disposing the database engine. Already
started provider calls can finish and incur charges after cancellation.

Cancellation, logout, page hiding and chat switching fence late results. Voice
still edits the ordinary draft and never submits a message or shows a global
notification. Dictation longer than the 4000-character message limit is retained
in full, including when the browser's native text insertion would truncate it.
An inline hint blocks sending until it is shortened; no automatic splitting or
automatic messages are introduced.

Validation: 364 backend checks, 362 passed, two existing native-PostgreSQL
concurrency cases skipped in the embedded PostgreSQL harness. All migrations,
duration defaults/constraints and downgrade/upgrade preservation were exercised.
162 interface checks passed. Native Chromium microphone/retry/settings tests
passed; separate 1440 px and 390 px checks cover tab navigation, duration saving,
provider retention and page overflow. Synthetic 180-second PCM tests verify exact
recording boundaries, lossless chunking, result order, billing and cancellation.
Real three-minute audio was not sent to a paid provider during these checks.

Assets: cabinet.css 2.24.0, app.js 0.14.1, chat.js 0.15.3, voice.js 1.6.0,
ai.js 1.2.0. No history cursor, message identity or CRM loader changes.
