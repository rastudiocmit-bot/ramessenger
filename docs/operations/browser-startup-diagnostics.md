# Browser startup diagnostics

The reported symptom is a visible cabinet that ignores clicks for about 20 seconds after reload. It also occurs in an incognito window. The actual account-specific stall has not been reproduced or fixed by this change.

A synthetic authenticated startup with 5,000 conversation records took about 1.5 seconds under 4× CPU throttling. The browser remained responsive with overdue task markers. Holding the bootstrap request reproduces an empty list, but not a main-thread freeze; the existing 20-second bootstrap timeout is therefore not evidence of the reported cause.

After reproducing the stall, open **Profile → Help → Copy startup diagnostics** and copy the report. The Russian button is «Скопировать диагностику запуска». If clipboard permission is denied, the selected read-only text can be copied manually.

The recorder is local and bounded to 45 seconds after the app script starts. It sends no telemetry, writes no local/session storage and records no request/response bodies, credentials, raw URLs, workspace/client identifiers or conversation text. It exports public asset basenames, allowlisted API categories and numeric timing/DOM measurements. Reports only leave the browser when the user copies and shares one.

- Navigation and startup phase timestamps are relative to the current navigation.
- A late `app-script` phase indicates time spent before app initialization.
- A large `heartbeat_gap_ms` and long task indicate main-thread blockage.
- Long animation frames can include a public source script, function name, source character offset and layout time. An unavailable offset is zero.
- `navigation_hit` records whether a navigation button is hit-testable at sampled moments. It contains a tag name, not element text.
- Completed requests only: up to twelve slow resources, with normalized categories, queue/response timing and transfer size. An ongoing request may not yet appear.
- Up to eight long tasks, eight long frames, twelve phases and eight pointer delays are retained. DOM counts describe the page at report time.

Validation: 383 UI tests passed. The Chromium diagnostic check distinguishes a synthetic 350 ms session request from a 250 ms main-thread block, verifies identifier/secret redaction, explicit clipboard copying, manual-copy fallback and zero diagnostic network/storage writes. The native-SSE startup check times out at its existing first-history recovery assertion both on the unchanged base and on this candidate in the executor; it is not reported as passed. Protected MAX/telephony files and the installed guard policy are unchanged. Mandatory container checks remain with the existing VPS deployment owner.
