#!/usr/bin/env python3
"""Start the dashboard with its project-local environment on any supported OS."""
from pathlib import Path
import subprocess
import sys

from platform_support import environment_python

BASE = Path(__file__).resolve().parent


def main():
    python = environment_python(BASE)
    if not python.is_file():
        print("Run install.py with Python 3.12 or newer before starting the app.", file=sys.stderr)
        return 1
    process = subprocess.Popen([str(python), "scripts/manage.py", "run"], cwd=BASE)
    try:
        return process.wait()
    except KeyboardInterrupt:
        # The child shares the terminal and receives Ctrl+C too. Give its MongoDB
        # shutdown handler time to flush and exit instead of killing it at once.
        try:
            return process.wait(timeout=35)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return 130


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as error:
        print(f"Could not start the dashboard: {error}", file=sys.stderr)
        raise SystemExit(1)
