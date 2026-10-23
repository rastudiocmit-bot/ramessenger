#!/usr/bin/env python3
"""Optional GitHub Actions poller. Disabled until locally opted in.

Reads /opt/center-messenger-github/autodeploy.json. Requires enabled=true,
verified successful build and verification workflows for the same main SHA.
Only updates API through github_promote_vps.py. Never invokes migrations.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path("/opt/center-messenger-github")
STATE = ROOT / "autodeploy.json"
REPO = "rastudiocmit-bot/ramessenger"
PROMOTER = ROOT / "github_promote_vps.py"

def gh_json(*args):
    output = subprocess.check_output(["gh", *args], text=True)
    return json.loads(output)

def workflow_runs(name):
    return gh_json("run", "list", "-R", REPO, "--workflow", name, "--branch",
                   "main", "--limit", "8", "--json",
                   "databaseId,headSha,status,conclusion")

def main():
    parser = argparse.ArgumentParser(description="GitHub deployment poller")
    parser.add_argument("--preview", action="store_true", help="Read-only selection: no downloads, writes or deployment")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("root required")
    if not STATE.is_file():
        print("DISABLED: missing autodeploy.json")
        return
    s = json.loads(STATE.read_text())
    if not args.preview and s.get("enabled") is not True:
        print("DISABLED: enabled is not true")
        return
    if not PROMOTER.is_file():
        raise RuntimeError("Promotion script not installed")
    builds = workflow_runs("package-release.yml")
    if not builds:
        raise RuntimeError("No package runs found")
    newest = builds[0]
    sha = newest.get("headSha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("Invalid GitHub SHA")
    if args.preview:
        print("PREVIEW: no downloads, writes, deployment or restart")
        print("Autodeploy enabled:", s.get("enabled") is True)
        print("Newest packaging run:", newest.get("databaseId"))
        print("Candidate SHA:", sha)
    if newest.get("conclusion") != "success" or newest.get("status") != "completed":
        print("WAIT: latest packaging not successful")
        return
    checks = workflow_runs("verify.yml")
    verified = any(r.get("headSha") == sha and r.get("status") == "completed"
                   and r.get("conclusion") == "success" for r in checks)
    if args.preview:
        print("Matching verification:", "success" if verified else "missing/unsuccessful")
    if not verified:
        print("WAIT: verification for selected SHA not successful")
        return
    if sha in (s.get("last_deployed_sha"), s.get("last_failed_sha")):
        print("UNCHANGED or blocked:", sha)
        return
    active = subprocess.check_output(
        ["docker", "inspect", "center-messenger-api-1", "--format",
         "{{.Config.Image}}"], text=True).strip()
    if args.preview:
        print("Running image:", active)
        print("Decision:", "already running" if active == "center-messenger:github-" + sha[:12] else "eligible for further artifact checks; NOT deployed")
        return
    if active == "center-messenger:github-" + sha[:12]:
        s["last_deployed_sha"] = sha
        write_state(s)
        print("Already running", sha)
        return
    run_id = str(newest["databaseId"])
    staging = Path(tempfile.mkdtemp(prefix="github-bundle-", dir=str(ROOT)))
    try:
        download = [
            "gh", "run", "download", run_id, "-R", REPO,
            "-n", "ramessenger-linux-amd64-" + sha, "-D", str(staging)
        ]
        for attempt in range(1, 4):
            try:
                subprocess.run(download, check=True, timeout=450)
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt == 3:
                    raise
                print("GitHub artifact download interrupted; retry", attempt + 1, flush=True)
                shutil.rmtree(staging)
                staging.mkdir(mode=0o700)
                time.sleep(15 * attempt)
        for name in ("image.tar", "compose.yaml", "release.json", "checksums.sha256"):
            if not (staging / name).is_file() or (staging / name).stat().st_size == 0:
                raise RuntimeError("Incomplete GitHub release artifact: " + name)
        try:
            subprocess.run(
                ["python3", str(PROMOTER), "--bundle", str(staging),
                 "--approve-sha", sha], check=True)
        except subprocess.CalledProcessError:
            s["last_failed_sha"] = sha
            write_state(s)
            raise
        s["last_deployed_sha"] = sha
        s.pop("last_failed_sha", None)
        write_state(s)
        print("UPDATED", sha)
    finally:
        shutil.rmtree(staging)

def write_state(state):
    tmp = ROOT / "autodeploy.json.tmp"
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("AUTODEPLOY BLOCKED:", exc, file=sys.stderr)
        sys.exit(1)
