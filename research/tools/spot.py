"""Load Binance bulk 1s kline zips (data.binance.vision) into a dense per-second close array.

usage: spot.py ZIP_DIR OUT.npz
Output: t0 (epoch second of element 0) and close[] with one element per second; gaps forward-filled.
"""
import glob
import io
import sys
import zipfile

import numpy as np


def load_dir(zip_dir):
    parts = []
    for path in sorted(glob.glob(zip_dir + "/*.zip")):
        with zipfile.ZipFile(path) as z:
            raw = z.read(z.namelist()[0])
        a = np.loadtxt(io.BytesIO(raw), delimiter=",", usecols=(0, 4))
        ts = a[:, 0]
        ts = np.where(ts > 1e14, ts / 1e6, ts / 1e3)  # microseconds since 2025, milliseconds before
        parts.append(np.column_stack([ts, a[:, 1]]))
    a = np.concatenate(parts)
    t0, t1 = int(a[0, 0]), int(a[-1, 0])
    close = np.full(t1 - t0 + 1, np.nan)
    close[(a[:, 0] - t0).astype(int)] = a[:, 1]
    idx = np.where(~np.isnan(close), np.arange(len(close)), 0)
    np.maximum.accumulate(idx, out=idx)
    return t0, close[idx]


if __name__ == "__main__":
    t0, close = load_dir(sys.argv[1])
    np.savez_compressed(sys.argv[2], t0=t0, close=close)
    print(f"t0={t0} seconds={len(close)} first={close[0]} last={close[-1]}")
