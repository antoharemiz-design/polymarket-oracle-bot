"""Phase 3 research: keep the full public taker tape of the markets the phase 2 bot quotes (22 Nov).

    python research/tools/lp_tape.py OUT_DIR REPORTS_REPO SCAN_DIR --since EPOCH --until EPOCH [--every 1800]

The data-api returns only the newest 1000 taker trades of a market, so on busy markets a 48-hour test cannot be
fetched in one go at the end. Every `every` seconds until `until`:
  - the markets: every row of the portfolio table in each status.md version since `since` (git history of the
    private reports repository, REPORTS_REPO), matched to condition ids by question prefix against the newest
    lp_scan metadata in SCAN_DIR (status.md cuts questions at 60 characters; every match is kept);
  - their newest 1000 taker trades, merged into OUT_DIR/<condition>.jsonl (one trade per line, deduplicated).
Read-only public data; nothing on the bot changes.
"""
import gzip
import json
import os
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polybot import lp  # noqa: E402


def portfolio_questions(repo, since):
    subprocess.run(["git", "-C", repo, "pull", "-q"], capture_output=True)
    hs = subprocess.run(["git", "-C", repo, "log", f"--since=@{int(since)}", "--format=%H", "--", "status.md"],
                        capture_output=True, text=True).stdout.split()
    qs = set()
    for h in hs:
        txt = subprocess.run(["git", "-C", repo, "show", f"{h}:status.md"], capture_output=True, text=True).stdout
        if "## Портфель" not in txt:
            continue
        for line in txt.split("## Портфель", 1)[1].splitlines():
            if line.startswith("## "):
                break
            cells = [x.strip() for x in line.strip().strip("|").split("|")]
            if len(cells) > 3 and cells[0] not in ("Рынок",) and not set(cells[0]) <= set("-"):
                qs.add(cells[0].replace(" (выход)", "").strip())
    return qs


def conds_for(qs, scan):
    metas = sorted(f for f in os.listdir(scan) if f.startswith("meta_"))
    meta = {}
    for f in metas[-2:]:
        with gzip.open(os.path.join(scan, f), "rt") as fh:
            meta.update(json.load(fh))
    out = set()
    for q in qs:
        out.update(c for c, m in meta.items() if (m.get("question") or "").startswith(q))
    return out


def merge(out_dir, c):
    d = lp._req(f"{lp.DATA}/v2/trades?" + urllib.parse.urlencode({"condition": c, "taker_only": "true", "limit": 1000}))
    path = os.path.join(out_dir, f"{c}.jsonl")
    seen = set()
    if os.path.exists(path):
        for line in open(path):
            r = json.loads(line)
            seen.add((r.get("transaction_hash"), r["outcome_index"], r["price"], r["size"], r["timestamp"]))
    new = 0
    with open(path, "a") as f:
        for r in sorted(d.get("data") or [], key=lambda r: r["timestamp"]):
            k = (r.get("transaction_hash"), r["outcome_index"], r["price"], r["size"], r["timestamp"])
            if k in seen:
                continue
            seen.add(k)
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
            new += 1
    rows = d.get("data") or []
    return new, len(rows), min((r["timestamp"] for r in rows), default=None)


def main():
    out_dir, repo, scan = sys.argv[1], sys.argv[2], sys.argv[3]
    arg = lambda k, dflt: float(sys.argv[sys.argv.index(k) + 1]) if k in sys.argv else dflt
    since, until, every = arg("--since", time.time() - 48 * 3600), arg("--until", 0.0), arg("--every", 1800.0)
    os.makedirs(out_dir, exist_ok=True)
    while True:
        t = time.time()
        qs = portfolio_questions(repo, since)
        cs = conds_for(qs, scan)
        tot = 0
        for c in sorted(cs):
            try:
                new, n, oldest = merge(out_dir, c)
                tot += new
                if n >= 1000 and oldest and oldest > since:
                    print(f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())} {c[:10]} newest 1000 reach back only to "
                          f"{time.strftime('%m-%d %H:%M', time.gmtime(oldest))} UTC", flush=True)
            except Exception as e:
                print(f"{c[:10]} failed: {e!r:.100}", flush=True)
        print(f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())} {len(qs)} portfolio questions -> {len(cs)} markets, "
              f"{tot} new trades", flush=True)
        if not until or time.time() + every > until:
            return
        time.sleep(max(0.0, every - (time.time() - t)))


if __name__ == "__main__":
    main()
