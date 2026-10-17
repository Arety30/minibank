"""End-to-end demo against a running gateway (./run.sh)."""
import json, urllib.request
B = "http://127.0.0.1:8777/api"
def api(path, body=None):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req) as r: return json.load(r)
    except urllib.error.HTTPError as e: return {"error": e.code, "detail": json.load(e)["detail"]}
def step(title, res): print(f"\n# {title}\n  {res}")

a1 = api("/accounts", {"name": "Abrar"}); a2 = api("/accounts", {"name": "Friend"})
step("accounts", (a1["id"], a1["name"], a1["btc_address"], a2["id"], a2["name"]))
c1 = api("/cards", {"account_id": a1["id"], "kind": "debit"}); step("Abrar debit card", c1)
c2 = api("/cards", {"account_id": a2["id"], "kind": "credit", "credit_limit": 50000}); step("Friend credit card (limit 500.00)", c2)
step("Friend wallet + NFC tag", api("/wallets", {"account_id": a2["id"], "handle": "friend", "pin": "1234", "nfc_uid": "04A1B2C3"}))
card1 = {"pan": c1["pan"], "expiry": c1["expiry"], "cvv": c1["cvv"]}; card2 = {"pan": c2["pan"], "expiry": c2["expiry"], "cvv": c2["cvv"]}
step("teller top-up Abrar 1000.00", api("/topup", {"destination": c1["pan_pretty"], "amount": 100000}))
step("Abrar card -> 'friend' wallet 250.50", api("/pay", {"source": card1, "destination": "friend", "amount": 25050, "memo": "lunch"}))
step("friend wallet (PIN) -> Abrar card 100.00", api("/pay", {"source": {"wallet": "friend", "pin": "1234"}, "destination": c1["pan"], "amount": 10000}))
step("Abrar card -> NFC tag 04A1B2C3 5.00", api("/pay", {"source": card1, "destination": "04A1B2C3", "amount": 500}))
step("Friend credit card pays 600.00 (balance 155.50 + limit 500) -> approved", api("/pay", {"source": card2, "destination": c1["pan"], "amount": 60000}))
step("Friend credit card pays another 100.00 -> declined", api("/pay", {"source": card2, "destination": c1["pan"], "amount": 10000}))
step("wrong CVV", api("/pay", {"source": {**card1, "cvv": "000"}, "destination": "friend", "amount": 100}))
step("wrong PIN", api("/pay", {"source": {"wallet": "friend", "pin": "0000"}, "destination": c1["pan"], "amount": 100}))
step("unknown card", api("/pay", {"source": card1, "destination": "4111 1111 1111 1111", "amount": 100}))
step("Abrar balance", api("/balance", card1))
step("Friend balance (via wallet)", api("/balance", {"wallet": "friend", "pin": "1234"}))
step("Abrar history", [ (t["kind"], t["amount"], t["status"]) for t in api(f"/accounts/{a1['id']}/history") ])
