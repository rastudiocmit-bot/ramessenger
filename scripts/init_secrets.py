"""Generate local Compose secrets; never replace an existing password."""

import os
import secrets
from pathlib import Path


def initialize(
    directory: Path, names=("db_owner_password", "db_app_password", "owner_bootstrap_token")
) -> list[str]:
    if directory.is_symlink():
        raise ValueError("Secret directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not directory.is_dir():
        raise ValueError("Secret path must be a directory")
    os.chmod(directory, 0o700)
    created = []
    for name in names:
        path = directory / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                raise ValueError(
                    "Existing secret is invalid; inspect it without printing contents"
                ) from None
            continue
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(48) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            # Directory is 0700 on the host. A file is mounted individually and read-only
            # into only the containers that need it, including the non-root API user.
            path.chmod(0o444)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        created.append(name)
    return created


if __name__ == "__main__":
    for name in initialize(Path(__file__).resolve().parents[1] / ".secrets"):
        print(f"Created {name}")
    print("Secrets ready; existing values were preserved.")
