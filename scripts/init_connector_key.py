"""Create the persistent connector encryption key outside database backup directories."""

import os
from pathlib import Path

try:
    from .init_secrets import initialize
except ImportError:
    from init_secrets import initialize


if __name__ == "__main__":
    directory = Path(os.environ.get("CM_KEY_DIR", "/opt/center-messenger-keys"))
    created = initialize(directory, names=("connection_master_key",))
    print("Connector key created." if created else "Existing connector key preserved.")
