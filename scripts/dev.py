"""Start the whole BlockShare stack locally with one command.

    python scripts/dev.py              # chain + 3 storage nodes + monitor + Django on :8000
    python scripts/dev.py --no-chain   # without the blockchain (Conventional mode only)

Processes started:
  * Hardhat node            http://127.0.0.1:8545   (blockchain, chain id 31337)
  * storage nodes A, B, C   TCP 9101-9103 · UDP 10101-10103 · HTTP 9301-9303
  * monitor                 failure detection / auto-repair
  * Django                  http://127.0.0.1:8000
Ctrl+C stops everything.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def wait_http(url, timeout=60, data=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-chain", action="store_true")
    ap.add_argument("--port", default="8000")
    args = ap.parse_args()

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    procs = []

    def start(name, cmd, cwd=ROOT):
        print(f"[dev] starting {name}: {' '.join(cmd)}")
        procs.append((name, subprocess.Popen(cmd, cwd=cwd, env=env)))

    try:
        if not args.no_chain:
            npx = shutil.which("npx")
            rpc_probe = b'{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}'
            if wait_http("http://127.0.0.1:8545", 1, rpc_probe):
                print("[dev] reusing the blockchain node already running on :8545")
            elif npx and (ROOT / "blockchain" / "node_modules").exists():
                start("hardhat", [npx, "hardhat", "node", "--hostname", "127.0.0.1", "--port", "8545"],
                      cwd=ROOT / "blockchain")
                ok = wait_http("http://127.0.0.1:8545", 90,
                               b'{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}')
                print("[dev] blockchain", "ready" if ok else "NOT reachable")
            else:
                print("[dev] skipping blockchain (run `npm install` in blockchain/ first)")

        start("storage-nodes", [PY, "-m", "storage_node", "--cluster", "3", "--port", "9101",
                                "--http-port", "9301", "--data-dir", str(ROOT / "var" / "nodes")])
        time.sleep(1.5)
        subprocess.run([PY, "manage.py", "bootstrap"], cwd=ROOT, env=env, check=True)
        start("monitor", [PY, "manage.py", "run_monitor", "--interval", "5"])
        start("django", [PY, "manage.py", "runserver", f"127.0.0.1:{args.port}", "--noreload"])
        print(f"\n[dev] BlockShare is running on http://127.0.0.1:{args.port}  (Ctrl+C to stop)\n")
        reported = set()
        while procs[-1][1].poll() is None:  # run until Django stops
            for name, p in procs:
                if p.poll() is not None and name not in reported:
                    reported.add(name)
                    print(f"[dev] WARNING: {name} exited with code {p.returncode}")
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for _, p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
