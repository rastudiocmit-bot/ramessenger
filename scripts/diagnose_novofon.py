#!/usr/bin/env python3
"""Read-only Novofon checks from a reviewed repository checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend" / "src"))
from center_messenger.novofon_client import main

if __name__ == "__main__":
    sys.exit(main())
