# Telephone AI and explicit night archive

The telephone worker now stores resumable ASR fragments and descriptions made
by the independent telephone agent. An explicit start/pause control scans from
2020-10-31 up to calls strictly older than seven days, only 00:00–08:00 Moscow,
with persistent 120-second whole-call pacing. New-call automation is separate
and both controls default off. Ordinary import is limited to the latest week.

Shared compatibility changes are deliberate: additive schema 0032, FFmpeg in the
common image, new readiness tables and the reviewed three-process transition.
The native MAX/browser/send/history implementations are unchanged. The external
policy adds the new modules/tests and preserves all previous behaviour gates.
The existing local-build service remains the only deployment owner; an exact
preparation command admits all three old pins. Rollback retains call facts,
operation receipts and partial/manual results; it does not restore an old dump.

Validation in the development executor:

* Targeted Python tests: agent HTTP/CSRF, worker/long stereo audio, media, source
  pages/night boundaries and ordinary call import pass.
* Guard tests: 27 passed. Existing MAX runtime tests: 30 passed. Telephony runtime
  tests: 19 passed, including a crash after phone metadata was committed.
* JavaScript tests cover call/audio preservation, lazy transcript, dedicated
  agent settings, night start/pause and the 1–7-day ordinary loader boundary.
* Compilation, protected file hashes and `git diff --check` pass.
* The complete pinned offline suite discovers 275 tests. In this executor it
  cannot pass: 11 existing browser tests skip and one existing real Chromium
  test errors because the browser executable is unavailable. The mandatory
  candidate-image gate must run all 275 with no skips on the server.
* The PostgreSQL suite includes 14 new receipt/RLS/cursor/rollback tests (98
  total). No local PostgreSQL server is present; these are NOT reported as
  passed. The isolated server gate is mandatory before DDL/promotion.

No real paid ASR was invoked by these tests. The previously observed 206 audio
fragment confirms only bounded source access. After installation, first verify
a single real recent call and its complete text before enabling a paid batch.
See [the operation and rollout contract](../operations/telephony-ai-night.md).
