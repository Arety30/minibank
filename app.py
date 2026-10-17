"""FastAPI front for the MiniBank gateway. Run: ./run.sh  → http://localhost:8777"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import gateway as gw

app = FastAPI(title="Abrar MiniBank Gateway", version="0.1")
STATIC = Path(__file__).parent / "static"


def run(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except gw.GatewayError as e:
        raise HTTPException(e.code, str(e))


class NewAccount(BaseModel):
    name: str
    with_btc: bool = True


class IssueCard(BaseModel):
    account_id: int
    kind: str = "debit"
    credit_limit: int = 0     # cents


class NewWallet(BaseModel):
    account_id: int
    handle: str
    pin: str
    nfc_uid: str | None = None


class Source(BaseModel):
    pan: str | None = None
    expiry: str | None = None
    cvv: str | None = None
    wallet: str | None = None
    pin: str | None = None

    def as_dict(self):
        if self.pan:
            return {"pan": self.pan, "expiry": self.expiry or "", "cvv": self.cvv or ""}
        if self.wallet:
            return {"wallet": self.wallet, "pin": self.pin or ""}
        raise HTTPException(400, "give card (pan/expiry/cvv) or wallet (wallet/pin)")


class Payment(BaseModel):
    source: Source
    destination: str          # card number or wallet handle / NFC uid
    amount: int               # cents
    memo: str = ""


class Topup(BaseModel):
    destination: str
    amount: int
    memo: str = "cash top-up"


class Withdraw(BaseModel):
    source: Source
    amount: int


class CardStatus(BaseModel):
    pan: str
    status: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/accounts")
def accounts():
    return gw.list_accounts()


@app.post("/api/accounts")
def new_account(body: NewAccount):
    return run(gw.create_account, body.name, body.with_btc)


@app.get("/api/accounts/{account_id}")
def account(account_id: int):
    return run(gw.get_account, account_id)


@app.get("/api/accounts/{account_id}/history")
def account_history(account_id: int):
    return run(gw.history, account_id)


@app.post("/api/cards")
def issue_card(body: IssueCard):
    return run(gw.issue_card, body.account_id, body.kind, body.credit_limit)


@app.post("/api/cards/status")
def card_status(body: CardStatus):
    run(gw.set_card_status, body.pan, body.status)
    return {"ok": True}


@app.post("/api/wallets")
def new_wallet(body: NewWallet):
    return run(gw.create_wallet, body.account_id, body.handle, body.pin, body.nfc_uid)


@app.post("/api/pay")
def pay(body: Payment):
    return run(gw.pay, body.source.as_dict(), body.destination, body.amount, body.memo)


@app.post("/api/topup")
def topup(body: Topup):
    return run(gw.topup, body.destination, body.amount, body.memo)


@app.post("/api/withdraw")
def withdraw(body: Withdraw):
    return run(gw.withdraw, body.source.as_dict(), body.amount)


@app.post("/api/balance")
def balance(body: Source):
    return run(gw.balance, body.as_dict())
