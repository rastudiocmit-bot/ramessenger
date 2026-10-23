"""Create an immutable GitHub release bundle; never touch the production server."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from shutil import copy2

def run(*args):
    return subprocess.check_output(args, text=True).strip()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--image", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--run-url", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", a.sha):
        raise SystemExit("Invalid commit SHA")
    if a.image != "center-messenger:github-" + a.sha[:12]:
        raise SystemExit("Image tag does not match commit")
    if not a.run_url.startswith("https://github.com/rastudiocmit-bot/ramessenger/actions/runs/"):
        raise SystemExit("Unexpected CI origin")
    if run("git", "rev-parse", "HEAD") != a.sha:
        raise SystemExit("Checkout is not the requested commit")
    root = Path(__file__).resolve().parents[1]
    a.output.mkdir(parents=True, exist_ok=False)
    details = json.loads(run("docker", "image", "inspect", a.image))[0]
    if (details.get("Os"), details.get("Architecture")) != ("linux", "amd64"):
        raise SystemExit("Unexpected image platform")
    template = json.loads((root / "release.json").read_text())
    metadata = dict(
        source_repo="rastudiocmit-bot/ramessenger",
        source_branch="main",
        source_commit=a.sha,
        run_url=a.run_url,
        image=a.image,
        image_id=details["Id"],
        platform="linux/amd64",
        postgres=template["postgres"],
        version=template["version"],
        schema_revision=template["schema_revision"],
        stage=template["stage"],
        deployment_format=2,
    )
    subprocess.run(["docker", "image", "save", "--output", str(a.output / "image.tar"), a.image], check=True)
    copy2(root / "compose.yaml", a.output / "compose.yaml")
    (a.output / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")
    names = ["image.tar", "compose.yaml", "release.json"]
    checksums = []
    for name in names:
        with (a.output / name).open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        checksums.append(f"{digest}  {name}\n")
    (a.output / "checksums.sha256").write_text("".join(checksums))
    print("GitHub build artifact prepared (not deployed):", a.sha)

if __name__ == "__main__":
    main()
