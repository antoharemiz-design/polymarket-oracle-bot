"""Read recorder output (research/tools/recorder.py) as a time-ordered event stream.

All times are local receive times in ms: this is what a live process would have observed.
Polymarket events are normalized to the Up token:
    ("S", t, mid)                      spot mid (Binance)
    ("B", t, bids, asks, srv)                 Up book snapshot, lists of [price, size] strings
    ("P", t, side, price_c, size, srv)        Up level update, side "B"/"S"(ask), size = new total
    ("T", t, taker_side, price_c, size, tx, srv)   trade, taker side in Up terms
`srv` is the exchange timestamp of the event in ms (0 if missing).
"""
import glob
import heapq
import json
import re
import zlib


def read_gz_lines(path):
    """Lines of a gzip file that may consist of several members, some cut short.

    The recorder appends a new gzip member every time it restarts and flushes every few
    seconds; a killed recorder leaves its member without a trailer. Each member is decoded
    separately and whatever decodes is used, so a restart costs seconds of data, not the file.
    """
    with open(path, "rb") as f:
        raw = f.read()
    magic = b"\x1f\x8b\x08"
    pos = raw.find(magic)
    tail = b""
    while pos != -1:
        d = zlib.decompressobj(wbits=31)
        try:
            chunk = d.decompress(raw[pos:])
        except zlib.error:
            # damaged member: decode as much as possible, then look for the next one
            chunk = b""
            d = zlib.decompressobj(wbits=31)
            step = 512
            i = pos
            try:
                while i < len(raw):
                    chunk += d.decompress(raw[i:i + step]); i += step
            except zlib.error:
                pass
        text = tail + chunk
        lines = text.split(b"\n")
        tail = lines.pop()
        for line in lines:
            yield line.decode("ascii", "replace") + "\n"
        if d.eof:
            pos = raw.find(magic, len(raw) - len(d.unused_data)) if d.unused_data else -1
        else:
            # truncated member: its unread tail is unusable; resync on the next member header
            tail = b""
            pos = raw.find(magic, pos + 3)
            while pos != -1:
                try:
                    zlib.decompressobj(wbits=31).decompress(raw[pos:pos + 4096])
                    break
                except zlib.error:
                    pos = raw.find(magic, pos + 3)


def market_events(path):
    for line in read_gz_lines(path):
        k = line[0]
        try:
            if k == "P":
                _, rcv, srv, a, side, price, size, _bb, _ba = line.rstrip("\n").split(",")
                if a == "0":
                    yield ("P", int(rcv), "B" if side == "B" else "S", int(round(float(price) * 100)), float(size), int(srv or 0))
            elif k == "T":
                _, rcv, srv, a, side, price, size, tx = line.rstrip("\n").split(",")
                pc = int(round(float(price) * 100))
                if a == "1":                       # trade in Down -> mirror into Up terms
                    side = "S" if side == "B" else "B"; pc = 100 - pc
                yield ("T", int(rcv), side, pc, float(size or 0), tx, int(srv or 0))
            elif k == "B":
                head = line.split(",", 4)
                if head[3] == "0":
                    # payload is "<bids json>","<asks json>" with unescaped inner quotes
                    body = head[4].strip()[1:-1]
                    cut = body.index(']","[')
                    yield ("B", int(head[1]), json.loads(body[:cut + 1]), json.loads(body[cut + 4:]), int(head[2] or 0))
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
                else:                              # coinbase rows: recv_ms, last price, best bid, best ask
                    yield ("S", t, (float(p[2]) + float(p[3])) / 2 if p[2] and p[3] else float(p[1]))
            except (ValueError, IndexError):
                continue


def list_markets(rec_dir):
    out = []
    for path in sorted(glob.glob(f"{rec_dir}/pm/*.csv.gz")):
        m = re.search(r"-(\d+)\.csv\.gz$", path)
        if m:
            out.append((int(m.group(1)), path))
    return out


def merged(rec_dir, start, path, dur=300, warmup=1300, source="coinbase"):
    """Spot from `warmup` seconds before the window, merged with the market's events."""
    spot = spot_events(rec_dir, (start - warmup) * 1000, (start + dur + 30) * 1000, source)
    return heapq.merge(spot, market_events(path), key=lambda e: e[1])
