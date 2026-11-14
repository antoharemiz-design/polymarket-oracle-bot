"""Read recorder output (research/tools/recorder.py) as a time-ordered event stream.

All times are local receive times in ms: this is what a live process would have observed.
Polymarket events are normalized to the Up token:
    ("S", t, mid)                      spot mid (Binance)
    ("B", t, bids, asks)               Up book snapshot, lists of [price, size] strings
    ("P", t, side, price_c, size)      Up level update, side "B"/"S"(ask), size = new total
    ("T", t, taker_side, price_c, size, tx)   trade, taker side in Up terms
"""
import glob
import gzip
import heapq
import json
import re
import zlib


def read_gz_lines(path):
    try:
        with gzip.open(path, "rt") as f:
            for line in f:
                yield line
    except (EOFError, OSError, zlib.error):
        return  # truncated or interrupted file: use what was readable


def market_events(path):
    for line in read_gz_lines(path):
        k = line[0]
        try:
            if k == "P":
                _, rcv, _srv, a, side, price, size, _bb, _ba = line.rstrip("\n").split(",")
                if a == "0":
                    yield ("P", int(rcv), "B" if side == "B" else "S", int(round(float(price) * 100)), float(size))
            elif k == "T":
                _, rcv, _srv, a, side, price, size, tx = line.rstrip("\n").split(",")
                pc = int(round(float(price) * 100))
                if a == "1":                       # trade in Down -> mirror into Up terms
                    side = "S" if side == "B" else "B"; pc = 100 - pc
                yield ("T", int(rcv), side, pc, float(size or 0), tx)
            elif k == "B":
                head = line.split(",", 4)
                if head[3] == "0":
                    # payload is "<bids json>","<asks json>" with unescaped inner quotes
                    body = head[4].strip()[1:-1]
                    cut = body.index(']","[')
                    yield ("B", int(head[1]), json.loads(body[:cut + 1]), json.loads(body[cut + 4:]))
        except (ValueError, IndexError):
            continue


def spot_events(rec_dir, t_from_ms, t_to_ms, source="binance"):
    for path in sorted(glob.glob(f"{rec_dir}/spot/{source}-*.csv.gz")):
        for line in read_gz_lines(path):
            p = line.rstrip("\n").split(",")
            try:
                t = int(p[0])
                if t < t_from_ms:
                    continue
                if t > t_to_ms:
                    return
                if source == "binance":
                    yield ("S", t, (float(p[1]) + float(p[2])) / 2)
                else:
                    yield ("S", t, float(p[1]))
            except (ValueError, IndexError):
                continue


def list_markets(rec_dir):
    out = []
    for path in sorted(glob.glob(f"{rec_dir}/pm/*.csv.gz")):
        m = re.search(r"-(\d+)\.csv\.gz$", path)
        if m:
            out.append((int(m.group(1)), path))
    return out


def merged(rec_dir, start, path, dur=300, warmup=1300):
    """Spot from `warmup` seconds before the window, merged with the market's events."""
    spot = spot_events(rec_dir, (start - warmup) * 1000, (start + dur + 30) * 1000)
    return heapq.merge(spot, market_events(path), key=lambda e: e[1])
