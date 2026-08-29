from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camera_service.licensing import generate_license_keypair


if __name__ == "__main__":
    keys = generate_license_keypair()
    print("SNAPKEY_LICENSE_PRIVATE_KEY=" + keys["private_key"])
    print("SNAPKEY_LICENSE_PUBLIC_KEY=" + keys["public_key"])
