# GitHub deployment controller for the existing VPS

**State:** source code prepared, **not installed or enabled on production by this commit**.

The active production deployment is Docker Compose project `center-messenger` at
`/opt/center-messenger-github`. Public URL: `https://messenger.rastudio.org`.
Database: `center-messenger_database`; expected Alembic revision:
`0012_member_sections`. The earlier GitVerse image remains a rollback option.

## Design

- `scripts/github_promote_vps.py` accepts a **local artifact** and its exact approved 40-character Git commit SHA. It checks all three checksums, parses the Docker image archive and compares the image configuration digest against `release.json`, validates repository, branch, format, platform, revision and compose bytes. It refuses to run if the existing API or DB is not ready, the legacy deploy agents are running, or the database schema changes.
- Before replacing the API it creates a pg_dump custom-format backup in `/opt/center-messenger-github/rollback/`. Backup failures are fatal. **Verify external backup, retention and restore procedure separately.** Database migrations are deliberately unsupported: images requiring a different schema must be reviewed and migrated manually.
- Recreates **only the API** using `docker compose -p center-messenger ... up -d --no-deps --no-build --force-recreate api`, verifies local and public readiness, and attempts an API-only rollback on failure. Rollback of schema, client-side problems, or external integrations is **not** guaranteed.
- `scripts/github_poll_vps.py` checks only after an **explicit local opt-in** (`autodeploy.json` containing `enabled: true`). It selects the newest successful `package-release.yml` run on main, requires `verify.yml` success for the same SHA, downloads the exact named artifact with `gh`, invokes the guarded promoter, and stores last successful/failed SHA. A failed SHA is held for manual investigation rather than retried indefinitely.

## Installation, only after production functional testing

Review these files and test `pg_dump -U cm_owner -d center_messenger -Fc`
on the active DB before enabling. The controller assumes the existing GitHub CLI login on the server can read Actions artifacts and that `gh`, `docker`, `python3`, `systemctl` are installed. Minimize privileges on CLI credentials. The poller uses root because Docker access effectively confers root privileges.

Download each file **from a pinned reviewed commit**, never piping unreviewed main into a privileged shell. Example once a final reviewed SHA is chosen:

```sh
install -m 700 scripts/github_promote_vps.py /opt/center-messenger-github/github_promote_vps.py
install -m 700 scripts/github_poll_vps.py /opt/center-messenger-github/github_poll_vps.py
python3 -m py_compile /opt/center-messenger-github/github_*.py
```

Create `/opt/center-messenger-github/autodeploy.json`:

```json
{"enabled": false}
```

Make the file root-owned mode 0600. Install systemd units from
`deploy/systemd/` into `/etc/systemd/system/`, run `systemctl daemon-reload`.
The timer may be enabled while state stays disabled, but **do not enable updates**
until checkout, GitHub Actions validation, test restore, client-flow smoke tests,
and rollback rehearsal have succeeded.

**For the existing GitHub release** `aff2da16426cb4fcc2c5a62f9a16d7aad5b62a54`,
its source is already deployed. Do not unnecessarily redeploy it. Set
`last_deployed_sha` to that full SHA when opting in, and only after
confirming the currently running image.

Example approval after validation:

```json
{
  "enabled": true,
  "last_deployed_sha": "aff2da16426cb4fcc2c5a62f9a16d7aad5b62a54"
}
```

The GitHub workflow packages **every push to main**. To avoid inadvertent
production releases, protect main, require mandatory reviews and passing checks,
or change packaging to a protected release branch. There is no authenticated
artifact signature beyond trusted GitHub Actions provenance and on-disk checksums.
Do not treat a public artifact or SHA-256 checksum alone as proof of origin.

Safety: no `docker compose down`, no volume deletion or pruning; do not
restart Postgres. Keep old Docker images and DB backups. Confirm application
authorization, dialogs, messaging, CRM and MAX integration separately.
