"""L2 book of the Up token in integer cents. The Down book is its exact mirror
(research/02_technical_base.md), so one book describes the whole market:
buying Down at q is selling Up at 100 - q."""


def cents(price) -> int:
    return int(round(float(price) * 100))


class UpBook:
    def __init__(self):
        self.bids = {}   # cents -> size
        self.asks = {}
        self.ready = False

    def snapshot(self, bids, asks) -> None:
        self.bids = {cents(p): float(s) for p, s in bids if float(s) > 0}
        self.asks = {cents(p): float(s) for p, s in asks if float(s) > 0}
        self.ready = True

    def change(self, side: str, price_c: int, size: float) -> None:
        d = self.bids if side == "B" else self.asks
        if size <= 0:
            d.pop(price_c, None)
        else:
            d[price_c] = size

    def best_bid(self) -> int:
        return max(self.bids) if self.bids else 0

    def best_ask(self) -> int:
        return min(self.asks) if self.asks else 100

    def size(self, side: str, price_c: int) -> float:
        return (self.bids if side == "B" else self.asks).get(price_c, 0.0)
