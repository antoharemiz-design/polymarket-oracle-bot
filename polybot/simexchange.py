"""A simulated Polymarket for the real SDK (polymarket-client): the CLOB, the relayer and the Polygon RPC behind an
httpx MockTransport, so LiveExec runs its real calls (order signing and validation, market orders, merges through
the relayer) without the network and without money.

Why: on 25 Nov every live taker close failed inside the SDK ("amount is required for BUY market orders") and the
pairs were never merged, while the unit tests passed: they used a fake client that accepted any arguments. Here
the SDK itself builds, validates and signs every request, so a wrong call fails the same way it fails live.

    sim = SimExchange()                          # one market: sim.cond, sim.yes, sim.no; tick 0.01
    with sim.patch():                            # every httpx client of the SDK talks to the simulator
        ex = await lp.LiveExec.create(key, None, relayer_key, relayer_address)
        ...
    sim.trade(token, "SELL", 0.48, 20)           # another trader hits the bids: our resting BUYs fill first

Simulated: order matching (price-time against the other orders of the book, our resting orders first at their
price), post-only rejection, FAK/FOK market orders with maker/taker amounts as the CLOB reports them, balances and
allowances, the account trade list, cancels, merges (the relayer executes them only with an API key, as the real
relayer does; the RPC answers balanceOfBatch for the merge amount). Not simulated: fees, neg-risk markets, the
websocket, rewards. Only for tests and research; never imported by the live service.
"""
import contextlib
import itertools
import json
import time
import uuid
from decimal import Decimal

import httpx

E6 = 10 ** 6


class SimExchange:
    def __init__(self, cond=None, tick=0.01, bids=((0.48, 500.0), (0.47, 500.0)), asks=((0.52, 500.0), (0.53, 500.0)),
                 usdc=300.0, relayer=True, clock=time.time):
        self.cond = cond or "0x" + "ab" * 32
        self.yes, self.no = "1" + "1" * 70, "2" + "2" * 70
        self.tick = tick
        self.book = {self.yes: {"BUY": [list(x) for x in bids], "SELL": [list(x) for x in asks]}}
        self.book[self.no] = {"BUY": [[round(1 - p, 6), z] for p, z in asks], "SELL": [[round(1 - p, 6), z] for p, z in bids]}
        self.bal = {"USDC": usdc, self.yes: 0.0, self.no: 0.0}
        self.relayer_enabled = relayer               # False: the relayer refuses (no API key accepted)
        self.clock = clock                           # trade times (a scenario may run on its own clock)
        self.orders = {}                             # our resting orders: id -> dict
        self.trades = []                             # account trades (ClobTrade JSON)
        self.requests = []                           # (method, host, path) of every request, for the tests
        self.merges = []                             # merged amounts (shares)
        self.wallet = None
        self.txs = {}
        self._n = itertools.count(1)

    # ---- order book -----------------------------------------------------------------------------------------
    def best(self, token, side):
        lv = sorted(self.book[token][side], key=lambda x: -x[0] if side == "BUY" else x[0])
        mine = [(o["price"], o["left"]) for o in self.orders.values() if o["token"] == token and o["side"] == side]
        allp = [p for p, _ in lv] + [p for p, _ in mine]
        if not allp:
            return None
        return max(allp) if side == "BUY" else min(allp)

    def _other(self, token):
        return self.no if token == self.yes else self.yes

    def _match_taker(self, token, side, price_limit, shares=None, usdc=None):
        """Our taker order against the other orders of the book (not against our own). Returns (shares, usdc)."""
        opp = "SELL" if side == "BUY" else "BUY"
        lv = sorted(self.book[token][opp], key=lambda x: x[0] if opp == "SELL" else -x[0])
        got_sh = got_usd = 0.0
        for level in lv:
            p, z = level
            if (side == "BUY" and p > price_limit + 1e-9) or (side == "SELL" and p < price_limit - 1e-9):
                break
            if side == "BUY":
                room = (usdc - got_usd) / p
            else:
                room = shares - got_sh
            q = min(z, room)
            if q <= 1e-9:
                break
            level[1] -= q
            got_sh += q
            got_usd += q * p
        self.book[token][opp] = [x for x in self.book[token][opp] if x[1] > 1e-9]
        return got_sh, got_usd

    def trade(self, token, side, price, size):
        """Another trader's taker order of `size` at `price` on `token` (side is the taker's side): our resting orders
        at a crossing price fill first (best price first)."""
        opp = "BUY" if side == "SELL" else "SELL"
        left = size
        for oid, o in sorted(self.orders.items(), key=lambda kv: -kv[1]["price"] if opp == "BUY" else kv[1]["price"]):
            if o["token"] != token or o["side"] != opp or left <= 1e-9:
                continue
            if (opp == "BUY" and o["price"] < price - 1e-9) or (opp == "SELL" and o["price"] > price + 1e-9):
                continue
            q = min(o["left"], left)
            o["left"] -= q
            left -= q
            if opp == "BUY":
                self.bal["USDC"] -= q * o["price"]
                self.bal[token] += q
            else:
                self.bal[token] -= q
                self.bal["USDC"] += q * o["price"]
            self._record(token, opp, o["price"], q, maker_order=oid)
            if o["left"] <= 1e-9:
                del self.orders[oid]
        return size - left

    def _record(self, token, our_side, price, size, maker_order=None, taker_order=None):
        now = int(self.clock())
        mo = []
        if maker_order:
            mo = [{"order_id": maker_order, "asset_id": token, "maker_address": self.wallet or "0x0", "owner": "k",
                   "side": our_side, "price": str(price), "matched_amount": str(round(size, 6)),
                   "outcome": "Yes" if token == self.yes else "No", "fee_rate_bps": "0"}]
        self.trades.append({
            "id": f"t{next(self._n)}", "market": self.cond, "asset_id": token, "owner": "k",
            "maker_address": self.wallet or "0x0", "taker_order_id": taker_order or f"x{next(self._n)}",
            "side": our_side, "trader_side": "MAKER" if maker_order else "TAKER", "price": str(price),
            "size": str(round(size, 6)), "outcome": "Yes" if token == self.yes else "No", "status": "MATCHED",
            "fee_rate_bps": "0", "bucket_index": 0, "transaction_hash": "0x" + "00" * 32, "maker_orders": mo,
            "match_time": str(now), "last_update": str(now)})

    # ---- http ---------------------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def patch(self):
        orig = httpx.AsyncClient.__init__
        sim = self

        def init(client, *a, **kw):
            kw["transport"] = httpx.MockTransport(sim.handle)
            kw.pop("http2", None)
            orig(client, *a, **kw)
        httpx.AsyncClient.__init__ = init
        try:
            yield self
        finally:
            httpx.AsyncClient.__init__ = orig

    def handle(self, req):
        host, path, q = req.url.host, req.url.path, dict(req.url.params)
        self.requests.append((req.method, host, path))
        body = json.loads(req.content) if req.content else None
        try:
            if host == "relayer-v2.polymarket.com":
                return self._relayer(req.method, path, q, body, req.headers)
            if host == "gamma-api.polymarket.com":
                return self._gamma(path, q)
            if "drpc" in host or "rpc" in host:
                return self._rpc(body)
            return self._clob(req.method, path, q, body)
        except Exception as e:                           # a bug of the simulator, not of the code under test
            return httpx.Response(500, json={"error": f"simulator: {e!r}"})

    def _clob(self, method, path, q, body):
        ok = lambda d: httpx.Response(200, json=d)
        if path == "/auth/api-keys":
            return ok({"apiKeys": ["k"]})
        if path in ("/auth/derive-api-key", "/auth/api-key") and method in ("GET", "POST"):
            return ok({"apiKey": "k", "secret": "c2VjcmV0", "passphrase": "p"})
        if path.startswith("/clob-markets/"):
            return ok({"t": [{"t": self.yes, "o": "Yes"}, {"t": self.no, "o": "No"}], "mts": self.tick, "nr": False,
                       "fd": {"r": 0, "e": 1}})
        if path.startswith("/markets-by-token/"):
            return ok({"condition_id": self.cond})
        if path == "/tick-size":
            return ok({"minimum_tick_size": self.tick})
        if path == "/neg-risk":
            return ok({"neg_risk": False})
        if path == "/balance-allowance":
            tok = q.get("token_id") or q.get("asset_id")
            key = "USDC" if q.get("asset_type") == "COLLATERAL" else tok
            return ok({"balance": str(int(round(self.bal.get(key, 0.0) * E6))),
                       "allowances": {"0xE111180000d2663C0091e4f400237545B87B996B": str(10 ** 30)}})
        if path == "/order" and method == "POST":
            return ok(self._post_order(body))
        if path == "/order" and method == "DELETE":
            oid = body.get("orderID") if body else q.get("orderID")
            gone = self.orders.pop(oid, None)
            return ok({"canceled": [oid] if gone else [], "not_canceled": {} if gone else {oid: "not found"}})
        if path in ("/cancel-all", "/cancel-market-orders") and method == "DELETE":
            ids = [i for i, o in self.orders.items() if path == "/cancel-all" or o["cond"] == (body or {}).get("market", self.cond)]
            for i in ids:
                del self.orders[i]
            return ok({"canceled": ids, "not_canceled": {}})
        if path == "/data/trades":
            return ok({"data": self.trades, "next_cursor": "LTE="})
        if path == "/orders-scoring":
            ids = body if isinstance(body, list) else (body or {}).get("orderIds", [])
            return ok({i: i in self.orders for i in ids})
        if path.startswith("/rewards/user/percentages"):
            return ok({self.cond: 50.0})
        if path in ("/heartbeats", "/v1/heartbeats"):
            return ok({"heartbeat_id": "h"})
        return httpx.Response(404, json={"error": f"simulator: no route {method} {path}"})

    def _post_order(self, body):
        o = body["order"]
        token, side = o["tokenId"], o["side"]
        maker, taker = int(o["makerAmount"]) / E6, int(o["takerAmount"]) / E6
        price = maker / taker if side == "BUY" else taker / maker
        otype, post = body.get("orderType", "GTC"), body.get("postOnly", False)
        if self.wallet is None:
            self.wallet = o["maker"]
        oid = "0x" + uuid.uuid4().hex
        rej = lambda msg: {"errorMsg": msg, "makingAmount": "", "takingAmount": "", "orderID": "", "status": "",
                           "success": False}
        if otype in ("GTC", "GTD"):
            opp = self.best(token, "SELL" if side == "BUY" else "BUY")
            crosses = opp is not None and (price >= opp - 1e-9 if side == "BUY" else price <= opp + 1e-9)
            if post and crosses:
                return rej("invalid post-only order: order crosses book")
            need = maker if side == "BUY" else maker
            have = self.bal["USDC"] if side == "BUY" else self.bal[token]
            locked = sum(x["price"] * x["left"] if side == "BUY" else x["left"] for x in self.orders.values()
                         if x["side"] == side and (side == "BUY" or x["token"] == token))
            if need > have - locked + 1e-9:
                return rej(f"not enough balance / allowance: the balance is not enough -> balance: "
                           f"{int(have * E6)}, order amount: {int(need * E6)}")
            self.orders[oid] = {"token": token, "side": side, "price": round(price, 6), "size": taker if side == "BUY" else maker,
                                "left": taker if side == "BUY" else maker, "cond": self.cond}
            return {"errorMsg": "", "makingAmount": "0", "takingAmount": "0", "orderID": oid, "status": "live",
                    "success": True, "tradeIDs": [], "transactionsHashes": []}
        # FAK / FOK: BUY spends `maker` USDC at most at `price`; SELL sells `maker` shares at least at `price`
        if side == "BUY":
            if maker > self.bal["USDC"] + 1e-9:
                return rej("not enough balance / allowance: the balance is not enough")
            sh, usd = self._match_taker(token, "BUY", price, usdc=maker)
        else:
            if maker > self.bal[token] + 1e-9:
                return rej("not enough balance / allowance: the balance is not enough")
            sh, usd = self._match_taker(token, "SELL", price, shares=maker)
        if sh <= 1e-9 or (otype == "FOK" and (usd < maker - 1e-6 if side == "BUY" else sh < maker - 1e-6)):
            return rej("no orders found to match with FAK order. FAK orders are partially filled or killed if no "
                       "match is found." if otype == "FAK" else
                       "order couldn't be fully filled. FOK orders are fully filled or killed.")
        if side == "BUY":
            self.bal["USDC"] -= usd; self.bal[token] += sh
            making, taking = usd, sh
        else:
            self.bal[token] -= sh; self.bal["USDC"] += usd
            making, taking = sh, usd
        self._record(token, side, round(usd / sh, 6), sh, taker_order=oid)
        return {"errorMsg": "", "makingAmount": f"{making:.6f}", "takingAmount": f"{taking:.6f}", "orderID": oid,
                "status": "matched", "success": True, "tradeIDs": [self.trades[-1]["id"]],
                "transactionsHashes": ["0x" + "11" * 32]}

    def _gamma_market(self):
        """A gamma market in the shape of the real /markets/keyset answer (taken from the live API on 26 Nov)."""
        return {"id": "1", "question": "simulated market", "conditionId": self.cond, "slug": "sim",
                "endDate": "2030-01-01T00:00:00Z", "outcomes": json.dumps(["Yes", "No"]),
                "outcomePrices": json.dumps(["0.5", "0.5"]), "active": True, "closed": False, "enableOrderBook": True,
                "orderPriceMinTickSize": self.tick, "orderMinSize": 5, "clobTokenIds": json.dumps([self.yes, self.no]),
                "positionIds": [self.yes, self.no], "acceptingOrders": True, "negRisk": False, "negRiskOther": False,
                "feesEnabled": True, "feeType": "weather_fees",
                "feeSchedule": {"exponent": 1, "rate": 0.0, "takerOnly": True, "rebateRate": 0.25},
                "rewardsMinSize": 20, "rewardsMaxSpread": 4.5, "version": "v1", "questionID": "0x" + "cd" * 32,
                "startDate": "2026-01-01T00:00:00Z", "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z", "archived": False, "restricted": True, "approved": True,
                "ready": False, "funded": False, "cyom": False}

    def _gamma(self, path, q):
        if path == "/markets/keyset":
            return httpx.Response(200, json={"markets": [self._gamma_market()], "next_cursor": None})
        if path == "/markets":
            return httpx.Response(200, json=[self._gamma_market()])
        return httpx.Response(404, json={"error": f"simulator: no gamma route {path}"})

    def _relayer(self, method, path, q, body, headers):
        ok = lambda d: httpx.Response(200, json=d)
        if path == "/deployed":
            return ok({"deployed": True})
        if path in ("/nonce", "/relay-payload", "/v1/account/transactions/params"):
            return ok({"nonce": "0", "address": "0x" + "44" * 20})
        if path == "/submit" and method == "POST":
            if not self.relayer_enabled:
                return httpx.Response(401, json={"error": "invalid api key"})
            amount = self._merge_amount()
            tid = f"tx{next(self._n)}"
            self.txs[tid] = amount
            if amount > 0:                                  # executed at once: merge `amount` pairs into USDC
                self.bal[self.yes] -= amount; self.bal[self.no] -= amount; self.bal["USDC"] += amount
                self.merges.append(amount)
            return ok({"transactionID": tid, "state": "STATE_NEW", "transactionHash": "0x" + "22" * 32})
        if path.startswith("/v1/account/transactions/") or path.startswith("/transaction"):
            tid = q.get("id") or path.rsplit("/", 1)[-1]
            return ok({"transaction_id": tid, "transaction_hash": "0x" + "22" * 32, "state": "STATE_CONFIRMED"})
        return httpx.Response(404, json={"error": f"simulator: no relayer route {method} {path}"})

    def _merge_amount(self):
        return min(self.bal[self.yes], self.bal[self.no])

    def _rpc(self, body):
        out = []
        for call in body if isinstance(body, list) else [body]:
            m = call.get("method")
            if m == "eth_call":                              # ERC1155 balanceOfBatch(owners, ids) -> uint256[2]
                a, b = int(round(self.bal[self.yes] * E6)), int(round(self.bal[self.no] * E6))
                res = "0x" + (32).to_bytes(32, "big").hex() + (2).to_bytes(32, "big").hex() + \
                      a.to_bytes(32, "big").hex() + b.to_bytes(32, "big").hex()
            elif m == "eth_chainId":
                res = hex(137)
            elif m == "eth_blockNumber":
                res = hex(1)
            else:
                res = None
            out.append({"jsonrpc": "2.0", "id": call.get("id"), "result": res})
        return httpx.Response(200, json=out if isinstance(body, list) else out[0])
