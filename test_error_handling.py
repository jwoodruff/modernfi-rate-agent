"""
One-command test for the "Anthropic API call fails" error path
(agent.py's APIError handling -> 503 -> status='error' in history).

This automates what would otherwise be manual .env editing + container
restarts:

  1. Backs up your current .env
  2. Swaps ANTHROPIC_API_KEY for an obviously-invalid value
  3. Recreates the `app` container so it picks up the bad key
  4. Runs the same checks as `test_agent.py --error-handling`
  5. ALWAYS restores your real .env and recreates the container again,
     even if something above fails or you Ctrl+C partway through

Usage:
    python test_error_handling.py

Requires: docker compose already set up as usual (this assumes a service
named `app` in docker-compose.yml, and a .env file in the current directory).
"""

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ENV_PATH = Path(".env")
BACKUP_PATH = Path(".env.bak")
BASE_URL = "http://localhost:8000"
FAKE_KEY = "sk-ant-invalid-test-key-0000000000000000000000000000000000"


def run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def swap_in_bad_key() -> None:
    if not ENV_PATH.exists():
        print(
            "ERROR: .env not found in the current directory. Run this from the project root."
        )
        sys.exit(1)

    shutil.copy(ENV_PATH, BACKUP_PATH)
    text = ENV_PATH.read_text()

    if "ANTHROPIC_API_KEY=" not in text:
        print("ERROR: ANTHROPIC_API_KEY not found in .env — nothing to swap.")
        sys.exit(1)

    new_text = re.sub(
        r"^ANTHROPIC_API_KEY=.*$",
        f"ANTHROPIC_API_KEY={FAKE_KEY}",
        text,
        flags=re.MULTILINE,
    )
    ENV_PATH.write_text(new_text)
    print(
        f"Backed up real .env to {BACKUP_PATH}, swapped in an invalid ANTHROPIC_API_KEY."
    )


def restore_real_env() -> None:
    if BACKUP_PATH.exists():
        shutil.move(BACKUP_PATH, ENV_PATH)
        print("Restored your real .env from backup.")
    else:
        print("No backup found to restore — check .env manually before continuing.")


def recreate_app_container() -> None:
    run(["docker", "compose", "up", "-d", "--build", "--force-recreate", "app"])


def wait_for_app(timeout_seconds: int = 30) -> bool:
    """The app should still come up healthy even with a bad Anthropic key —
    /health only checks Postgres. Wait for that before running checks."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            response = httpx.get(f"{BASE_URL}/health", timeout=5.0)
            if response.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return False


def run_checks() -> None:
    print(f"\n{'=' * 70}")
    print("API ERROR HANDLING CHECK")

    response = httpx.post(
        f"{BASE_URL}/ask",
        json={"question": "What is the federal funds rate?"},
        timeout=30.0,
    )
    print(f"Status: {response.status_code} (expect 503)")
    print(f"Body: {response.text}")

    if response.status_code == 503:
        print("✅ Got 503 as expected — the app degraded gracefully")
    else:
        print(
            f"⚠️  Expected 503, got {response.status_code} — error handling "
            "may not be catching this correctly"
        )

    history = httpx.get(f"{BASE_URL}/history?limit=1", timeout=10.0).json()
    if history and history[0]["status"] == "error":
        print("✅ Most recent history row correctly shows status='error'")
    elif history:
        print(
            f"⚠️  Most recent history row has status={history[0]['status']!r}, "
            "expected 'error'"
        )
    else:
        print("⚠️  Couldn't fetch history to verify")

    health = httpx.get(f"{BASE_URL}/health", timeout=10.0)
    print(
        f"/health -> {health.status_code} "
        "(expected to still be 200/healthy — it doesn't check Anthropic)"
    )


def main() -> None:
    swap_in_bad_key()
    try:
        recreate_app_container()
        if not wait_for_app():
            print(
                "⚠️  App didn't come up healthy in time — check `docker compose logs app`"
            )
            return
        run_checks()
    finally:
        restore_real_env()
        recreate_app_container()
        print("\nDone — app container recreated with your real key.")


if __name__ == "__main__":
    main()
