# Indexed identity checks for cached client cards

The ordinary chat context, list, history and prepared-card reads use PostgreSQL.
Opening a full card can enqueue independent CRM refresh jobs, but the request does
not wait for AlfaCRM HTTP calls. Background ingestion and its refresh schedules
remain unchanged.

Profiling exposed an expensive local database path: `cache_status` called
`card_preference.is_pending`, which generated the complete pending-family set just
to test one requested family. Each projected family also ran an identity guard
whose CASE expression prevented direct indexed contact lookups into the CRM
catalog. This also affected list reads and background pending-family planning.

Identity guards now use mutually exclusive UNION ALL paths: an indexed contact
lookup for ordinary cards, and indexed primary-phone/contact-cohort lookups for
aliases. Guard contents, ordering, branch availability, current publication,
settings version, manual links and CRM IDs keep the same meaning.

Single-card pending checks first find the requested contacts and persisted alias
candidates, then run the full identity guard only for those candidates. Candidate
payloads are hints, never authority. Stale guards fall back to the original
contact; manual links retain precedence. Malformed legacy source IDs are ignored
as candidates instead of causing a UUID conversion failure.

The isolated benchmark uses tenant RLS with `cm_app`, four CRM branches, 14965
source cards, 2776 dialogs and 55520 messages. All fixture dialogs need a reply,
deliberately exercising the worst case; this is not a claim about the live unread
count. The same synthetic profile measured:

| Read | Before | After |
| --- | ---: | ---: |
| Chat bootstrap | 0.57 s | 0.24 s |
| Prepared card, cached only | 9.55 s | 0.05 s |
| Card opening with local job preparation | 9.22 s | 0.44 s |

These are local embedded-PostgreSQL measurements, excluding real session
authentication, network setup and production load. The new standalone
`scripts/benchmark_cached_cards.py` repeats the fixture in a disposable local
schema and checks that cached-card reads stay within the 2.5-second query budget.
It refuses non-loopback database hosts and never constructs an external CRM
client. Fixture seeding temporarily skips only derived version-invalidation
triggers; source foreign keys, message counters and tenant policies stay enabled,
and invalidation triggers are restored before any measured read.

Responses now include `Server-Timing: app;dur=...`, measured until response headers.
API GET requests taking at least one second produce a `Slow API read` log with
only the route template, status and duration. It includes auth and database
checkout/read time, excludes an event stream's subsequent lifetime, and never
logs query parameters, customer IDs, message text or credentials. This allows a
slow authenticated request to be distinguished from browser/network delay.

Validation: 337 backend tests, 335 passed; two existing native-PG concurrency
checks skipped in the embedded harness. Regression cases cover repeated branch
observations, changed phones, malformed alias hints, manual-link precedence and
privacy of timing diagnostics. No frontend bundle, schema, messages or source
history cursors change.
