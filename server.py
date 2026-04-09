from __future__ import annotations

import os
import sys

from app.config import Settings

def main() -> None:
    settings = Settings.from_env()
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(settings.serve_port),
        ],
    )

if __name__ == "__main__":
    main()
