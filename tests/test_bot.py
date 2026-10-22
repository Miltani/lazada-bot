"""Tests run against fake Lazada pages served through Playwright routing (no network)."""
import datetime as dt
import json
import os
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.async_api import async_playwright

from lazada_bot import lazada
from lazada_bot.bot import Bot, is_blocked
from lazada_bot.config import Config, Product, load
from lazada_bot.lazada import CheckoutResult
from lazada_bot.schedule import next_window

SGT = ZoneInfo("Asia/Singapore")
PRODUCT_URL = "https://www.lazada.sg/products/test-i1-s2.html"

# Stand-in for the page's own Mtop client: the real one signs and sends
# mtop.global.detail.web.getDetailInfo; this one fetches the fake API below.
MTOP_STUB = """<script>
window.__moduleData__ = {data: {root: {fields: {primaryKey: {itemId: "1", skuId: "2"}}}}};
window.Mtop = {default: {request: (req, ok, fail) =>
  fetch('https://www.lazada.sg/fake-mtop', {method: 'POST'}).then(r => r.text())
    .then(t => ok({ret: ['SUCCESS::ok'], data: {module: t}})).catch(fail)}};
</script>"""

# Trimmed from the real lazada.sg page while the item was out of stock.
OUT_OF_STOCK = MTOP_STUB + """<html><body><h1>Pokemon Booster Bundle</h1>
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


def api_module(in_stock: bool) -> str:
    sku = {"itemId": "1", "skuId": "2", "operation": {"disable": not in_stock, "text": "Add to Cart"},
           "quantity": {"limit": {"max": 2 if in_stock else 0, "min": 1}}}
    return json.dumps({"data": {"root": {"fields": {"skuInfos": {"2": sku}}}}})


class FakeLazada:
    def __init__(self, product_html, api_in_stock=lambda n: False, checkout_ok=True):
        self.product_html = product_html
        self.api_in_stock = api_in_stock
        self.checkout_ok = checkout_ok
        self.product_hits = 0
        self.api_hits = 0
        self.checkout_qty = None
        self.direct_checkout = False

    async def handle(self, route):
        url = route.request.url
        if "/fake-mtop" in url:
            self.api_hits += 1
            return await route.fulfill(status=200, content_type="application/json",
                                       body=api_module(self.api_in_stock(self.api_hits)))
        if "/products/" in url:
            self.product_hits += 1
            body = self.product_html(self.product_hits) if callable(self.product_html) else self.product_html
        elif "/shipping" in url:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if "buyParams" in q:
                self.direct_checkout = True
                self.checkout_qty = json.loads(q["buyParams"][0])["items"][0]["quantity"]
            else:
                self.checkout_qty = int(q["qty"][0])
            body = CHECKOUT if self.checkout_ok else "<html><body>Item is out of stock</body></html>"
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


def _cfg(tmp_path, fast=False, **kw):
    return Config(products=[Product(url=PRODUCT_URL, quantity=2, **kw)], poll_seconds=0.1, poll_jitter=0,
                  fast_poll=fast, fast_poll_seconds=0.1, auto_place_order=True, auto_pay=True,
                  state_file=tmp_path / "state.json")


@pytest.mark.asyncio
async def test_status_out_of_stock_vs_in_stock():
    fake = FakeLazada(lambda n: OUT_OF_STOCK if n == 1 else MTOP_STUB + IN_STOCK.format(price="41.40"))
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
    fake = FakeLazada(lambda n: OUT_OF_STOCK if n <= 3 else MTOP_STUB + IN_STOCK.format(price="41.40"))
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
    fake = FakeLazada(MTOP_STUB + IN_STOCK.format(price="41.40"))
    pw, browser, page = await _page(fake)
    try:
        bot = Bot(_cfg(tmp_path), dry_run=True)
        result = await bot.monitor(page, bot.cfg.products[0], bot.now() + dt.timedelta(seconds=20))
        assert result == CheckoutResult.AT_CHECKOUT and "/shipping" in page.url
    finally:
        await browser.close(); await pw.stop()


@pytest.mark.asyncio
async def test_price_cap_skips_purchase(tmp_path):
    fake = FakeLazada(MTOP_STUB + IN_STOCK.format(price="99.00"))
    pw, browser, page = await _page(fake)
    try:
        bot = Bot(_cfg(tmp_path, max_price=45))
        result = await bot.monitor(page, bot.cfg.products[0], bot.now() + dt.timedelta(seconds=1))
        assert result is None and fake.checkout_qty is None
    finally:
        await browser.close(); await pw.stop()


@pytest.mark.asyncio
async def test_fast_poll_buys_via_direct_checkout(tmp_path):
    # Page always renders sold out; the stock API flips on its 5th call.
    fake = FakeLazada(OUT_OF_STOCK, api_in_stock=lambda n: n >= 5)
    pw, browser, page = await _page(fake)
    try:
        bot = Bot(_cfg(tmp_path, fast=True))
        result = await bot.monitor(page, bot.cfg.products[0], bot.now() + dt.timedelta(seconds=30))
        assert result == CheckoutResult.PLACED
        assert fake.product_hits == 1, "fast mode should not reload the product page"
        assert fake.direct_checkout and fake.checkout_qty == 2
    finally:
        await browser.close(); await pw.stop()


@pytest.mark.asyncio
async def test_fast_poll_falls_back_when_direct_checkout_rejected(tmp_path):
    # API flips, direct checkout is refused, product page then shows Buy Now.
    fake = FakeLazada(lambda n: OUT_OF_STOCK if n == 1 else MTOP_STUB + IN_STOCK.format(price="41.40"),
                      api_in_stock=lambda n: n >= 3)
    fake_direct = {"first": True}
    orig = fake.handle

    async def handle(route):
        if "buyParams" in route.request.url and fake_direct.pop("first", False):
            fake.direct_checkout = True
            return await route.fulfill(status=200, content_type="text/html", body="<html>Sold out</html>")
        return await orig(route)
    fake.handle = handle
    pw, browser, page = await _page(fake)
    try:
        bot = Bot(_cfg(tmp_path, fast=True))
        result = await bot.monitor(page, bot.cfg.products[0], bot.now() + dt.timedelta(seconds=40))
        assert result == CheckoutResult.PLACED and fake.direct_checkout
        assert fake.checkout_qty == 2  # bought through the Buy Now button after fallback
    finally:
        await browser.close(); await pw.stop()


@pytest.mark.asyncio
async def test_poll_api_reads_snapshot():
    fake = FakeLazada(OUT_OF_STOCK)
    pw, browser, page = await _page(fake)
    try:
        await lazada.load_product(page, PRODUCT_URL)
        snap = await lazada.poll_api(page)
        assert snap.usable and snap.sku_id == "2" and snap.item_id == "1" and not snap.blocked
    finally:
        await browser.close(); await pw.stop()


@pytest.mark.asyncio
async def test_warm_up_pings_checkout_and_detects_logout():
    seen = []

    async def handle(route):
        url = route.request.url
        seen.append(url)
        if url.startswith("https://cart.lazada.sg/cart"):
            # Playwright can't fake an HTTP redirect on navigation, so redirect from script.
            body = ("<script>location.replace('https://member.lazada.sg/user/login')</script>"
                    if logged_out else "<html>cart</html>")
            return await route.fulfill(status=200, content_type="text/html", body=body)
        if "member.lazada.sg" in url:
            return await route.fulfill(status=200, content_type="text/html", body="<html>login</html>")
        return await route.fulfill(status=204, body="")

    fake = FakeLazada(OUT_OF_STOCK)
    fake.handle = handle
    pw, browser, page = await _page(fake)
    try:
        logged_out = False
        assert await lazada.warm_up(page, "https://s.lazada.sg/s.abc", first=True)
        assert any(u.startswith("https://checkout.lazada.sg/") for u in seen)
        logged_out = True
        assert not await lazada.warm_up(page, PRODUCT_URL, first=True)
    finally:
        await browser.close(); await pw.stop()


def test_tracker_and_media_blocking():
    assert is_blocked("https://www.google-analytics.com/collect", "script")
    assert is_blocked("https://connect.facebook.net/en_US/fbevents.js", "script")
    assert is_blocked("https://img.lazcdn.com/x.jpg", "image")
    assert not is_blocked("https://g.lazcdn.com/g/lzdfe/pdp.js", "script")
    assert not is_blocked("https://checkout.lazada.sg/shipping", "document")
    assert not is_blocked("https://notfacebook.com/x.js", "script")


def test_direct_checkout_url():
    url = lazada.direct_checkout_url("https://www.lazada.sg/products/x-i1-s2.html", "1", "2", 2)
    assert url.startswith("https://checkout.lazada.sg/shipping?buyParams=")
    params = json.loads(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["buyParams"][0])
    assert params["items"][0] == {"itemId": "1", "skuId": "2", "quantity": 2, "attributes": None}


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
