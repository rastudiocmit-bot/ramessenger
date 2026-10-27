#!/usr/bin/env python3
"""Guarded, manually approved promotion of a verified GitHub release bundle.

Never manages PostgreSQL containers, performs migrations, or changes Nginx.
No unattended promotion without an explicitly supplied artifact directory.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
import urllib.request
import urllib.error

REPO = "rastudiocmit-bot/ramessenger"
ROOT = Path("/opt/center-messenger-github")
EXPECTED_DB_VOLUME = "center-messenger_database"
EXPECTED_DB_REVISION = "0023_member_tasks_section"
READY_LOCAL = "http://127.0.0.1:8100/health/ready"
READY_PUBLIC = "https://messenger.rastudio.org/health/ready"

def cmd(*argv, env=None, capture=True):
    r = subprocess.run(argv, check=True, text=True, stdout=subprocess.PIPE if capture else None, env=env)
    return r.stdout.strip() if capture else ""

def ready(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            return response.status == 200 and json.load(response).get("status") == "ready"
    except (OSError, ValueError, urllib.error.HTTPError):
        return False

def wait_ready(image, timeout=90):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        info = json.loads(cmd("docker", "inspect", "center-messenger-api-1"))[0]
        if info["Config"]["Image"] != image or info["State"]["Status"] != "running":
            return False
        if ready(READY_LOCAL) and ready(READY_PUBLIC):
            return True
        time.sleep(3)
    return False

def verify_bundle(bundle):
    if not bundle.is_dir():
        raise ValueError("Bundle directory does not exist")
    sums = (bundle / "checksums.sha256").read_text().splitlines()
    expected_files = {"image.tar", "compose.yaml", "release.json"}
    found = set()
    for line in sums:
        m = re.fullmatch(r"([0-9a-f]{64})  ([a-zA-Z0-9._-]+)", line)
        if not m or m.group(2) not in expected_files or m.group(2) in found:
            raise ValueError("Invalid checksums manifest")
        name = m.group(2)
        found.add(name)
        with (bundle / name).open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != m.group(1):
            raise ValueError("Checksum mismatch: " + name)
    if found != expected_files:
        raise ValueError("Incomplete checksums manifest")
    release = json.loads((bundle / "release.json").read_text())
    sha = release["source_commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid source SHA")
    if (release.get("source_repo") != REPO or release.get("source_branch") != "main"
        or release.get("image") != "center-messenger:github-" + sha[:12]
        or release.get("deployment_format") != 2 or release.get("platform") != "linux/amd64"
        or release.get("schema_revision") != EXPECTED_DB_REVISION):
        raise ValueError("Unexpected provenance or migration revision")
    if not re.fullmatch(r"https://github.com/rastudiocmit-bot/ramessenger/actions/runs/[0-9]+", release.get("run_url", "")):
        raise ValueError("Unexpected release workflow URL")
    with tarfile.open(bundle / "image.tar") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        matches = [m for m in manifest if release["image"] in (m.get("RepoTags") or [])]
        if len(matches) != 1:
            raise ValueError("Missing or ambiguous image")
        config = archive.extractfile(matches[0]["Config"]).read()
        ident = "sha256:" + hashlib.sha256(config).hexdigest()
        conf = json.loads(config)
        if ident != release["image_id"] or (conf["os"], conf["architecture"]) != ("linux", "amd64"):
            raise ValueError("Docker image config does not match release")
    # Compare checked compose bytes to the immutable local production compose.
    # A new composition requires separate review; only update the API image here.
    if (bundle / "compose.yaml").read_bytes() != (ROOT / "compose.yaml").read_bytes():
        raise ValueError("Compose changed: manual review required")
    return release

def run_promotion(bundle, sha, check_only=False):
    if os.geteuid() != 0:
        raise ValueError("Run as root on the VPS")
    if not (ROOT / ".secrets/db_app_password").is_file() or not (ROOT / "infra/postgres/010-runtime-role.sh").is_file():
        raise ValueError("Required local deployment files are missing")
    release = verify_bundle(bundle)
    if sha != release["source_commit"]:
        raise ValueError("Explicit approved SHA does not match release")
    if subprocess.run(["systemctl", "is-active", "--quiet", "center-messenger-gitverse-runner.service"]).returncode == 0:
        raise ValueError("Legacy runner still active")
    if subprocess.run(["systemctl", "is-active", "--quiet", "center-messenger-deploy.timer"]).returncode == 0:
        raise ValueError("Legacy timer still active")
    api = json.loads(cmd("docker", "inspect", "center-messenger-api-1"))[0]
    db = json.loads(cmd("docker", "inspect", "center-messenger-db-1"))[0]
    old_image = api["Config"]["Image"]
    if (db["State"]["Status"] != "running" or
        db["State"].get("Health", {}).get("Status") != "healthy"):
        raise ValueError("DB not healthy")
    volumes = [m["Name"] for m in db["Mounts"] if m["Type"] == "volume"]
    if EXPECTED_DB_VOLUME not in volumes:
        raise ValueError("Unexpected database volume")
    if not ready(READY_LOCAL) or not ready(READY_PUBLIC):
        raise ValueError("Current API not ready")
    revision = cmd("docker", "exec", "center-messenger-db-1", "psql", "-U", "cm_owner",
                   "-d", "center_messenger", "-Atc", "SELECT version_num FROM alembic_version;")
    if revision != EXPECTED_DB_REVISION:
        raise ValueError("Database revision changed: manual migration review required")
    image = release["image"]
    print("Approved:", sha, "Old:", old_image, "New:", image, flush=True)
    if check_only:
        print("CHECK ONLY: validated release, existing API, DB, schema and legacy deploy status")
        return
    if image == old_image:
        print("Already deployed:", image)
        return
    print("Loading verified image without touching live services...", flush=True)
    cmd("docker", "load", "-i", str(bundle / "image.tar"), capture=False)
    # Distinct docker Id can refer to OCI manifest rather than the config digest.
    inspect = json.loads(cmd("docker", "image", "inspect", image))[0]
    if (inspect["Os"], inspect["Architecture"]) != ("linux", "amd64"):
        raise ValueError("Imported image wrong platform")
    # Backup must succeed before any service is recreated.
    backups = ROOT / "rollback"
    backups.mkdir(mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest = backups / ("before-" + stamp + ".dump")
    print("Creating database backup:", dest, flush=True)
    with dest.open("wb") as output:
        backup = subprocess.run(["docker", "exec", "center-messenger-db-1",
                                 "pg_dump", "-U", "cm_owner", "-d", "center_messenger", "-Fc"],
                                stdout=output)
    os.chmod(dest, 0o600)
    if backup.returncode or dest.stat().st_size < 1024:
        raise ValueError("Backup failed; refusing deployment")
    deploy_api_with_rollback(old_image, image, dest)

def deploy_api_with_rollback(old_image, new_image, backup_path):
    """Recreate only API; revert to previous image on any failed readiness.

    The caller must validate provenance, DB compatibility and backup first.
    This function is unit-tested without Docker or production access.
    """
    compose = ["docker", "compose", "-p", "center-messenger", "-f", str(ROOT / "compose.yaml")]
    new_env = dict(os.environ, CM_IMAGE=new_image)
    changed = False
    try:
        print("Recreating only API...", flush=True)
        changed = True
        cmd(*compose, "up", "-d", "--no-deps", "--no-build", "--force-recreate", "api",
            env=new_env, capture=False)
        if not wait_ready(new_image):
            raise ValueError("New API failed readiness")
        print("DEPLOYED", new_image, "backup", backup_path, flush=True)
    except Exception:
        if changed:
            print("ERROR: rolling back API only to", old_image, file=sys.stderr, flush=True)
            try:
                restore_env = dict(os.environ, CM_IMAGE=old_image)
                cmd(*compose, "up", "-d", "--no-deps", "--no-build",
                    "--force-recreate", "api", env=restore_env, capture=False)
                if not wait_ready(old_image):
                    print("ROLLBACK NOT READY: manual intervention required", file=sys.stderr)
                    raise RuntimeError("Rollback health check failed")
                print("Rollback ready: True", file=sys.stderr, flush=True)
            except Exception as rollback_error:
                print("ROLLBACK FAILED:", rollback_error, file=sys.stderr)
                raise RuntimeError("New API failed and rollback did not recover service") from rollback_error
        raise

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--approve-sha", required=True, help="Exact 40-character GitHub commit SHA")
    p.add_argument("--check-only", action="store_true", help="Validate, but do not load, back up or deploy")
    args = p.parse_args()
    lock = ROOT / ".deploy.lock"
    with lock.open("a+") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_promotion(args.bundle.resolve(), args.approve_sha, args.check_only)

if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("DEPLOYMENT BLOCKED:", error, file=sys.stderr)
        sys.exit(1)
