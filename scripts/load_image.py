"""Verify a downloaded runtime bundle and load its image; do not start services."""

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def saved_image_identity(path: Path, image: str) -> dict:
    """Read the platform config digest, independent of the Docker image store.

    Docker's classic store reports the config digest as Id; containerd reports
    a manifest/index digest. docker save retains the same config bytes in both
    stores. The config also commits to the ordered, uncompressed layer digests.
    No archive paths are extracted onto the filesystem.
    """
    with tarfile.open(path, "r:*") as archive:
        members = {}
        for member in archive.getmembers():
            name = str(PurePosixPath(member.name))
            if name in members:
                raise ValueError("Duplicate image archive member")
            members[name] = member

        def read_json_file(name):
            name = PurePosixPath(name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("Invalid image archive path")
            member = members.get(str(name))
            if member is None or not member.isfile() or member.size > 8 * 1024 * 1024:
                raise ValueError("Invalid image archive JSON file")
            with archive.extractfile(member) as stream:
                data = stream.read()
            return data, json.loads(data)

        _, manifest = read_json_file("manifest.json")
        if not isinstance(manifest, list):
            raise ValueError("Invalid image archive manifest")
        names = {image, f"docker.io/library/{image}", f"index.docker.io/library/{image}"}
        matches = [
            entry
            for entry in manifest
            if isinstance(entry, dict) and names.intersection(entry.get("RepoTags") or [])
        ]
        if len(matches) != 1:
            raise ValueError("Image archive must contain exactly one matching image")
        config_bytes, config = read_json_file(matches[0]["Config"])
        platform = f"{config['os']}/{config['architecture']}"
        if config.get("variant"):
            platform += f"/{config['variant']}"
        return {
            "config_digest": "sha256:" + hashlib.sha256(config_bytes).hexdigest(),
            "platform": platform,
        }


def verify_bundle(root: Path) -> dict:
    checked = set()
    for line in (root / "checksums.sha256").read_text().splitlines():
        digest, name = line.split("  ", 1)
        path = PurePosixPath(name)
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or path.is_absolute() or ".." in path.parts:
            raise ValueError("Invalid checksum entry")
        target = root / name
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("Invalid runtime file path")
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError(f"Checksum mismatch: {name}")
        checked.add(name)
    if not {"image.tar.gz", "release.json", "compose.yaml", ".env"} <= checked:
        raise ValueError("Incomplete bundle")
    metadata = json.loads((root / "release.json").read_text())
    if not re.fullmatch(r"center-messenger:ci-\d+-\d+", metadata["image"]):
        raise ValueError("Invalid image reference")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", metadata["image_id"]):
        raise ValueError("Invalid image identity")
    return metadata


def expected_image_identity(root: Path, metadata: dict) -> dict:
    expected = saved_image_identity(root / "image.tar.gz", metadata["image"])
    if expected["platform"] != "linux/amd64" or metadata["platform"] != expected["platform"]:
        raise ValueError("Unexpected release image platform")
    # Older packages were produced in a classic store, so image_id is their
    # config digest. Keep those immutable packages verifiable without editing
    # their scripts, manifests or checksums.
    recorded = metadata.get("image_config_digest", metadata["image_id"])
    if recorded != expected["config_digest"]:
        raise ValueError("Release config digest does not match the tested image")
    return expected


def save_image(image: str, destination: Path):
    subprocess.run(["docker", "image", "save", "--output", str(destination), image], check=True)


def verify_loaded_image(root: Path, metadata: dict, *, exporter=save_image, expected=None):
    expected = expected or expected_image_identity(root, metadata)
    with tempfile.TemporaryDirectory(prefix="cm-image-check-") as temporary:
        saved = Path(temporary) / "loaded.tar"
        exporter(metadata["image"], saved)
        actual = saved_image_identity(saved, metadata["image"])
    if actual != expected:
        raise ValueError("Loaded image content does not match the tested image")
    return expected


def load_image(root: Path, *, verify_only=False):
    metadata = verify_bundle(root)
    expected = expected_image_identity(root, metadata)
    if not verify_only:
        subprocess.run(
            ["docker", "image", "load", "--input", str(root / "image.tar.gz")], check=True
        )
    verify_loaded_image(root, metadata, expected=expected)
    print(f"Verified image content: {expected['config_digest']}")
    action = "Verified existing image" if verify_only else "Verified image loaded"
    print(f"{action}: {metadata['image']}")
    print("No services were started.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--verify-only", action="store_true", help="Verify an already loaded image")
    args = parser.parse_args()
    load_image(args.bundle_root.resolve(), verify_only=args.verify_only)
