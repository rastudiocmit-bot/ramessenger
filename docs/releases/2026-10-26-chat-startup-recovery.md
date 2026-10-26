# Reloaded chat startup and database capacity

A timeline error previously discarded the entire bootstrap, including its already
authorized context and dialog list. The first selection also displayed a history
error without retaining it in the history retry state, so a successful list read
could clear the warning while the timeline remained unavailable.

Bootstrap now preserves its completed list after a database failure in the first
timeline transaction and marks history pending. The browser displays the list,
then retries the timeline independently. A failed bootstrap can fall back once to
the existing context/list endpoints for transient transport/service errors or an
unavailable bootstrap route. Authorization errors are not startup fallbacks.
An unloaded timeline or failed read keeps the recovery polling cadence even when
the event stream is healthy. A failed first history read retains its own error
and eight-second backoff until that feed recovers. First recovery opens the latest
page at the bottom. The empty-list explanation stays hidden during initial load.

History metadata and notification reads now reuse the same transactional queue
projection already used by the dialog list. Missing or older projections fall
back to the live policy. Sending and marking handled continue to lock and
recompute live policy; a stale cached checkpoint cannot authorize handling.
Source messages, chronology, CRM identities and unread policy are unchanged.

The API's shared database pool previously allowed only three concurrent
connections for HTTP reads and up to six background workers. It now keeps six
connections and allows two overflow connections, retaining the three-second pool
wait and existing statement timeout. Short transactional queries disable optional
JIT compilation per application connection. No database-global settings or schema
migrations are required.

Validation:

- Backend: 334 tests, 332 passed; two existing native PostgreSQL concurrency
  checks skipped by the embedded database harness.
- Frontend: all 152 tests passed, including failed/partial bootstrap, forbidden
  startup, first-history backoff, independent recovery and healthy-stream retry.
- Native Chromium at 1440px and 390px: bootstrap 503 plus first-history 503
  recover without reloading; partial bootstrap displays 40 dialogs before its
  deferred timeline; all four fixture messages appear, with no writes or popups.
- Existing native EventSource/scroll/card regression passed at both widths:
  1500 dialogs, bounded row rendering, 162 retained messages, subpixel scroll
  anchors, stable card focus/scroll and working polling fallback.
- An isolated synthetic fixture with 4002 CRM cards and 1001 messages in one
  conversation, under `cm_app` with tenant RLS, returned equivalent live/cached
  queue facts. Metadata reads measured 68.56ms live and 12.44ms cached. These are
  local embedded-database measurements, not production latency. This harness
  has no native JIT implementation; no production JIT timing is claimed.

The reported production SQL exception cannot be identified from public readiness
alone. Authenticated reload recovery is covered by the browser fixtures; public
post-deploy checks verify the installed build, assets and readiness separately.

Schema remains `0017_chat_read_versions`; chat.js is `0.15.1`.
