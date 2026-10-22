# Lazada drop bot

Watches one or more Lazada product listings every day between **1pm and 2pm (SGT)**
and buys as soon as a listing comes back in stock. You don't need to know the exact
drop time: each product gets its own browser tab that checks Lazada's live stock
about once a second during the window.

## How it works

1. You log in once in a real browser window. The session is saved in `browser-profile/`.
2. Each day at 12:58 it opens a tab per product and loads the product page once.
3. **Fast mode** (default): instead of reloading, it re-runs the page's own live-stock
   API call (`mtop.global.detail.web.getDetailInfo`) about every second. The page HTML is
   a cached placeholder; this API is where Lazada's real stock comes from, so this is the
   earliest a browser can see a restock. The page is fully reloaded every 5 min.
4. The moment the stock data changes it jumps **straight to the checkout URL**
   (what Buy Now would open), skipping the product page re-render, and clicks
   **Place Order** (and optionally **Pay Now**). If the direct link is refused it falls
   back to reloading the product page and clicking **Buy Now**.
   If fast mode isn't available on a page, it reloads every ~3s and clicks Buy Now instead.
   Meanwhile a background tab keeps connections to Lazada's cart/checkout servers open
   (pinged every 45s), so checkout doesn't wait on connection setup, and warns you
   if your Lazada login has expired. All tabs skip images, fonts, video and third-party
   ad/analytics trackers; Lazada's own scripts are left alone.
5. It alerts you (terminal bell + optional Telegram) and leaves the browser open
   15 min so you can approve card OTP / e-wallet payment.
6. Each product is bought at most once per day (tracked in `state.json`, so restarts don't double-buy).
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
| `fast_poll` / `fast_poll_seconds` | in-page stock API polling (default on, every 1s) |
| `direct_checkout` | on restock, open the checkout URL directly instead of clicking Buy Now |
| `page_refresh_minutes` | full reload interval in fast mode |
| `poll_seconds` / `poll_jitter` | reload speed when fast mode is off/unavailable |
| `auto_place_order` | click "Place Order" on checkout (default on) |
| `auto_pay` | also click the final "Pay Now" (default off; card OTP still needs you) |
| `max_price` | per product: don't buy if price is above this |
| `telegram.bot_token` / `chat_id` | phone alerts — create a bot with @BotFather |

## Tips for winning drops

Most of the race is decided after stock appears, at checkout and payment:

- **Set `auto_pay: true`** and pay with **Lazada Wallet (topped up)** or another method with
  no OTP. A card that needs 3-D Secure OTP adds 10–30s of you typing, which is where bots win.
- Set your **default address** and make sure no voucher/insurance popup blocks checkout.
- Leave `max_price` unset for the fastest path: with a price cap the bot must reload the
  product page to check the price before buying.
- Run `check` before 1pm: it should print `fast mode: OK`. If not, tell me the output.
- Run on a wired connection, close other heavy apps, keep the product list short.
- `fast_poll_seconds: 1` is a sensible default. Going below ~0.5s mostly buys you
  Lazada's slider check, which stops you completely until you solve it.
- Lazada's terms may restrict automated purchasing, and listings like
  "Limit 2 per person" are enforced server-side. Use at your own risk.

## Development

```bash
pip install pytest pytest-asyncio
pytest          # tests run against fake Lazada pages, no network needed
```

Page selectors live in `lazada_bot/lazada.py`; if Lazada changes its layout, that's
the file to update.
