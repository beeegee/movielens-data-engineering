#!/usr/bin/env python3
"""Set up MovieLens using standard Python and pip, without uv or shell activation."""
from pathlib import Path
import argparse
import platform
import subprocess
import sys

from platform_support import environment_python as native_environment_python, runtime_target

BASE = Path(__file__).resolve().parent


def run(command):
    subprocess.run([str(part) for part in command], cwd=BASE, check=True)


def check_runtime():
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required. Python 3.12 is the tested version.")
    runtime_target(platform.system(), platform.machine())


def environment_python():
    environment = BASE / ".venv"
    python = native_environment_python(BASE)
    if environment.exists():
        if not (environment / "pyvenv.cfg").is_file() or not python.is_file():
            raise RuntimeError(
                ".venv exists but is not a usable virtual environment. "
                "Move it aside before retrying. The installer will not delete it."
            )
        print("Reusing .venv.", flush=True)
    else:
        print("Creating a project-local Python environment...", flush=True)
        try:
            run([sys.executable, "-m", "venv", environment])
        except subprocess.CalledProcessError as error:
            raise RuntimeError(
                "Could not create .venv. On Ubuntu, install python3-venv "
                "for your Python version, then rerun the installer."
            ) from error
    # An existing environment may use a different interpreter from this script.
    run([python, "-c", "import sys; raise SystemExit(sys.version_info < (3, 12))"])
    # Also supports existing environments created without pip.
    run([python, "-m", "ensurepip", "--upgrade"])
    return python


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="also install the test and browser-check dependencies")
    parser.add_argument(
        "--dependencies-only", action="store_true",
        help="install Python dependencies without downloading data or starting MongoDB",
    )
    parser.add_argument("--reset-data", action="store_true",
                        help="recover an incomplete import by replacing dataset collections, app ratings and rating history; preserve accounts")
    args = parser.parse_args(argv)
    if args.reset_data and args.dependencies_only:
        parser.error("--reset-data cannot be combined with --dependencies-only")
    check_runtime()
    if args.reset_data:
        print("WARNING: replacing dataset collections, app ratings and rating history. Accounts are preserved.", flush=True)
    if not args.dependencies_only:
        print(
            "First setup downloads MongoDB and the complete MovieLens 25M dataset. "
            "Allow several GB of free disk space and time for the full import. "
            "A normal rerun preserves completed imports, accounts and ratings. Interrupted imports require explicit recovery; see README.md.",
            flush=True,
        )
    python = environment_python()
    requirements = "requirements-dev.txt" if args.dev else "requirements.txt"
    print("Installing Python dependencies...", flush=True)
    run([python, "-m", "pip", "install", "-r", requirements])
    if args.dependencies_only:
        print("Dependencies installed. Run install.py again without --dependencies-only to finish database setup.", flush=True)
        return
    command = [python, "scripts/manage.py", "setup"]
    if args.reset_data:
        command.append("--reset-data")
    run(command)
    print("\nReady. Start the dashboard with:", flush=True)
    print("  python run.py" if platform.system() == "Windows" else "  python3 run.py", flush=True)
    print("Open http://127.0.0.1:5008 and read the generated .env locally for login details.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nSetup interrupted. Retry downloads with the same command. If ingestion was interrupted, see the explicit recovery procedure in README.md.", file=sys.stderr)
        raise SystemExit(130)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        print("Fix the reported problem and rerun the same installer command. See README.md for help.", file=sys.stderr)
        raise SystemExit(1)
