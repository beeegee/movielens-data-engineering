"""CI-only native MongoDB, real regression-suite and HTTP-login checks.

Uses isolated test databases, not the full MovieLens import or local accounts.
Download the native runtime with bootstrap_runtime.mongo() before running.
"""
from pathlib import Path
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid

import requests

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
from scripts.manage import LOCAL_URI, mongodb


def main():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        http_port = reservation.getsockname()[1]
    base_url = f"http://127.0.0.1:{http_port}"
    environment = dict(os.environ, MONGO_URI=LOCAL_URI, TEST_MONGO_URI=LOCAL_URI,
                       SECRET_KEY=secrets.token_hex(32),
                       DB_NAME="movielens_test_http_" + uuid.uuid4().hex,
                       PORT=str(http_port))
    with mongodb({"MONGO_URI": LOCAL_URI}) as client:
        subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=BASE,
                       env=environment, check=True)
        process = subprocess.Popen([sys.executable, "app.py"], cwd=BASE, env=environment)
        try:
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError("Flask exited before the HTTP login check")
                try:
                    response = requests.get(base_url + "/login", timeout=2)
                    if response.status_code == 200 and 'csrf-token' in response.text:
                        break
                except requests.ConnectionError:
                    pass
                time.sleep(.5)
            else:
                raise RuntimeError("Flask did not serve the login page")
            response = requests.get(base_url + "/api/years", timeout=5)
            assert response.status_code == 401 and response.headers['Content-Type'].startswith('application/json')
            print("Native Flask HTTP login and anonymous API guard: PASS", flush=True)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            assert environment['DB_NAME'].startswith('movielens_test_http_')
            client.drop_database(environment['DB_NAME'])
    print("Native MongoDB startup, integration suite and clean shutdown: PASS", flush=True)


if __name__ == "__main__":
    main()
