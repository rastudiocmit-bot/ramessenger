# Chat refresh and client card performance

Slow dialog-list requests no longer hold live message updates. One refresh clock
starts the two feeds independently, with at most one pending poll per feed and
separate eight-second retry delays. The existing per-conversation history queue
continues to serialize archive pagination, live cursors and send confirmation.
Chat reads have a 20-second deadline; switching clients, leaving Chats or resetting
the workspace cancels the previous timeline request. Drafts and send requests are
retained.

The timeline now patches messages and day markers by ID. Unchanged bubbles stay
in place, including expanded technical notices; edits, removals, grouping and
timestamp changes still update their affected rows. Metadata-only changes update
the controls without rebuilding the timeline. Date formatters are reused per
workspace timezone, and scrolling keeps the visible message as its anchor.

The client card updates its footer separately from its facts. Unchanged facts and
child buttons retain their DOM, focused child and scroll position. Concurrent
opens share a pending card read, and a hidden document does not poll card facts.
CRM preparation and the existing 30-minute server refresh remain server-owned.

The outer family dialog no longer adds its generic modal shadow to the inner
panel's shared surface shadow. Stylesheet version: 2.22.0; chat script: 0.14.0.

Validation:

- 138 UI tests pass, including ten new regressions for slow independent feeds,
  separate error recovery, cancellation, retained message nodes, ordering, card
  freshness, child selection and timezone changes.
- Chromium archive-scroll, dialog-list and subscription-card checks pass.
  The 201-message wheel test retains all IDs, with 0px older-page anchor drift.
- Phone and desktop card checks in both themes retain focus and scroll while
  changing freshness, and preserve scroll when the selected balance changes.
  Only the inner panel casts a shadow; its computed value matches the chat header.
- A local synthetic 1000-message timeline test edits one message and adds another:
  existing bubbles retained: 0 -> 999; elements created: 5810 -> 14;
  observed update time: 545.7ms -> 183.5ms. This is a fixture-based browser
  comparison, not a production API or CRM latency measurement.

No database migration or server import/history policy changes are required.
