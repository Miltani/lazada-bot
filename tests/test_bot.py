"""Tests run against fake Lazada pages served through Playwright routing (no network)."""
import asyncio
import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.async_api import async_playwright

from lazada_bot import lazada
from lazada_bot.bot import Bot
from lazada_bot.config import Config, Product, load
from lazada_bot.lazada import CheckoutResult
from lazada_bot.schedule import next_window

SGT = ZoneInfo("Asia/Singapore")
PRODUCT_URL = "https://www.lazada.sg/products/test-i1-s2.html"

# Trimmed from the real lazada.sg page while the item was out of stock.
OUT_OF_STOCK = """<html><body><h1>Pokemon Booster Bundle</h1>
<div id="module_product_price_v2"><span class="pdp-v2-product-price-content-salePrice-amount">41.40</span></div>
<div id="module_quantity-input"><a class="next-number-picker-handler next-number-picker-handler-up"></a>
<input min="0" max="0" disabled value="0"><span class="quantity-content-warning ">Out of stock</span></div>
<div id="module_add_to_cart"><button type="button"><span class="btn-txt">Add to Wishlist</span></button></div>
</body></html>"""

IN_STOCK = """<html><body><h1>Pokemon Booster Bundle</h1>
<div id="module_product_price_v2"><span class="pdp-v2-product-price-content-salePrice-amount">{price}</span></div>
<div id="module_quantity-input"><a class="next-number-picker-handler next-number-picker-handler-up"
  onclick="var i=this.parentNode.querySelector('input'); i.value=Math.min(+i.value+1, +i.max)">+</a>
<input min="1" max="2" value="1"><span class="quantity-content-warning "></span></div>
<div id="module_add_to_cart">
  <button type="button" onclick="location.href='https://checkout.lazada.sg/shipping?qty='+document.querySelector('input').value">
    <span class="btn-txt">Buy Now</span></button>
  <button type="button"><span class="btn-txt">Add to Cart</span></button>
</div></body></html>"""

CHECKOUT = """<html><body><button onclick="location.href='https://checkout.lazada.sg/payment'">Place Order</button></body></html>"""
PAYMENT = """<html><body><button onclick="location.href='https://checkout.lazada.sg/success'">Pay Now</button></body></html>"""
SUCCESS = "<html><body>Thank you</body></html>"
CAPTCHA = '<html><body><div id="nc_1_wrapper">slide to verify</div></body></html>'


class FakeLazada:
    def __init__(self, product_html):
        self.product_html = product_html
        self.product_hits = 0
        self.checkout_qty = None

    async def handle(self, route):
        url = route.request.url
        if "/products/" in url:
            self.product_hits += 1
            body = self.product_html(self.product_hits) if callable(self.product_html) else self.product_html
        elif "/shipping" in url:
            self.checkout_qty = int(url.split("qty=")[1])
            body = CHECKOUT
        elif "/payment" in url:
            body = PAYMENT
        elif "/success" in url:
            body = SUCCESS
        else:
            return await route.abort()
        await route.fulfill(status=200, content_type="text/html", body=body)


async def _page(fake):
    pw = await async_playwright().start()
    exe = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium")
    browser = await pw.chromium.launch(executable_path=exe if os.path.exists(exe) else None)
    ctx = await browser.new_context()
    await ctx.route("**/*", fake.handle)
    return pw, browser, await ctx.new_page()


def _cfg(tmp_path, **kw):
    return Config(products=[Product(url=PRODUCT_URL, quantity=2, **kw)], poll_seconds=0.1, poll_jitter=0,
                  auto_place_order=True, auto_pay=True, state_file=tmp_path / "state.json")


@pytest.mark.asyncio
async def test_status_out_of_stock_vs_in_stock():
    fake = FakeLazada(lambda n: OUT_OF_STOCK if n == 1 else IN_STOCK.format(price="41.40"))
    pw, browser, page = await _page(fake)
    try:
        st = await lazada.load_product(page, PRODUCT_URL)
        assert st.loaded and st.sold_out and not st.in_stock and st.price == 41.40
        st = await lazada.load_product(page, PRODUCT_URL)
        assert st.in_stock and st.max_qty == 2
    finally:
        await browser.close(); await pw.stop()


@pytest.mark.asyncio
async def test_captcha_detected():
    fake = FakeLazada(CAPTCHA)
    pw, browser, page = await _page(fake)
    try:
        st = await lazada.load_product(page, PRODUCT_URL, timeout_ms=500)
        assert st.blocked and not st.in_stock
    finally:
        await browser.close(); await pw.stop()


@pytest.mark.asyncio
async def test_monitor_buys_after_restock(tmp_path):
    # Sold out for the first 3 reloads, then drops.
    fake = FakeLazada(lambda n: OUT_OF_STOCK if n <= 3 else IN_STOCK.format(price="41.40"))
    pw, browser, page = await _page(fake)
    try:
        bot = Bot(_cfg(tmp_path))
        until = bot.now() + dt.timedelta(seconds=30)
        result = await bot.monitor(page, bot.cfg.products[0], until)
        assert result == CheckoutResult.PLACED
        assert fake.product_hits == 4 and fake.checkout_qty == 2
        assert bot.state.done(bot.now().date(), PRODUCT_URL)
    finally:
        await browser.close(); await pw.stop()


@pytest.mark.asyncio
async def test_dry_run_stops_at_checkout(tmp_path):
    fake = FakeLazada(IN_STOCK.format(price="41.40"))
    pw, browser, page = await _page(fake)
    try:
        bot = Bot(_cfg(tmp_path), dry_run=True)
        result = await bot.monitor(page, bot.cfg.products[0], bot.now() + dt.timedelta(seconds=20))
        assert result == CheckoutResult.AT_CHECKOUT and "/shipping" in page.url
    finally:
        await browser.close(); await pw.stop()


@pytest.mark.asyncio
async def test_price_cap_skips_purchase(tmp_path):
    fake = FakeLazada(IN_STOCK.format(price="99.00"))
    pw, browser, page = await _page(fake)
    try:
        bot = Bot(_cfg(tmp_path, max_price=45))
        result = await bot.monitor(page, bot.cfg.products[0], bot.now() + dt.timedelta(seconds=1))
        assert result is None and fake.checkout_qty is None
    finally:
        await browser.close(); await pw.stop()


def test_next_window():
    start, end = dt.time(13), dt.time(14)
    before = dt.datetime(2026, 10, 22, 9, 0, tzinfo=SGT)
    assert next_window(before, start, end, 2) == (
        dt.datetime(2026, 10, 22, 12, 58, tzinfo=SGT), dt.datetime(2026, 10, 22, 14, 0, tzinfo=SGT))
    during = dt.datetime(2026, 10, 22, 13, 30, tzinfo=SGT)
    assert next_window(during, start, end)[1] == dt.datetime(2026, 10, 22, 14, 0, tzinfo=SGT)
    after = dt.datetime(2026, 10, 22, 14, 0, tzinfo=SGT)
    assert next_window(after, start, end)[0] == dt.datetime(2026, 10, 23, 13, 0, tzinfo=SGT)


def test_example_config_loads():
    cfg = load(Path(__file__).parent.parent / "config.example.yaml")
    assert cfg.products[0].quantity == 2 and cfg.window_start == dt.time(13)
