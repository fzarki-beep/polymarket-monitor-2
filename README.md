# Polymarket Monitor

Prati Polymarket markete (prioritet nogomet/SP, plus sve ostalo po volumenu)
i šalje Telegram alert kad detektira:
- 🐋 whale trade (default ≥ $500 po jednoj transakciji)
- 📈 nagli pomak cijene (default ≥ 7 centi unutar jednog ciklusa provjere)
- 📊 volume spike (default ≥ $2.000 prometa na jednom marketu unutar jednog ciklusa)

Vrti se na GitHub Actionsu svakih 10 minuta, besplatno, bez potrebe za
ostavljenim laptopom.

---

## 1. Napravi Telegram bota (2 minute)

1. Otvori Telegram, potraži **@BotFather**, pošalji mu `/newbot`.
2. Daj botu ime i username (username mora završavati na `bot`, npr. `filip_polymarket_bot`).
3. BotFather će ti vratiti **token** — izgleda otprilike ovako:
   `123456789:AAEhBOweik6ad6PsVZTKzM7Wt2bmEEcfdDQ`
   To je tvoj `TELEGRAM_BOT_TOKEN`.
4. Pošalji svom novom botu bilo koju poruku (npr. "hej") da ga "aktiviraš".
5. Otvori u browseru:
   `https://api.telegram.org/bot<TVOJ_TOKEN>/getUpdates`
   (zamijeni `<TVOJ_TOKEN>` stvarnim tokenom)
6. U JSON odgovoru potraži `"chat":{"id":123456789, ...}` — taj broj je tvoj
   `TELEGRAM_CHAT_ID`.

## 2. Stavi kod na GitHub

1. Napravi novi repozitorij na GitHubu. **Preporuka: javni repo** — na
   javnim repozitorijima su GitHub Actions minute neograničene i besplatne,
   dok privatni repo ima 2.000 min/mjesečno što 10-minutni cron potroši
   za ~2-3 tjedna. Secrets (Telegram token) su sigurni i u javnom repu —
   ne vide se nikome i ne ispisuju se u logove. U kodu nema ničeg osjetljivog.
   Ako ipak želiš privatni repo, povećaj cron interval na `*/20` ili `*/30`.
2. Pushaj sav sadržaj ovog foldera u taj repo.

## 3. Dodaj secrets

U repu: **Settings → Secrets and variables → Actions → New repository secret**

- `TELEGRAM_BOT_TOKEN` — token iz koraka 1
- `TELEGRAM_CHAT_ID` — chat ID iz koraka 1

## 4. Pokreni

Workflow se automatski pokreće svakih 10 minuta čim je pushan na `main`
granu. Za ručni test odmah: u repu idi na **Actions → Polymarket Monitor →
Run workflow**.

---

## Prilagodba pragova

U `.github/workflows/monitor.yml`, sekcija `env:`, možeš dodati:

```yaml
env:
  TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
  TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
  WHALE_USD_THRESHOLD: "300"      # niži prag = više alerta
  PRICE_MOVE_THRESHOLD: "0.05"
  VOLUME_SPIKE_USD: "1500"
  SOCCER_MARKET_LIMIT: "40"
  OTHER_MARKET_LIMIT: "15"
```

## Lokalni test prije pushanja

```bash
cd polymarket-monitor
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python monitor.py
```

Prvi run gleda samo zadnjih 15 minuta unazad da te ne zatrpa starim
transakcijama. Ako ne dobiješ ništa odmah, to je očekivano — pričekaj
sljedeći ciklus ili smanji pragove za test.

## Napomena o pouzdanosti API-ja

Polymarket povremeno mijenja detalje svojih javnih API-ja (Gamma i Data).
Ako skripta počne vraćati prazne rezultate ili greške u logu, prva stvar
koju provjeriti je https://docs.polymarket.com — strukture poziva u
`monitor.py` su točne na dan pisanja, ali nisu garantirano vječne.
