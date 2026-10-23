"""L$: balance, paying, buying objects for sale. The caps are checked here, again in the daemon (which also
counts the day's and the login's spending), and once more by the Ibasho viewer itself."""

from __future__ import annotations

from ..policy import BALANCE_PATH
from .common import Ctx, Fail, name_of, names, need_ibasho, resolve, rid


def register(sub):
    sub.add_parser("balance", help="L$ balance").set_defaults(fn=balance)
    p = sub.add_parser("pay", help="pay an avatar or an object that takes money")
    p.add_argument("who")
    p.add_argument("amount", type=int)
    p.add_argument("--desc", default="")
    p.set_defaults(fn=pay)
    p = sub.add_parser("buy", help="buy an object for sale, at its price")
    p.add_argument("id")
    p.add_argument("price", type=int, help="the price you expect (must match)")
    p.set_defaults(fn=buy)


def balance(ctx: Ctx, a):
    if ctx.api.ibasho:
        r = ctx.api.op("IbashoAgent", "balance")
        print(f"L${r['balance']}" + ("" if r.get("known") else " (not known yet)"))
        return
    print(ctx.api.cmd("UI", "getValue", {"path": BALANCE_PATH}).get("value"))


def cap_check(ctx: Ctx, amount: int):
    st = ctx.api.status(fresh=True)
    c = st["caps"]
    if amount <= 0:
        raise Fail("the amount must be positive")
    if amount > c["max_payment"]:
        raise Fail(f"L${amount} is over the per-payment cap (L${c['max_payment']}, caps.max_payment)")
    room = min(c["max_session_spend"] - st["spent_session"], c["max_daily_spend"] - st["spent_today"])
    if amount > room:
        raise Fail(f"L${amount} would pass the spending caps (L${room} left this login/today)")


def pay(ctx: Ctx, a):
    need_ibasho(ctx, "pay")
    cap_check(ctx, a.amount)
    _, who = resolve(ctx, a.who, ("avatar", "object"), lookup="explicit")
    r = ctx.api.op("IbashoAgent", "pay", {"to": who, "amount": a.amount, "desc": a.desc})
    n = names(ctx, [who]).get(who) if r.get("target") != "object" else None
    print(f"paid L${a.amount} to {name_of(n) + ' ' + who if n else r.get('target') or who} (watch for the money "
          "event)")


def buy(ctx: Ctx, a):
    need_ibasho(ctx, "buy")
    cap_check(ctx, a.price)
    i = rid(ctx, a.id)
    # Fresh sale info: the viewer keeps what it learned about an object for minutes, and a price may have changed.
    since = ctx.api.seq()
    ctx.api.op("IbashoObjects", "request", {"ids": [i]})
    for _ in range(4):
        if any((e.get("data") or {}).get("id") == i for e in ctx.api.events(since, wait=1, kinds=["object_properties"])):
            break
    ctx.api.op("IbashoObjects", "buy", {"id": i, "price": a.price})
    print("bought; watch for the inventory offer or an alert")
