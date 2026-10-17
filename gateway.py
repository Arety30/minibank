"""
Abrar MiniBank — a fully local card gateway + ledger + digital wallet.

No API keys, no external services. You are the bank, the card network and the
acquirer. Cards are issued by this gateway (own BIN), so a payment only needs:
    sender:   card number + expiry + CVV   (or wallet id + PIN)
    receiver: card number                  (or wallet id)

Money units are integer cents to avoid float errors.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

DB_PATH = Path(os.environ.get("MINIBANK_DB", Path(__file__).parent / "minibank.db"))
SECRET_PATH = Path(__file__).parent / ".gateway_secret"
BIN_DEBIT = "534210"    # first 6 digits of every debit card this bank issues
BIN_CREDIT = "534211"   # ... and every credit card
CURRENCY = os.environ.get("MINIBANK_CURRENCY", "PKR")


# --------------------------------------------------------------------------- crypto helpers
def _secret() -> bytes:
    if not SECRET_PATH.exists():
        SECRET_PATH.write_bytes(secrets.token_bytes(32))
        SECRET_PATH.chmod(0o600)
    return SECRET_PATH.read_bytes()


def _hmac(value: str) -> str:
    """Keyed hash used to look cards / PINs up without storing them in clear."""
    return hmac.new(_secret(), value.encode(), hashlib.sha256).hexdigest()


def luhn_checksum(number: str) -> int:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:          # double every second digit, starting right of the check digit
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10


def luhn_valid(number: str) -> bool:
    return number.isdigit() and len(number) == 16 and luhn_checksum(number) == 0


def generate_pan(bin_prefix: str) -> str:
    body = bin_prefix + "".join(secrets.choice("0123456789") for _ in range(9))
    check = (10 - luhn_checksum(body + "0")) % 10
    return body + str(check)


def normalize_pan(pan: str) -> str:
    return "".join(c for c in pan if c.isdigit())


# --------------------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    balance     INTEGER NOT NULL DEFAULT 0,      -- cents
    btc_address TEXT,
    btc_wif     TEXT,                            -- private key; local personal bank only
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    kind         TEXT NOT NULL CHECK(kind IN ('debit','credit')),
    pan_hash     TEXT NOT NULL UNIQUE,
    last4        TEXT NOT NULL,
    expiry       TEXT NOT NULL,                  -- MM/YY
    cvv_hash     TEXT NOT NULL,
    credit_limit INTEGER NOT NULL DEFAULT 0,     -- cents, credit cards only
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wallets (
    id          TEXT PRIMARY KEY,                 -- handle e.g. "abrar" or phone number
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    pin_hash    TEXT NOT NULL,
    nfc_uid     TEXT UNIQUE,                      -- optional NFC tag bound to the wallet
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ref          TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL,                   -- payment | topup | withdraw
    from_account INTEGER,
    to_account   INTEGER,
    amount       INTEGER NOT NULL,
    memo         TEXT,
    status       TEXT NOT NULL,                   -- approved | declined
    decline_reason TEXT,
    created_at   INTEGER NOT NULL
);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)


class GatewayError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- accounts
def _btc_keypair():
    try:
        import pybtc
        k = pybtc.PrivateKey()
        return pybtc.Address(k).address, k.wif
    except Exception:
        return None, None


def create_account(name: str, with_btc: bool = True) -> dict:
    addr, wif = _btc_keypair() if with_btc else (None, None)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO accounts(name, balance, btc_address, btc_wif, created_at) VALUES (?,?,?,?,?)",
            (name, 0, addr, wif, int(time.time())),
        )
        return get_account(cur.lastrowid, conn)


def get_account(account_id: int, conn=None) -> dict:
    def _q(c):
        row = c.execute("SELECT id,name,balance,btc_address,created_at FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise GatewayError("account not found", 404)
        d = dict(row)
        d["cards"] = [dict(r) for r in c.execute(
            "SELECT id,kind,last4,expiry,credit_limit,status FROM cards WHERE account_id=?", (account_id,))]
        d["wallets"] = [dict(r) for r in c.execute(
            "SELECT id,nfc_uid,status FROM wallets WHERE account_id=?", (account_id,))]
        return d
    if conn is not None:
        return _q(conn)
    with db() as c:
        return _q(c)


def list_accounts() -> list[dict]:
    with db() as conn:
        return [get_account(r["id"], conn) for r in conn.execute("SELECT id FROM accounts ORDER BY id")]


# --------------------------------------------------------------------------- cards
def issue_card(account_id: int, kind: str = "debit", credit_limit: int = 0, years: int = 4) -> dict:
    if kind not in ("debit", "credit"):
        raise GatewayError("kind must be debit or credit")
    pan = generate_pan(BIN_CREDIT if kind == "credit" else BIN_DEBIT)
    cvv = f"{secrets.randbelow(1000):03d}"
    today = date.today()
    expiry = f"{today.month:02d}/{(today.year + years) % 100:02d}"
    with db() as conn:
        get_account(account_id, conn)
        conn.execute(
            "INSERT INTO cards(account_id,kind,pan_hash,last4,expiry,cvv_hash,credit_limit,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (account_id, kind, _hmac(pan), pan[-4:], expiry, _hmac(pan + cvv), credit_limit, int(time.time())),
        )
    # The full PAN and CVV are shown exactly once, like a real issuer.
    return {"account_id": account_id, "kind": kind, "pan": pan, "pan_pretty": " ".join(pan[i:i+4] for i in range(0, 16, 4)),
            "expiry": expiry, "cvv": cvv, "credit_limit": credit_limit}


def _card_by_pan(conn, pan: str):
    pan = normalize_pan(pan)
    if not luhn_valid(pan):
        raise GatewayError("invalid card number")
    row = conn.execute("SELECT * FROM cards WHERE pan_hash=?", (_hmac(pan),)).fetchone()
    if not row:
        raise GatewayError("card not issued by this bank", 404)
    if row["status"] != "active":
        raise GatewayError("card is " + row["status"])
    return row


def _authenticate_card(conn, pan: str, expiry: str, cvv: str):
    card = _card_by_pan(conn, pan)
    pan = normalize_pan(pan)
    if card["expiry"] != expiry.strip():
        raise GatewayError("wrong expiry date", 401)
    mm, yy = expiry.split("/")
    if (int("20" + yy), int(mm)) < (date.today().year, date.today().month):
        raise GatewayError("card expired", 401)
    if not hmac.compare_digest(card["cvv_hash"], _hmac(pan + cvv.strip())):
        raise GatewayError("wrong CVV", 401)
    return card


def set_card_status(pan: str, status: str):
    if status not in ("active", "frozen", "cancelled"):
        raise GatewayError("bad status")
    with db() as conn:
        pan = normalize_pan(pan)
        n = conn.execute("UPDATE cards SET status=? WHERE pan_hash=?", (status, _hmac(pan))).rowcount
        if not n:
            raise GatewayError("card not found", 404)


# --------------------------------------------------------------------------- wallets
def create_wallet(account_id: int, handle: str, pin: str, nfc_uid: str | None = None) -> dict:
    handle = handle.strip().lower()
    if not handle or len(pin) < 4:
        raise GatewayError("handle required and PIN must be >= 4 digits")
    with db() as conn:
        get_account(account_id, conn)
        try:
            conn.execute("INSERT INTO wallets(id,account_id,pin_hash,nfc_uid,created_at) VALUES (?,?,?,?,?)",
                         (handle, account_id, _hmac(handle + pin), nfc_uid, int(time.time())))
        except sqlite3.IntegrityError:
            raise GatewayError("wallet handle or NFC uid already used", 409)
    return {"wallet_id": handle, "account_id": account_id, "nfc_uid": nfc_uid}


def _wallet(conn, handle: str):
    row = conn.execute("SELECT * FROM wallets WHERE id=? OR nfc_uid=?", (handle.strip().lower(), handle.strip())).fetchone()
    if not row:
        raise GatewayError("wallet not found", 404)
    if row["status"] != "active":
        raise GatewayError("wallet is " + row["status"])
    return row


def _authenticate_wallet(conn, handle: str, pin: str):
    w = _wallet(conn, handle)
    if not hmac.compare_digest(w["pin_hash"], _hmac(w["id"] + pin.strip())):
        raise GatewayError("wrong PIN", 401)
    return w


# --------------------------------------------------------------------------- resolving parties
def resolve_destination(conn, dest: str) -> int:
    """A destination is a card number OR a wallet handle/NFC uid. Returns account id."""
    digits = normalize_pan(dest)
    if len(digits) == 16 and dest.replace(" ", "").isdigit():
        return _card_by_pan(conn, digits)["account_id"]
    return _wallet(conn, dest)["account_id"]


def _available(conn, account_id: int, card_row=None) -> int:
    bal = conn.execute("SELECT balance FROM accounts WHERE id=?", (account_id,)).fetchone()["balance"]
    if card_row is not None and card_row["kind"] == "credit":
        return bal + card_row["credit_limit"]
    return bal


# --------------------------------------------------------------------------- money movement
def _record(conn, kind, from_acc, to_acc, amount, memo, status, reason=None) -> dict:
    ref = "TX" + secrets.token_hex(6).upper()
    conn.execute(
        "INSERT INTO transactions(ref,kind,from_account,to_account,amount,memo,status,decline_reason,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (ref, kind, from_acc, to_acc, amount, memo, status, reason, int(time.time())))
    return {"ref": ref, "kind": kind, "from_account": from_acc, "to_account": to_acc,
            "amount": amount, "currency": CURRENCY, "status": status, "decline_reason": reason}


def pay(source: dict, destination: str, amount: int, memo: str = "") -> dict:
    """
    source = {"pan","expiry","cvv"}  or  {"wallet","pin"}
    destination = card number or wallet handle / NFC uid
    """
    if amount <= 0:
        raise GatewayError("amount must be positive")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            card = None
            if "pan" in source:
                card = _authenticate_card(conn, source["pan"], source.get("expiry", ""), source.get("cvv", ""))
                from_acc = card["account_id"]
            else:
                from_acc = _authenticate_wallet(conn, source["wallet"], source.get("pin", ""))["account_id"]
            to_acc = resolve_destination(conn, destination)
            if to_acc == from_acc:
                raise GatewayError("cannot pay yourself")
            if _available(conn, from_acc, card) < amount:
                tx = _record(conn, "payment", from_acc, to_acc, amount, memo, "declined", "insufficient funds")
                conn.execute("COMMIT")
                return tx
            conn.execute("UPDATE accounts SET balance=balance-? WHERE id=?", (amount, from_acc))
            conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, to_acc))
            tx = _record(conn, "payment", from_acc, to_acc, amount, memo, "approved")
            conn.execute("COMMIT")
            return tx
        except Exception:
            conn.execute("ROLLBACK")
            raise


def topup(destination: str, amount: int, memo: str = "cash top-up") -> dict:
    """Bank-teller action: put money into an account (cash at the counter)."""
    if amount <= 0:
        raise GatewayError("amount must be positive")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            acc = resolve_destination(conn, destination)
            conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, acc))
            tx = _record(conn, "topup", None, acc, amount, memo, "approved")
            conn.execute("COMMIT")
            return tx
        except Exception:
            conn.execute("ROLLBACK")
            raise


def withdraw(source: dict, amount: int, memo: str = "cash withdrawal") -> dict:
    if amount <= 0:
        raise GatewayError("amount must be positive")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            card = None
            if "pan" in source:
                card = _authenticate_card(conn, source["pan"], source.get("expiry", ""), source.get("cvv", ""))
                acc = card["account_id"]
            else:
                acc = _authenticate_wallet(conn, source["wallet"], source.get("pin", ""))["account_id"]
            if _available(conn, acc, card) < amount:
                tx = _record(conn, "withdraw", acc, None, amount, memo, "declined", "insufficient funds")
            else:
                conn.execute("UPDATE accounts SET balance=balance-? WHERE id=?", (amount, acc))
                tx = _record(conn, "withdraw", acc, None, amount, memo, "approved")
            conn.execute("COMMIT")
            return tx
        except Exception:
            conn.execute("ROLLBACK")
            raise


def balance(source: dict) -> dict:
    with db() as conn:
        card = None
        if "pan" in source:
            card = _authenticate_card(conn, source["pan"], source.get("expiry", ""), source.get("cvv", ""))
            acc = card["account_id"]
        else:
            acc = _authenticate_wallet(conn, source["wallet"], source.get("pin", ""))["account_id"]
        a = get_account(acc, conn)
        return {"account_id": acc, "name": a["name"], "balance": a["balance"],
                "available": _available(conn, acc, card), "currency": CURRENCY, "btc_address": a["btc_address"]}


def history(account_id: int, limit: int = 50) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT ref,kind,from_account,to_account,amount,memo,status,decline_reason,created_at FROM transactions "
            "WHERE from_account=? OR to_account=? ORDER BY id DESC LIMIT ?", (account_id, account_id, limit))
        return [dict(r) for r in rows]


init_db()
