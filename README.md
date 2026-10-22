# Lazada drop bot

Watches one or more Lazada product listings every day between **1pm and 2pm (SGT)**
and buys as soon as a listing comes back in stock. You don't need to know the exact
drop time: each product gets its own browser tab that reloads every few seconds
during the window.

## How it works

1. You log in once in a real browser window. The session is saved in `browser-profile/`.
2. Each day at 12:58 it opens a tab per product and reloads it every ~3s (±1s jitter).
3. When **Buy Now** becomes clickable (quantity no longer "Out of stock"), it sets the
   quantity, clicks **Buy Now**, then **Place Order** on checkout, and optionally **Pay Now**.
4. It alerts you (terminal bell + optional Telegram) and leaves the browser open
   15 min so you can approve card OTP / e-wallet payment.
5. Each product is bought at most once per day (tracked in `state.json`, so restarts don't double-buy).
   At 2pm it closes the browser and sleeps until tomorrow.

If Lazada shows a slider/captcha or logs you out, the bot **does not try to bypass it**.
It alerts you and waits for you to solve it in the browser window, then carries on.

## Setup (on your own PC — Windows, macOS or Linux)

```bash
git clone https://github.com/miltani/lazada-bot && cd lazada-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp config.example.yaml config.yaml   # then edit: add your product links
python -m lazada_bot login           # log in; also check your default address & payment method
```

Before the first real run, set a **default delivery address** and ideally a **saved
payment method** in your Lazada account. The bot uses whatever checkout pre-selects.

## Running

```bash
python -m lazada_bot check                    # one-off: print price/stock for each product
python -m lazada_bot run --now 5 --dry-run    # test now: watch 5 min, stop at checkout (no order)
python -m lazada_bot run                      # real thing: runs daily 13:00-14:00 forever
```

Leave `run` going on a PC that stays awake (disable sleep). Ctrl+C to stop.

## Config (`config.yaml`)

| Key | Meaning |
|---|---|
| `products` | list of `{url, quantity, max_price, name}`; short `s.lazada.sg` links work |
| `window.start` / `window.end` | daily window, in `timezone` |
| `poll_seconds` / `poll_jitter` | reload speed. Faster gets you in sooner but triggers captchas more often |
| `auto_place_order` | click "Place Order" on checkout (default on) |
| `auto_pay` | also click the final "Pay Now" (default off; card OTP still needs you) |
| `max_price` | per product: don't buy if price is above this |
| `telegram.bot_token` / `chat_id` | phone alerts — create a bot with @BotFather |

## Tips for winning drops

- Run on a wired connection; keep the number of products modest (each is a tab).
- Use a fast payment method with no extra steps (e.g. saved card / Lazada Wallet with balance).
- Have your phone ready for OTP: the order is only reserved for a short time.
- Lazada's terms may restrict automated purchasing, and listings like
  "Limit 2 per person" are enforced server-side. Use at your own risk.

## Development

```bash
pip install pytest pytest-asyncio
pytest          # tests run against fake Lazada pages, no network needed
```

Page selectors live in `lazada_bot/lazada.py`; if Lazada changes its layout, that's
the file to update.
