#!/usr/bin/env python3
"""Optional GitHub Actions poller. Disabled until locally opted in.

Reads /opt/center-messenger-github/autodeploy.json. Requires enabled=true,
verified successful build and verification workflows for the same main SHA.
Only updates API through github_promote_vps.py. Never invokes migrations.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

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
    if os.geteuid() != 0:
        raise RuntimeError("root required")
    if not STATE.is_file():
        print("DISABLED: missing autodeploy.json")
        return
    s = json.loads(STATE.read_text())
    if s.get("enabled") is not True:
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
    if newest.get("conclusion") != "success" or newest.get("status") != "completed":
        print("WAIT: latest packaging not successful")
        return
    checks = workflow_runs("verify.yml")
    if not any(r.get("headSha") == sha and r.get("status") == "completed"
               and r.get("conclusion") == "success" for r in checks):
        print("WAIT: verification for selected SHA not successful")
        return
    if sha in (s.get("last_deployed_sha"), s.get("last_failed_sha")):
        print("UNCHANGED or blocked:", sha)
        return
    active = subprocess.check_output(
        ["docker", "inspect", "center-messenger-api-1", "--format",
         "{{.Config.Image}}"], text=True).strip()
    if active == "center-messenger:github-" + sha[:12]:
        s["last_deployed_sha"] = sha
        write_state(s)
        print("Already running", sha)
        return
    run_id = str(newest["databaseId"])
    staging = Path(tempfile.mkdtemp(prefix="github-bundle-", dir=str(ROOT)))
    try:
        subprocess.run([
            "gh", "run", "download", run_id, "-R", REPO,
            "-n", "ramessenger-linux-amd64-" + sha, "-D", str(staging)
        ], check=True)
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
