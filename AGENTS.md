# Concurrent work on Center Messenger

## Protected MAX / AlfaCRM connector

- For the container split read `docs/operations/max-container.md`. The native
  browser and its queues belong to the pinned `max-worker` image. An ordinary
  API release must never recreate that service or change `CM_MAX_IMAGE`.
  The initial transition is executed only by the existing pinned deploy owner,
  after an explicit exact-SHA plan. Never run a second deployment controller.
  New telephony/channel adapters publish through the shared application/event
  layer, not through the native MAX worker or direct writes to its queues.

- Read `docs/operations/max-release-protection.md` and the pinned file list in
  `deploy/max-protection.json` before changing messaging, dependencies or schema.
- Design, CRM-card, AI and future-channel tasks must not modify that contour.
  Do not regenerate the manifest to hide a diff. A MAX fix is a separate task:
  review exact changes, pass compatibility checks, obtain exact-SHA server
  admission and explicitly refresh the baseline after a verified release.
- Queues, message storage, identity, CRM publications and the shared loader also
  affect MAX. Keep their contracts compatible; never treat them as isolated UI
  changes. All candidate images pass the pinned MAX regression gate.
- The first explicitly published guard release installs it once through the
  existing deployment owner, before the API is changed. Subsequent guard and
  baseline updates require explicit server installation. It lives
  under `/etc/ramessenger-max-guard`, outside Git checkouts, and never updates
  itself from a candidate commit. Do not bypass/remove its systemd drop-in,
  edit its policy/approvals or invoke another runner to get a blocked build out.
- Future VK, OK and Telegram connectors have separate tabs and conversations.
  Optional per-client linked history is not an automatic global chat merge.


- This repository is `rastudiocmit-bot/ramessenger`. Do not change another
  project's SSH access, proxy, containers, networks, volumes or deployment files.
- Each independent task uses its own branch and checkout/worktree. Fetch current
  `main` before integration. Merge/rebase intervening work; do not overwrite it.
  Use a pull request or an atomic ref update with the observed expected SHA.
  Never force-push shared branches. A shared GitHub account is not a task lock.
- Changes to the same files can still conflict and need review before merging.
  Do not resolve a conflict by dropping another task's changes.
- Do not invoke GitHub Actions. Publish commits with `[skip ci]`. Run meaningful
  local checks and record their results; report existing unrelated failures.
- Production deployment has one owner: `ramessenger-local-build.service` on the
  VPS. Use `deploy <current-main-full-SHA>` through the project support account,
  or the existing server timer. A queued request is not a successful deployment.
- Do not run a second deploy script, edit the live checkout with rsync, prune
  global Docker resources, delete deploy/browser locks, kill another task's
  worker, or start an arbitrary branch in production.
- A stale build is discarded before promotion. Once promotion starts, migration,
  readiness verification and any rollback finish under the project deploy lock.
- Keep SSH private keys, cookies, tokens and server configuration out of Git.
  Each executor has its own actor/key. Keys in an ephemeral assistant workspace
  may need replacement; a permanent server account does not preserve that key.
- See `docs/operations/multi-agent-server-access.md` for installation, revocation,
  request states, the concurrency contract and the limits of project isolation.
