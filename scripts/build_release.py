"""Package a locally built production image for the verified deploy controller."""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def run(*args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


def saved_config_digest(path: Path, image: str) -> str:
    """Return the digest of the image config stored in Docker's saved archive."""
    with tarfile.open(path, "r:*") as archive:
        members = {}
        for member in archive.getmembers():
            name = str(PurePosixPath(member.name))
            if name in members:
                raise SystemExit("Docker image archive contains duplicate paths")
            members[name] = member

        manifest_member = members.get("manifest.json")
        if manifest_member is None or not manifest_member.isfile():
            raise SystemExit("Docker image archive has no manifest.json")
        with archive.extractfile(manifest_member) as stream:
            manifest = json.load(stream)

        matches = [
            entry
            for entry in manifest
            if isinstance(entry, dict) and image in (entry.get("RepoTags") or [])
        ]
        if len(matches) != 1:
            raise SystemExit("Docker image archive does not contain exactly one expected image")

        config_path = PurePosixPath(matches[0]["Config"])
        if config_path.is_absolute() or ".." in config_path.parts:
            raise SystemExit("Docker image archive contains an invalid config path")
        config_member = members.get(str(config_path))
        if config_member is None or not config_member.isfile() or config_member.size > 8 * 1024 * 1024:
            raise SystemExit("Docker image config is missing or too large")
        with archive.extractfile(config_member) as stream:
            config = json.load(stream)
        if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
            raise SystemExit("Saved image config is not linux/amd64")
        with archive.extractfile(config_member) as stream:
            return "sha256:" + hashlib.sha256(stream.read()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    image = args.image
    match = re.fullmatch(r"center-messenger:gitverse-([a-f0-9]{12})", image)
    if not match:
        raise SystemExit("Invalid GitVerse image tag")
    root = Path(__file__).resolve().parents[1]
    output = args.output
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(mode=0o700, parents=True)

    commit = run("git", "-C", str(root), "rev-parse", "HEAD")
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or not commit.startswith(match.group(1)):
        raise SystemExit("Image tag does not match checked out commit")
    template = json.loads((root / "release.json").read_text())
    required = ("version", "stage", "schema_revision", "postgres")
    if any(not isinstance(template.get(key), str) or not template[key] for key in required):
        raise SystemExit("release.json template lacks version/stage/schema_revision/postgres")
    inspection = json.loads(run("docker", "image", "inspect", image))[0]
    if (inspection.get("Os"), inspection.get("Architecture")) != ("linux", "amd64"):
        raise SystemExit("Production image must be linux/amd64")
    config_digest = inspection.get("Id", "")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", config_digest):
        raise SystemExit("Docker did not return a valid image config digest")

    metadata = {
        "source_commit": commit,
        "run_url": f"https://gitverse.ru/olegsheff/center-messenger/commit/{commit}",
        "image": image,
        "image_id": config_digest,
        "image_config_digest": config_digest,
        "platform": "linux/amd64",
        "postgres": template["postgres"],
        "stage": template["stage"],
        "version": template["version"],
        "schema_revision": template["schema_revision"],
        "deployment_format": 1,
    }
    (output / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")
    shutil.copy2(root / "compose.yaml", output / "compose.yaml")

    with tempfile.TemporaryDirectory(prefix="cm-image-export-") as temporary:
        raw = Path(temporary) / "image.tar"
        subprocess.run(["docker", "image", "save", "--output", str(raw), image], check=True)
        with raw.open("rb") as source, (output / "image.tar.gz").open("wb") as target:
            with gzip.GzipFile(fileobj=target, mode="wb", compresslevel=1, mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)

    # Docker's .Id differs between image-store implementations. Record the
    # config digest from the exact archive that the deploy controller verifies.
    config_digest = saved_config_digest(output / "image.tar.gz", image)
    metadata["image_id"] = config_digest
    metadata["image_config_digest"] = config_digest
    (output / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")

    entries = []
    for name in ("compose.yaml", "image.tar.gz", "release.json"):
        digest = hashlib.sha256((output / name).read_bytes()).hexdigest()
        entries.append(f"{digest}  {name}\n")
    (output / "checksums.sha256").write_text("".join(entries))
    for path in output.iterdir():
        path.chmod(0o600)
    print(f"Prepared image bundle for {commit}")


if __name__ == "__main__":
    main()
