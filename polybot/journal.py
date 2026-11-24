"""Append-only decision journal: one JSON object per line, one file per UTC day.

Every order action, fill, rejection, risk event and settlement is written with the local
timestamp and the inputs the decision was based on, so a run can be audited afterwards.
"""
import json
import os
import time


class Journal:
    def __init__(self, directory: str, mode: str):
        self.dir, self.mode = directory, mode
        os.makedirs(directory, exist_ok=True)
        self._day, self._f = None, None

    def write(self, ts_ms: int, market: int, event: str, **fields) -> None:
        day = time.strftime("%Y%m%d", time.gmtime(ts_ms / 1000))
        if day != self._day:
            if self._f:
                self._f.close()
            self._day = day
            self._f = open(os.path.join(self.dir, f"{self.mode}-{day}.jsonl"), "a", buffering=1)
        rec = {"ts": ts_ms, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000)) + f".{ts_ms % 1000:03d}Z",
               "mode": self.mode, "market": market, "event": event}
        rec.update(fields)
        self._f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def close(self) -> None:
        if self._f:
            self._f.close(); self._f = None
