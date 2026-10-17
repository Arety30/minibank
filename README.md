# minibank

Abrar MiniBank is a fully local card-issuing gateway by Syed Abrar Ali: a FastAPI service over SQLite that plays bank, card network and acquirer at once. It opens accounts, issues its own Luhn-valid debit and credit cards under a private BIN, creates PIN-protected digital wallets (optionally bound to an NFC tag UID), and moves money between them with an atomic approved/declined ledger. Nothing leaves the machine: no payment provider, no API keys, no sign-up. It was built to learn and experiment with how card payments, wallets and ledgers fit together without touching real money, and to give other local projects (a point-of-sale sketch, an MCP wrapper) a realistic payment back end to talk to.

## Where it is used

It runs on the owner's laptop as a user service and is reached from a browser dashboard on a local port and from scripts. A companion MCP wrapper imports `gateway.py` directly so a chat assistant can pay, top up and check balances using payment sources saved by nickname, and an NFC point-of-sale prototype in the surrounding `fintech-stack` folder uses the same wallet/UID flow as its reference.

## How it works

### `gateway.py` — the bank

Pure Python plus `sqlite3`; all public functions raise `GatewayError(message, http_code)` on failure.

Storage (`MINIBANK_DB`, default `minibank.db` next to the code; WAL, foreign keys on):

| Table | Purpose |
| --- | --- |
| `accounts` | name, `balance` in integer cents, optional Bitcoin address and key (generated with `pybtc` when available) |
| `cards` | account, kind (`debit`/`credit`), `pan_hash`, `last4`, expiry `MM/YY`, `cvv_hash`, `credit_limit`, status (`active`/`frozen`/`cancelled`) |
| `wallets` | handle (PK, lower-cased), account, `pin_hash`, optional unique `nfc_uid`, status |
| `transactions` | unique `ref` (`TX` + 12 hex), kind (`payment`/`topup`/`withdraw`), from/to account, amount, memo, `approved`/`declined`, decline reason |

Key rules:

- Card numbers: `generate_pan()` builds 16 digits from BIN `534210` (debit) or `534211` (credit), nine random digits and a Luhn check digit. `luhn_valid()` rejects malformed input before any lookup.
- Secrets are never stored in clear. A 32-byte gateway secret is created once in `.gateway_secret` (mode 600) and used for HMAC-SHA256 of the PAN, of PAN+CVV and of handle+PIN; verification uses `hmac.compare_digest`. The full PAN, expiry and CVV are returned exactly once, at issue time.
- Authentication: a card source needs PAN + matching expiry (and not past) + CVV; a wallet source needs handle + PIN. Frozen or cancelled cards and inactive wallets are refused.
- Destination resolution: a 16-digit string is a card, anything else is a wallet handle or NFC UID.
- Available funds = balance, plus `credit_limit` for a credit card source.
- `pay`, `topup` and `withdraw` run inside `BEGIN IMMEDIATE ... COMMIT` so debit and credit happen together; insufficient funds produce a `declined` row rather than an error; paying yourself is rejected.
- `history()` returns the last 50 transactions touching an account.

### `app.py` — the API

FastAPI with Pydantic models; `run()` maps `GatewayError` to `HTTPException`. Serves the dashboard at `/` and JSON routes:

| Method and path | Body | Result |
| --- | --- | --- |
| `GET /api/accounts`, `GET /api/accounts/{id}`, `GET /api/accounts/{id}/history` | — | accounts with their cards and wallets; ledger |
| `POST /api/accounts` | `{name, with_btc}` | new account |
| `POST /api/cards` | `{account_id, kind, credit_limit}` | card details shown once |
| `POST /api/cards/status` | `{pan, status}` | freeze / cancel / reactivate |
| `POST /api/wallets` | `{account_id, handle, pin, nfc_uid}` | wallet |
| `POST /api/pay` | `{source, destination, amount, memo}` | transaction record |
| `POST /api/topup` | `{destination, amount, memo}` | teller cash-in |
| `POST /api/withdraw` | `{source, amount}` | teller cash-out |
| `POST /api/balance` | `source` | balance, available, currency |

`source` is either `{pan, expiry, cvv}` or `{wallet, pin}`. Amounts are integer cents everywhere.

### `static/index.html` — dashboard

A single page with sections for accounts, card issuing (with a card visual), wallets, teller top-up/withdraw, payments and history, calling the JSON API with `fetch`.

### `demo.py`

An end-to-end script against a running server: creates two accounts, a debit and a credit card, a wallet with an NFC UID, tops up, pays card → wallet, wallet → card, card → NFC tag, exercises the credit limit (approved then declined), wrong CVV, wrong PIN and an unknown card, and prints balances and history.

### MCP wrapper

A separate `minibank-mcp` package (not part of this folder) imports `gateway.py`, keeps named payment sources in `~/.config/minibank-mcp/sources.json`, and runs as a FastMCP server over stdio or HTTP (port 8974) so an MCP client can call pay/topup/balance by nickname.

## Setup

Requirements: Python 3.11+, `fastapi`, `uvicorn`; optional `pybtc` for Bitcoin addresses (accounts are created without one if the import fails).

```bash
git clone https://github.com/USER/minibank.git
cd minibank
python -m venv ../venv && ../venv/bin/pip install fastapi uvicorn pybtc
```

`run.sh` expects the virtualenv at `../venv` (the layout of the surrounding fintech-stack folder); edit it if yours lives elsewhere.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MINIBANK_DB` | `./minibank.db` | SQLite path |
| `MINIBANK_CURRENCY` | `PKR` | Currency label returned with transactions |

`.gateway_secret` is created automatically on first use; back it up with the database, because without it no stored card or PIN hash can be verified.

## Usage

```bash
./run.sh                      # uvicorn on 0.0.0.0:8777 → http://localhost:8777
../venv/bin/python demo.py    # scripted walk-through against the running server
```

Manual flow with curl:

```bash
curl -s -X POST localhost:8777/api/accounts -H 'content-type: application/json' -d '{"name":"Alice"}'
curl -s -X POST localhost:8777/api/cards -H 'content-type: application/json' -d '{"account_id":1,"kind":"debit"}'
# -> {"pan":"5342105...", "expiry":"MM/YY", "cvv":"123", ...}  (shown once)
curl -s -X POST localhost:8777/api/topup -H 'content-type: application/json' -d '{"destination":"5342105...","amount":100000}'
curl -s -X POST localhost:8777/api/wallets -H 'content-type: application/json' -d '{"account_id":2,"handle":"shop","pin":"4321","nfc_uid":"04A1B2C3"}'
curl -s -X POST localhost:8777/api/pay -H 'content-type: application/json' \
  -d '{"source":{"pan":"5342105...","expiry":"MM/YY","cvv":"123"},"destination":"04A1B2C3","amount":2500,"memo":"coffee"}'
```

Optional user service:

```ini
[Service]
WorkingDirectory=%h/minibank
ExecStart=%h/minibank/run.sh
Restart=always
```

## Project structure

```
minibank/
├── gateway.py        # schema, HMAC helpers, Luhn, accounts/cards/wallets, atomic money movement
├── app.py            # FastAPI routes and Pydantic models
├── static/index.html # browser dashboard
├── demo.py           # end-to-end demo script
├── run.sh            # uvicorn launcher (expects ../venv)
├── minibank.db       # SQLite data (created on first run)
└── .gateway_secret   # HMAC key (created on first run, keep private)
```

## Limitations and notes

- This is a toy bank for one machine: no real card network, no settlement, no interest, no multi-user authentication on the API. Anyone who can reach port 8777 can create accounts and move the play money.
- The server binds to all interfaces by default; restrict it to loopback or a firewall if the machine is on a shared network.
- Bitcoin key pairs are generated for convenience only and the private key is stored in plain text in the database; nothing is broadcast to any network.
- Card numbers use a made-up BIN and will never work outside this gateway.
- Expiry validation compares year/month only; there is no 3-D Secure, velocity check or fraud logic (see the separate spending-guardrail project for that).
- Losing `.gateway_secret` orphans every card and wallet because only keyed hashes are stored.
