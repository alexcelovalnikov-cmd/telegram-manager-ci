#!/usr/bin/env python3
"""Run a worker target behind a local Tor SOCKS transport."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def wait_bootstrap(log_path, process, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            try:
                details = log_path.read_text(errors="replace")[-4000:]
            except FileNotFoundError:
                details = ""
            raise RuntimeError("Tor exited before bootstrap\n" + details)
        try:
            text = log_path.read_text(errors="replace")
        except FileNotFoundError:
            text = ""
        if "Bootstrapped 100%" in text:
            print("TOR_TRANSPORT_READY", flush=True)
            return
        time.sleep(1)
    try:
        details = log_path.read_text(errors="replace")[-8000:]
    except FileNotFoundError:
        details = ""
    raise RuntimeError("Tor bootstrap timeout\n" + details)


def main():
    target = sys.argv[1:]
    if not target:
        raise SystemExit("worker target required")
    if os.environ.get("TM_TELEGRAM_PROXY_MODE", "").lower() != "tor":
        os.execv(sys.executable, [sys.executable, "-u", *target])

    state = Path(os.environ.get("TM_WORKER_STATE_DIR", "/state"))
    data = state / "tor-data"
    log_path = state / "tor-bootstrap.log"
    data.mkdir(parents=True, exist_ok=True)
    try:
        log_path.unlink()
    except FileNotFoundError:
        pass

    torrc = state / "torrc"
    torrc.write_text(
        "ClientOnly 1\n"
        "SocksPort 127.0.0.1:9050\n"
        f"DataDirectory {data}\n"
        "Log notice stdout\n",
        encoding="utf-8",
    )
    torrc.chmod(0o600)

    log_handle = log_path.open("w")
    tor = subprocess.Popen(
        ["tor", "-f", str(torrc)],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    child = None

    def stop(signum, frame):
        if child and child.poll() is None:
            child.terminate()
        if tor.poll() is None:
            tor.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        wait_bootstrap(log_path, tor)
        child = subprocess.Popen([sys.executable, "-u", *target])
        code = child.wait()
        return code
    finally:
        if child and child.poll() is None:
            child.terminate()
        if tor.poll() is None:
            tor.terminate()
            try:
                tor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                tor.kill()
        log_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
