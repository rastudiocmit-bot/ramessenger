# Concurrent work on Center Messenger

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
