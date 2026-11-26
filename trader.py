"""Trader fictif Polymarket. Aucun ordre reel, aucun identifiant.
pip install requests anthropic ; export ANTHROPIC_API_KEY=...
Cron : */10 * * * * cd /chemin && python trader.py
"""
import json, re, os, time, datetime as dt, requests, anthropic

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com/book"
MODEL = "claude-sonnet-5-5"
MAX_ANALYSES = 25        # marches analyses par cycle (cout)
ECART_MIN, MAX_SLIP, KELLY_CAP, MAX_OPEN = 0.08, 0.03, 0.06, 5
STATE, LOG = "state.json", "paris.txt"
client = anthropic.Anthropic()

def now(): return dt.datetime.now(dt.timezone.utc)
def log(msg): open(LOG, "a").write(f"[{now():%Y-%m-%d %H:%M}] {msg}\n")

def load():
    if os.path.exists(STATE): return json.load(open(STATE))
    return {"cash": 50.0, "peak": 50.0, "positions": [], "stopped": False}
def save(s): json.dump(s, open(STATE, "w"), indent=1)

def book(token):
    b = requests.get(CLOB, params={"token_id": token}, timeout=15).json()
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))
    return bids, asks

def sell_proceeds(bids, shares):
    got, left = 0.0, shares
    for p, sz in bids:
        t = min(left, sz); got += t * p; left -= t
        if left <= 1e-9: return got
    return None  # carnet trop mince

def buy_cost(asks, budget):
    shares, spent = 0.0, 0.0
    for p, sz in asks:
        t = min(sz, (budget - spent) / p)
        shares += t; spent += t * p
        if spent >= budget - 1e-9: break
    return shares, spent

def kelly_stake(q, price, cash):  # q = ma proba du camp achete
    f = (q - price) / (1 - price)
    return max(0.0, min(f / 2, KELLY_CAP)) * cash

def fetch_markets():
    out = []
    for off in (0, 500):
        out += requests.get(GAMMA, params={"active": "true", "closed": "false",
               "limit": 500, "offset": off, "order": "volume24hr", "ascending": "false"}, timeout=30).json()
    keep = []
    for m in out:
        try:
            end = dt.datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
            if (end - now()).total_seconds() < 7200: continue
            if float(m.get("liquidityNum") or m.get("liquidity") or 0) < 5000: continue
            if not m.get("enableOrderBook"): continue
            m["_end"] = end; keep.append(m)
        except Exception: continue
    return keep

def estimate(m, price_yes):
    prompt = f"""Marche de prediction (simulation).
Question: {m['question']}
Regles de resolution (integrales): {m.get('description')}
Source indiquee: {m.get('resolutionSource')}
Fin: {m['endDate']} | Prix actuel YES: {price_yes:.3f}
1) Identifie la source officielle qui tranche. Si aucune, source_officielle=null.
2) Cherche des infos recentes et verifiables (sources fiables, ignore promo et comptes automatises).
3) Donne ta probabilite de YES, sans te caler sur le prix.
Reponds UNIQUEMENT par un JSON: {{"source_officielle": str|null, "prob_yes": float, "raison": str}}"""
    r = client.messages.create(model=MODEL, max_tokens=1500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}])
    txt = "".join(b.text for b in r.content if b.type == "text")
    j = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
    return j if j.get("source_officielle") else None

def mark(pos):
    bids, _ = book(pos["token"])
    return bids

def equity(s):
    v = s["cash"]
    for p in s["positions"]:
        bids = mark(p); v += p["shares"] * (bids[0][0] if bids else 0)
    return v

def review(s, markets):
    by_id = {m["id"]: m for m in markets}
    for p in list(s["positions"]):
        m = by_id.get(p["id"])
        reason = None
        if m is None:
            reason = "marche absent de la liste (cloture ou filtre)"
        else:
            if (m["_end"] - now()).total_seconds() < 3600: reason = "fin dans moins d'1h"
            else:
                py = float(json.loads(m["outcomePrices"])[0])
                est = estimate(m, py)
                if est:
                    q = est["prob_yes"] if p["side"] == "YES" else 1 - est["prob_yes"]
                    price = py if p["side"] == "YES" else 1 - py
                    if abs(q - p["prob"]) > 0.05: reason = f"proba bougee ({p['prob']:.2f}->{q:.2f})"
                    elif q - price <= 0.0: reason = "ecart referme"
        if reason:
            bids = mark(p); got = sell_proceeds(bids, p["shares"]) or p["shares"] * (bids[0][0] if bids else 0)
            s["cash"] += got; s["positions"].remove(p)
            log(f"SORTIE {p['question']} | {p['side']} | recu {got:.2f} $ (mise {p['stake']:.2f}) | {reason}")

def main():
    s = load()
    if s["stopped"]: return
    markets = fetch_markets()
    review(s, markets)
    eq = equity(s); s["peak"] = max(s["peak"], eq)
    if eq <= 0 or eq <= 0.6 * s["peak"]:
        s["stopped"] = True; save(s)
        log(f"ARRET : equity {eq:.2f} $ vs plus haut {s['peak']:.2f} $ (-40%). Bilan : voir etat ci-dessus.")
        print("ARRET : drawdown 40 % atteint, voir paris.txt"); return
    held = {p["id"] for p in s["positions"]}
    for m in markets[:MAX_ANALYSES]:
        if len(s["positions"]) >= MAX_OPEN: break
        if m["id"] in held: continue
        try:
            py = float(json.loads(m["outcomePrices"])[0])
            est = estimate(m, py)
            if not est: continue
            qy = float(est["prob_yes"])
            if abs(qy - py) <= ECART_MIN: continue
            side = "YES" if qy > py else "NO"
            q = qy if side == "YES" else 1 - qy
            tok = json.loads(m["clobTokenIds"])[0 if side == "YES" else 1]
            bids, asks = book(tok)
            if not asks or not bids: continue
            stake = kelly_stake(q, asks[0][0], s["cash"])
            if stake < 0.5: continue
            shares, spent = buy_cost(asks, stake)
            got = sell_proceeds(bids, shares)
            if got is None or got < spent * (1 - MAX_SLIP): continue  # revente > 3 % de perte
            s["cash"] -= spent
            s["positions"].append({"id": m["id"], "question": m["question"], "side": side,
                "token": tok, "shares": shares, "stake": spent, "prob": q})
            log(f"ENTREE {m['question']} | {side} | ma proba {q:.2f} | prix {spent/shares:.3f} | mise {spent:.2f} $ | "
                f"sortie: ecart referme ou proba +/-5 pts ou fin <1h | raison: {est['raison']}")
        except Exception as e:
            log(f"ERREUR {m.get('question','?')[:60]}: {e}")
    eq = equity(s); s["peak"] = max(s["peak"], eq); save(s)
    log(f"FIN CYCLE : equity {eq:.2f} $ | plus haut {s['peak']:.2f} $ | ouverts {len(s['positions'])}")

if __name__ == "__main__":
    main()
