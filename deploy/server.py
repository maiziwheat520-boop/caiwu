from __future__ import annotations

import sys
from pathlib import Path


script_directory = Path(__file__).resolve().parent
package_root = script_directory if (script_directory / "server").is_dir() else script_directory.parent
sys.path.insert(0, str(package_root))

from server.app import run  # noqa: E402


if __name__ == "__main__":
    run()
