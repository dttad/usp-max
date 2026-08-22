"""Profile ultimate-sitemap-parser under replay corpus.

Runs pyinstrument + memray on the configured profile and emits a flamegraph
artifact and a short textual summary.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
RESULTS_DIR = Path(__file__).parent / "results"
PROFILE_DIR = Path(__file__).parent / "profiles"


def profile_pyinstrument(profile: str, cap: int, replay_url: str, homepage: str):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    out = PROFILE_DIR / f"pyinstrument-{profile}-cap{cap}.html"
    script = PROFILE_DIR / f"_profile-{profile}-{cap}.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "from bench._profile_runner import crawl, make_reroute_client\n"
        f"client = make_reroute_client('{replay_url}')\n"
        f"pages = crawl('{homepage}', client, {cap})\n"
        f"print('OK urls=', len(pages), flush=True)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    cmd = [
        "uv", "run", "pyinstrument",
        "-o", str(out), "-r", "html",
        str(script),
    ]
    log.info("pyinstrument %s cap=%d -> %s", profile, cap, out)
    return subprocess.call(cmd, cwd=ROOT, env=env)


def profile_memray(profile: str, cap: int, replay_url: str, homepage: str):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    bin_out = PROFILE_DIR / f"memray-{profile}-cap{cap}.bin"
    script = PROFILE_DIR / f"_profile-memray-{profile}-{cap}.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "from bench._profile_runner import crawl, make_reroute_client\n"
        f"client = make_reroute_client('{replay_url}')\n"
        f"pages = crawl('{homepage}', client, {cap})\n"
        f"print('OK urls=', len(pages), flush=True)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    cmd = [
        "uv", "run", "--group", "perf", "python", "-m", "memray", "run",
        "-o", str(bin_out), "--force", "--native", str(script),
    ]
    log.info("memray %s cap=%d -> %s", profile, cap, bin_out)
    rc = subprocess.call(cmd, cwd=ROOT, env=env)
    if rc != 0:
        return rc
    fg = PROFILE_DIR / f"memray-{profile}-cap{cap}.html"
    subprocess.call(
        ["uv", "run", "--group", "perf", "python", "-m", "memray", "flamegraph",
         "-o", str(fg), "-f", str(bin_out)],
        cwd=ROOT, env=env,
    )
    stats = PROFILE_DIR / f"memray-{profile}-cap{cap}.json"
    subprocess.call(
        ["uv", "run", "--group", "perf", "python", "-m", "memray", "stats",
         "-o", str(stats), "-f", str(bin_out)],
        cwd=ROOT, env=env,
    )
    return 0


def count_tempfiles(profile: str, cap: int, replay_url: str, homepage: str):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    script = PROFILE_DIR / f"_profile-tempfile-{profile}-{cap}.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "import tempfile\n"
        "original = tempfile.mkstemp\n"
        "peak = {'count': 0}\n"
        "def patched(*a, **kw):\n"
        "    peak['count'] += 1\n"
        "    return original(*a, **kw)\n"
        "tempfile.mkstemp = patched\n"
        "from bench._profile_runner import crawl, make_reroute_client\n"
        f"client = make_reroute_client('{replay_url}')\n"
        f"pages = crawl('{homepage}', client, {cap})\n"
        f"open('/tmp/tempfile-count-{profile}-{cap}.txt', 'w').write(str(peak['count']))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    log.info("tempfile-count %s cap=%d", profile, cap)
    subprocess.call(["uv", "run", sys.executable, str(script)], cwd=ROOT, env=env)
    out_path = Path(f"/tmp/tempfile-count-{profile}-{cap}.txt")
    if out_path.exists():
        return int(out_path.read_text().strip())
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="zero")
    parser.add_argument("--cap", type=int, default=200)
    parser.add_argument("--replay-url", default="http://127.0.0.1:8765")
    parser.add_argument("--homepage", default="http://127.0.0.1:8765/")
    parser.add_argument("--mode", choices=["pyinstrument", "memray", "tempfile", "all"], default="all")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    global log
    log = logging.getLogger("bench.profile")

    if args.mode in ("pyinstrument", "all"):
        rc = profile_pyinstrument(args.profile, args.cap, args.replay_url, args.homepage)
        if rc != 0:
            log.error("pyinstrument failed (rc=%d)", rc)
    if args.mode in ("memray", "all"):
        rc = profile_memray(args.profile, args.cap, args.replay_url, args.homepage)
        if rc != 0:
            log.error("memray failed (rc=%d)", rc)
    if args.mode in ("tempfile", "all"):
        count = count_tempfiles(args.profile, args.cap, args.replay_url, args.homepage)
        log.info("tempfile peak count = %s", count)

    return 0


if __name__ == "__main__":
    sys.exit(main())
