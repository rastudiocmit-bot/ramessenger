# Current CRM review findings

Successful child-ID repairs previously set `needs_review=true` on the saved
customer and family directory, without clearing that flag afterward. The card
then displayed the stored boolean, also accepting an old ambiguous-selection
hint. This could leave “Нужна проверка в CRM” visible after the underlying link
had been corrected. A missing optional birth date in one branch could also be
treated as a contradiction to a known date in another branch.

Review status now comes from current saved identity facts in the published CRM
snapshot. It checks contradictory known child names and birth dates for one
Customer ID, current child-ID collisions, conflicting representatives and the
existing primary-contact cohort rules. Missing optional values and phone
placeholders in name fields do not constitute contradictory names. Secondary
phone coincidence does not join families. Stable CRM IDs, manual links, contact
grouping and message recipients retain their existing meaning.

Prepared-card reads use bounded queries for the requested Customer IDs, related
primary contacts and current binding owners, scoped to the workspace, published
run, connection and available branches. They make no external CRM requests.
Oversized or incomplete input retains a review warning. A refused redirect to a
related contradictory candidate remains a generic finding without disclosing
another family's customer IDs or personal details. A stale redirect hint alone
cannot keep the warning active.

The existing background card scheduler also cleans derived directory/customer
flags in resumable batches of 100 contacts. It waits for an in-progress child-ID
repair, fences the publication and settings version, and repeats completed local
checks after 30 minutes. It changes flags only when their value differs. The
progress marker itself does not invalidate chat projections. This does not
restart source history scans or edit message contents, balances or identity
links.

The badge keeps its existing appearance. Its tooltip now explains current
findings and includes the affected visible AlfaCRM Customer IDs and branch IDs.
Resolved findings disappear on the next full card refresh. The precise live
pre-fix reason for the reported client's badge was not read from the production
database; this release fixes the confirmed source-code defect and exposes the
current reason rather than assuming that client's source records are clean.

Validation: 353 backend checks, 351 passed and two existing native PostgreSQL
concurrency checks skipped in the embedded harness; 153 frontend checks passed.
Native Chromium checks passed at 1440 px and 390 px, covering a reason tooltip,
removal after refresh and preservation of the client status and balance. The
existing isolated benchmark with 14965 CRM records, 2776 dialogs and 55520
messages measured a prepared-card read at about 51 ms. This synthetic result
excludes production authentication, network delay and production load.

No schema migration. `chat.js` is version 0.15.2; other asset versions stay the
same. Existing sessions obtain the updated script after a page reload.
