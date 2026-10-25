# Chat loading, private card views and change notifications

Signed-in startup now reads the session first and opens Chats with one bootstrap
answer containing context, the first 90 dialogs and 40 messages. A late bootstrap
cannot replace a newer search/filter selection. Existing clients and APIs remain
compatible; protocol 2 is advertised by authenticated session responses.

History reads use a stored count maintained transactionally on message inserts,
superseding, moves and deletes. Existing counts are backfilled once. A matching
chronological index preserves microsecond timestamps, CRM numeric ordering and
the ID tie-breaker. Default API pages remain 20; the new chat requests 40 and
starts upward preloading up to 1.5 screens before reaching the loaded boundary.

List invalidation uses workspace-scoped committed versions instead of repeatedly
aggregating and hashing every dialog. Relevant CRM, connection, queue and contact
changes invalidate the view. Credential resealing and job-lease renewal do not.
Deferred triggers acquire the version row at commit, after ordinary writes.

The right card requests a compact prepared view. Balances, subscriptions, group
relationships, previous/next lessons and readiness flags stay available. Raw
yearly lesson arrays remain in PostgreSQL and in the existing full-detail API.
The private server cache is actor/workspace/conversation/revision scoped, expires
after 60 seconds and is bounded by 96 entries and 8 MiB of encoded payloads.
Authorization and the original contact link are checked before cache hits.
Unchanged card versions return a small `not_modified` answer; footer/version
changes do not rebuild the card body or discard the selected child.

One same-origin EventSource sends only committed revision and write-access hints.
Each short read rechecks the session and message-section permission, then returns
its database connection before the idle wait. Four streams per session/workspace
are allowed per API process. Heartbeats, a first-frame deadline, permission loss,
reconnection, notification catchup and quiet polling fallback are handled. A
healthy stream replaces frequent empty polling with a 45-second safety refresh.
The AlfaCRM ingestion and 30-minute card preparation remain server processes.

Lists over 240 loaded dialogs keep visible rows plus overscan in the DOM. Row
heights, explicit scroll anchors and full data/order are retained. Protocol 2
turns off the old intrinsic offscreen placeholders so they cannot conflict with
the measured window. Search, unread priority, pagination, mobile swipes and the
shared card/message shadows are preserved.

Schema `0017_chat_read_versions` adds only derived metadata and an index. It is
reversible and does not delete or rewrite source messages. Migration and downgrade
acquire the complete table-lock set in short, rollback-safe attempts before
changing anything. A busy database can abort safely for a guarded deploy retry.
The guarded deployment still verifies its backup and recreates only the API.

Validation:

- Backend suite: 329 tests, 327 passed and 2 existing concurrency tests skipped
  by the embedded PostgreSQL harness. Counter rollback, migration downgrade/
  upgrade, filtered bootstrap, bounded caches and event authorization are covered.
- Frontend suite: all 147 tests passed, including bootstrap/filter races,
  compact-card revisions, stream catchup/fallback and measured list windows.
- Native Chromium EventSource tests pass at 390px and 1440px: one initial
  bootstrap without redundant context/history reads; 1500 loaded dialogs use
  58/60 rendered rows; list and history anchor drift is below 1px; all 162
  messages survive paging, streamed delivery and transport fallback. The card
  retains focus and scroll, with a 98-byte unchanged response in this fixture.
- Light/dark screenshots were reviewed. Legacy history-scroll, list and
  subscription-card browser regressions also pass.

Assets: cabinet.css 2.23.0, chat.js 0.15.0, app.js 0.14.0.
