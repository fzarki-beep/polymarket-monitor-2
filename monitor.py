#!/usr/bin/env python3
"""
Polymarket monitor v2 — prati nogometne/SP markete (prioritet) i ostale
top markete po volumenu, detektira whale trade-ove, pomake cijene i
volume spike-ove, te šalje Telegram alert.

Pokreće se preko GitHub Actions na cron rasporedu. State (timestamp,
snapshot cijena, viđeni tx hashevi) sprema se u state.json koji se
commita natrag u repo nakon svakog runa.

Javni API-ji (bez autentikacije za čitanje):
  - https://gamma-api.polymarket.com   (popis marketa, cijene, metapodaci)
  - https://data-api.polymarket.com    (transakcije)

NAPOMENA: Polymarket povremeno mijenja detalje API-ja. Ako pozivi počnu
vraćati prazno/greške, provjeri https://docs.polymarket.com
"""

import os
import sys
import json
import time
import requests

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# ---- Pragovi (defaulti, mijenjaj preko env varijabli u workflow-u) ----
WHALE_USD_THRESHOLD = float(os.environ.get("WHALE_USD_THRESHOLD", 500))
PRICE_MOVE_THRESHOLD = float(os.environ.get("PRICE_MOVE_THRESHOLD", 0.07))
VOLUME_SPIKE_USD = float(os.environ.get("VOLUME_SPIKE_USD", 2000))

SOCCER_MARKET_LIMIT = int(os.environ.get("SOCCER_MARKET_LIMIT", 30))
OTHER_MARKET_LIMIT = int(os.environ.get("OTHER_MARKET_LIMIT", 20))

# Prvi run (ili run nakon reseta statea) gleda samo ovoliko unazad
FIRST_RUN_LOOKBACK_S = 15 * 60
# Koliko dugo pamtimo viđene tx hasheve (dedup preko granice timestampa)
SEEN_TX_TTL_S = 60 * 60

SOCCER_KEYWORDS = [
    "soccer", "football", "world cup", "fifa", "uefa", "premier league",
    "champions league", "la liga", "bundesliga", "serie a", "ligue 1",
    "europa league", "copa", "mls",
    "fifwc",  # slug prefiks za FIFA World Cup utakmice (npr. fifwc-esp-aut-...)
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

session = requests.Session()
session.headers.update({"User-Agent": "polymarket-monitor/2.0"})


# --------------------------------------------------------------------- state

def load_state():
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            state = {}
    now = int(time.time())
    # last_timestamp == 0 ili nepostojeći → tretiraj kao svježi start
    if not state.get("last_timestamp"):
        state["last_timestamp"] = now - FIRST_RUN_LOOKBACK_S
    state.setdefault("prices", {})      # conditionId -> zadnja poznata cijena (outcome[0])
    state.setdefault("seen_tx", {})     # txHash -> timestamp kad smo ga vidjeli
    return state


def save_state(state):
    # Očisti stare tx hasheve da state.json ne raste unedogled
    cutoff = int(time.time()) - SEEN_TX_TTL_S
    state["seen_tx"] = {h: ts for h, ts in state["seen_tx"].items() if ts > cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ------------------------------------------------------------------- markets

def fetch_active_markets(limit=250):
    params = {
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
        "limit": limit,
    }
    try:
        r = session.get(f"{GAMMA_API}/markets", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"[warn] fetch_active_markets failed: {e}", file=sys.stderr)
        return []


def is_soccer_market(market):
    text = " ".join([
        str(market.get("question", "")),
        str(market.get("slug", "")),
        str(market.get("category", "") or ""),
    ]).lower()
    return any(kw in text for kw in SOCCER_KEYWORDS)


def market_price(market):
    """Cijena prvog outcome-a (Yes) iz Gamma podataka, ili None.

    Gamma vraća outcomePrices kao JSON-encoded string, npr. '["0.45", "0.55"]'.
    """
    raw = market.get("outcomePrices")
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        return float(raw[0])
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        return None


def select_markets_to_watch():
    all_markets = fetch_active_markets()
    soccer = [m for m in all_markets if is_soccer_market(m)][:SOCCER_MARKET_LIMIT]
    soccer_ids = {m.get("conditionId") for m in soccer}
    others = [m for m in all_markets
              if m.get("conditionId") not in soccer_ids][:OTHER_MARKET_LIMIT]
    return soccer, others


# -------------------------------------------------------------------- trades

def fetch_trades_batch(condition_ids, limit=200):
    """Povuče transakcije za više marketa odjednom (comma-separated IDs)."""
    if not condition_ids:
        return []
    params = {"market": ",".join(condition_ids), "limit": limit}
    try:
        r = session.get(f"{DATA_API}/trades", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except requests.RequestException as e:
        print(f"[warn] fetch_trades_batch failed: {e}", file=sys.stderr)
        return []


def trade_usd_size(trade):
    if trade.get("usdcSize") is not None:
        try:
            return float(trade["usdcSize"])
        except (TypeError, ValueError):
            pass
    try:
        return float(trade.get("size", 0)) * float(trade.get("price", 0))
    except (TypeError, ValueError):
        return 0.0


# -------------------------------------------------------------------- alerts

def market_url(market):
    slug = market.get("slug", "")
    return f"https://polymarket.com/event/{slug}" if slug else ""


def analyze_trades(market, trades, tag):
    """Whale + volume spike detekcija nad novim transakcijama jednog marketa.

    Whale transakcije se agregiraju po (side, outcome): jedan veliki order
    na CLOB-u se često matchira protiv desetaka protustrana, pa API vraća
    puno zasebnih fillova iste cijene — to je JEDAN događaj, ne dvadeset.
    """
    alerts = []
    title = market.get("question") or market.get("slug") or "?"
    url = market_url(market)

    total_usd = 0.0
    whales = {}  # (side, outcome) -> {"usd": suma, "count": n, "prices": [..]}
    for t in trades:
        usd = trade_usd_size(t)
        total_usd += usd
        if usd >= WHALE_USD_THRESHOLD:
            key = (t.get("side", "?"), t.get("outcome", "?"))
            w = whales.setdefault(key, {"usd": 0.0, "count": 0, "prices": []})
            w["usd"] += usd
            w["count"] += 1
            if t.get("price") is not None:
                try:
                    w["prices"].append(float(t["price"]))
                except (TypeError, ValueError):
                    pass

    for (side, outcome), w in whales.items():
        if w["count"] == 1:
            price_txt = f"{w['prices'][0]:.3f}" if w["prices"] else "?"
            detail = f"${w['usd']:,.0f} po cijeni {price_txt}"
        else:
            lo, hi = (min(w["prices"]), max(w["prices"])) if w["prices"] else (0, 0)
            price_txt = f"{lo:.3f}" if abs(hi - lo) < 0.0005 else f"{lo:.3f}–{hi:.3f}"
            detail = f"${w['usd']:,.0f} ukupno u {w['count']} fillova po cijeni {price_txt}"
        alerts.append(
            f"🐋 WHALE [{tag}] {title}\n"
            f"   {side} {outcome} — {detail}\n"
            f"   {url}"
        )

    if total_usd >= VOLUME_SPIKE_USD:
        alerts.append(
            f"📊 VOLUME SPIKE [{tag}] {title}\n"
            f"   ${total_usd:,.0f} prometa u ovom ciklusu ({len(trades)} transakcija)\n"
            f"   {url}"
        )
    return alerts


def analyze_price_move(market, prev_price, tag):
    """Usporedba trenutne cijene marketa s cijenom iz prošlog ciklusa.

    Uspoređujemo uvijek cijenu istog outcome-a (prvog), pa nema miješanja
    Yes/No strana — pomak je stvarni pomak vjerojatnosti marketa.
    """
    cur = market_price(market)
    if cur is None or prev_price is None:
        return None, cur
    move = cur - prev_price
    if abs(move) >= PRICE_MOVE_THRESHOLD - 1e-9:
        title = market.get("question") or market.get("slug") or "?"
        arrow = "↑" if move > 0 else "↓"
        alert = (
            f"📈 POMAK CIJENE [{tag}] {title}\n"
            f"   {prev_price:.2f} → {cur:.2f} ({arrow} {abs(move):.2f} od prošle provjere)\n"
            f"   {market_url(market)}"
        )
        return alert, cur
    return None, cur


# ------------------------------------------------------------------ telegram

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[info] Telegram nije konfiguriran, ispisujem u log:\n" + message)
        return
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message[:4000],  # Telegram limit je 4096 znakova
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not r.ok:
            print(f"[warn] Telegram send failed: {r.status_code} {r.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"[warn] Telegram send exception: {e}", file=sys.stderr)


def send_alerts(alerts):
    chunk, chunk_len = [], 0
    for alert in alerts:
        if chunk_len + len(alert) > 3500 and chunk:
            send_telegram("\n\n".join(chunk))
            chunk, chunk_len = [], 0
        chunk.append(alert)
        chunk_len += len(alert) + 2
    if chunk:
        send_telegram("\n\n".join(chunk))


# ---------------------------------------------------------------------- main

def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main():
    state = load_state()
    last_ts = int(state["last_timestamp"])
    seen_tx = state["seen_tx"]
    now = int(time.time())
    new_max_ts = last_ts

    soccer_markets, other_markets = select_markets_to_watch()
    print(f"[info] Pratim {len(soccer_markets)} nogometnih + {len(other_markets)} ostalih marketa")
    if not soccer_markets and not other_markets:
        print("[error] Gamma API nije vratio nijedan market — preskačem ciklus, state ostaje netaknut", file=sys.stderr)
        sys.exit(1)

    all_alerts = []
    tagged = [("FOOTBALL", m) for m in soccer_markets] + [("OSTALO", m) for m in other_markets]
    by_cid = {m.get("conditionId"): (tag, m) for tag, m in tagged if m.get("conditionId")}

    # --- 1) Pomaci cijene: usporedba sa snapshotom iz prošlog ciklusa ---
    new_prices = {}
    for cid, (tag, market) in by_cid.items():
        prev = state["prices"].get(cid)
        alert, cur = analyze_price_move(market, prev, tag)
        if alert:
            all_alerts.append(alert)
        if cur is not None:
            new_prices[cid] = cur
    state["prices"] = new_prices  # markete koji su ispali iz praćenja čistimo

    # --- 2) Transakcije: batch dohvat, pa whale/volume analiza po marketu ---
    trades_by_cid = {}
    cids = list(by_cid.keys())
    for batch in chunked(cids, 20):
        for t in fetch_trades_batch(batch):
            ts = int(t.get("timestamp", 0))
            tx = t.get("transactionHash") or ""
            if ts <= last_ts or (tx and tx in seen_tx):
                continue
            cid = t.get("conditionId")
            if cid in by_cid:
                trades_by_cid.setdefault(cid, []).append(t)
                new_max_ts = max(new_max_ts, ts)
                if tx:
                    seen_tx[tx] = now
        time.sleep(0.2)

    for cid, trades in trades_by_cid.items():
        tag, market = by_cid[cid]
        all_alerts.extend(analyze_trades(market, trades, tag))

    # --- 3) Slanje i spremanje statea ---
    if all_alerts:
        send_alerts(all_alerts)
        print(f"[info] Poslano {len(all_alerts)} alert(a)")
    else:
        print("[info] Nema alert-vrijednih događaja ovaj ciklus")

    state["last_timestamp"] = new_max_ts
    save_state(state)


if __name__ == "__main__":
    main()
