"""Lazada page logic: read stock status from a product page and run checkout.

Selectors are based on the lazada.sg product page as of Oct 2026. If Lazada
changes its markup, this is the only file that should need updating.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass

from playwright.async_api import Page, TimeoutError as PWTimeout

log = logging.getLogger("lazada_bot")

# Runs in the page; returns a plain dict so it is cheap to call every poll.
_STATUS_JS = r"""
() => {
  const txt = el => (el ? el.innerText || el.textContent || '' : '').trim();
  const url = location.href;
  const blocked = /punish|_____tmd_____|captcha|baxia/i.test(url)
    || !!document.querySelector('#baxia-dialog-content, .baxia-dialog, iframe[src*="punish"], #nc_1_wrapper, .nc-container');
  const login = /member\.lazada\.[a-z.]+\/user\/login/i.test(url);

  const cart = document.querySelector('#module_add_to_cart');
  const buttons = cart ? [...cart.querySelectorAll('button')] : [];
  const buyNow = buttons.find(b => /buy\s*now/i.test(txt(b)));
  const buyEnabled = !!buyNow && !buyNow.disabled && !/disabled/.test(buyNow.className);

  const qtyBox = document.querySelector('#module_quantity-input');
  const warn = txt(qtyBox && qtyBox.querySelector('.quantity-content-warning'));
  const qtyInput = qtyBox && qtyBox.querySelector('input');
  const maxQty = qtyInput ? parseInt(qtyInput.getAttribute('max') || '0', 10) : null;
  const soldOut = /out of stock|sold out/i.test(warn) || maxQty === 0;

  const priceEl = document.querySelector(
    '#module_product_price_v2 .pdp-v2-product-price-content-salePrice-amount, ' +
    '#module_product_price_1 .pdp-price, .pdp-price_type_normal');
  const price = priceEl ? parseFloat(txt(priceEl).replace(/[^0-9.]/g, '')) : null;

  return {
    loaded: !!cart, blocked, login, buyEnabled, soldOut, maxQty,
    price: Number.isFinite(price) ? price : null,
    warning: warn, title: txt(document.querySelector('h1')),
  };
}
"""


@dataclass
class Status:
    loaded: bool
    blocked: bool
    login: bool
    buy_enabled: bool
    sold_out: bool
    max_qty: int | None
    price: float | None
    warning: str
    title: str

    @property
    def in_stock(self) -> bool:
        return self.loaded and self.buy_enabled and not self.sold_out


async def read_status(page: Page) -> Status:
    d = await page.evaluate(_STATUS_JS)
    return Status(
        loaded=d["loaded"], blocked=d["blocked"], login=d["login"],
        buy_enabled=d["buyEnabled"], sold_out=d["soldOut"], max_qty=d["maxQty"],
        price=d["price"], warning=d["warning"], title=d["title"],
    )


async def load_product(page: Page, url: str, timeout_ms: int = 20000) -> Status:
    """(Re)load a product page and return its status once the cart block renders."""
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_selector("#module_add_to_cart button", timeout=timeout_ms)
    except PWTimeout:
        pass  # captcha/login pages never render it; read_status reports why
    return await read_status(page)


async def set_quantity(page: Page, quantity: int, max_qty: int | None) -> int:
    """Click the '+' button until the requested quantity (capped by max) is set."""
    target = max(1, min(quantity, max_qty or quantity))
    plus = page.locator("#module_quantity-input .next-number-picker-handler-up")
    inp = page.locator("#module_quantity-input input")
    for _ in range(target - 1):
        try:
            await plus.first.click(timeout=1500)
        except PWTimeout:
            break  # never stall checkout over quantity; buy whatever is set
    try:
        return int(await inp.first.input_value(timeout=1000))
    except Exception:
        return target


_BUY_NOW = re.compile(r"buy\s*now", re.I)
_PLACE_ORDER = re.compile(r"place\s*order|proceed\s*to\s*pay", re.I)
_PAY_NOW = re.compile(r"pay\s*now|place\s*order\s*now|confirm\s*order", re.I)


class CheckoutResult:
    PLACED = "placed"            # order placed (and paid, if auto_pay)
    AWAITING_PAYMENT = "awaiting_payment"  # on the payment page; user must finish
    AT_CHECKOUT = "at_checkout"  # stopped on checkout page (auto_place_order off / dry run)
    LOGIN_REQUIRED = "login_required"
    BLOCKED = "blocked"          # captcha / slider verification shown
    FAILED = "failed"


async def _click_button(page: Page, pattern: re.Pattern, timeout_ms: int) -> bool:
    btn = page.get_by_role("button", name=pattern)
    try:
        await btn.first.wait_for(state="visible", timeout=timeout_ms)
        await btn.first.click()
        return True
    except PWTimeout:
        # Lazada sometimes renders these as divs/spans rather than <button>.
        alt = page.get_by_text(pattern).first
        try:
            await alt.wait_for(state="visible", timeout=1000)
            await alt.click()
            return True
        except PWTimeout:
            return False


async def checkout(page: Page, quantity: int, max_qty: int | None, *,
                   place_order: bool, pay: bool) -> str:
    """From an in-stock product page, run Buy Now -> checkout -> (pay)."""
    await set_quantity(page, quantity, max_qty)

    if not await _click_button(page, _BUY_NOW, 3000):
        return CheckoutResult.FAILED

    # Buy Now navigates to checkout.lazada.*; a login popup/page means the session expired.
    try:
        await page.wait_for_url(re.compile(r"checkout\.lazada|member\.lazada|punish"), timeout=15000)
    except PWTimeout:
        st = await read_status(page)
        if st.blocked:
            return CheckoutResult.BLOCKED
        if await page.locator("iframe[src*='login'], .login-popup, #login-dialog").count():
            return CheckoutResult.LOGIN_REQUIRED
        return CheckoutResult.FAILED
    return await finish_checkout(page, place_order=place_order, pay=pay)


def _tld(product_url: str) -> str:
    host = urllib.parse.urlparse(product_url).hostname or "www.lazada.sg"
    return host.split("lazada.", 1)[-1]


def direct_checkout_url(product_url: str, item_id: str, sku_id: str, quantity: int) -> str:
    """The URL Buy Now leads to, so we can jump straight there without re-rendering the product page."""
    tld = _tld(product_url)
    buy = json.dumps({"items": [{"itemId": item_id, "skuId": sku_id, "quantity": quantity, "attributes": None}]},
                     separators=(",", ":"))
    return f"https://checkout.lazada.{tld}/shipping?buyParams={urllib.parse.quote(buy)}"


async def direct_checkout(page: Page, url: str, *, place_order: bool, pay: bool) -> str:
    """Skip the product page: open the checkout URL directly.

    Returns FAILED if checkout didn't offer a Place Order button (e.g. item still
    sold out, or Lazada rejected the direct link), so the caller can fall back.
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    if "member.lazada" in page.url:
        return CheckoutResult.LOGIN_REQUIRED
    if re.search(r"punish", page.url):
        return CheckoutResult.BLOCKED
    if "checkout.lazada" not in page.url:
        return CheckoutResult.FAILED
    btn = page.get_by_role("button", name=_PLACE_ORDER)
    try:
        await btn.first.wait_for(state="visible", timeout=8000)
    except PWTimeout:
        if not await page.get_by_text(_PLACE_ORDER).count():
            return CheckoutResult.FAILED
    return await finish_checkout(page, place_order=place_order, pay=pay)


async def finish_checkout(page: Page, *, place_order: bool, pay: bool) -> str:
    """On the checkout page: Place Order -> (Pay Now)."""
    if "member.lazada" in page.url:
        return CheckoutResult.LOGIN_REQUIRED
    if re.search(r"punish", page.url):
        return CheckoutResult.BLOCKED

    if not place_order:
        return CheckoutResult.AT_CHECKOUT

    if not await _click_button(page, _PLACE_ORDER, 15000):
        return CheckoutResult.FAILED

    # Next: the payment-method page (or straight to success for some methods).
    try:
        await page.wait_for_url(re.compile(r"payment|cashier|success|order", re.I), timeout=15000)
    except PWTimeout:
        pass
    if re.search(r"success|thank", page.url, re.I):
        return CheckoutResult.PLACED

    if not pay:
        return CheckoutResult.AWAITING_PAYMENT

    if not await _click_button(page, _PAY_NOW, 15000):
        return CheckoutResult.AWAITING_PAYMENT
    try:
        await page.wait_for_url(re.compile(r"success|thank|result", re.I), timeout=20000)
        return CheckoutResult.PLACED
    except PWTimeout:
        # Card OTP / 3DS or e-wallet confirmation is in progress: user must finish.
        return CheckoutResult.AWAITING_PAYMENT


async def warm_up(page: Page, product_url: str, first: bool) -> bool:
    """Keep connections to the checkout servers open so the first checkout request
    doesn't pay for DNS + TLS setup. Returns False if Lazada says we're logged out.

    On the first call it opens the cart page (on the same login session as
    checkout); after that it just pings the checkout host to keep sockets alive.
    """
    tld = _tld(product_url)
    if first:
        try:
            await page.goto(f"https://cart.lazada.{tld}/cart", wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
    if "member.lazada" in page.url:
        return False
    try:
        await page.evaluate(
            """(urls) => Promise.all(urls.map(u => fetch(u, {mode: 'no-cors', credentials: 'include',
                                                           cache: 'no-store'}).catch(() => null)))""",
            [f"https://checkout.lazada.{tld}/", f"https://cart.lazada.{tld}/"],
        )
    except Exception:
        # The page navigated mid-ping (typically a redirect to the login page).
        await page.wait_for_load_state("domcontentloaded")
    return "member.lazada" not in page.url


# --- Fast stock polling -------------------------------------------------------
# The product page's HTML is a cached placeholder; live stock arrives via the
# signed API call mtop.global.detail.web.getDetailInfo, made by the page's own
# Mtop client. Re-issuing that call from inside the loaded page costs one small
# request instead of a full page reload, so we can poll ~every second.

_API_POLL_JS = r"""
async (timeoutMs) => {
  const mtop = window.Mtop && window.Mtop.default && window.Mtop.default.request;
  const pk = (((window.__moduleData__ || {}).data || {}).root || {}).fields?.primaryKey || {};
  if (!mtop) return {usable: false, error: 'no Mtop client on page'};
  const uri = (location.pathname.match(/\/([\w-]+)\.html/) || [])[1] || '';
  const cookies = Object.fromEntries(document.cookie.split('; ').map(c => c.split('=')));
  const params = Object.fromEntries(new URLSearchParams(location.search));
  const ua = navigator.userAgent;
  const deviceType = /android/i.test(ua) ? 'android' : /iphone|ipad/i.test(ua) ? 'ios' : 'pc';
  const req = {
    api: 'mtop.global.detail.web.getDetailInfo', v: '1.0', type: 'POST', needLogin: false,
    data: {deviceType, path: location.href, uri,
           headerParams: JSON.stringify({'user-agent': ua}),
           cookieParams: JSON.stringify(cookies), requestParams: JSON.stringify(params)},
  };
  const res = await new Promise(resolve => {
    const t = setTimeout(() => resolve({timeout: true}), timeoutMs);
    try {
      mtop(req, r => { clearTimeout(t); resolve(r); }, e => { clearTimeout(t); resolve(e || {failed: true}); });
    } catch (e) { clearTimeout(t); resolve({error: String(e)}); }
  });
  const ret = (res && res.ret) ? res.ret.join(';') : '';
  if (/RGV587|USER_VALIDATE|punish|x5sec/i.test(ret + JSON.stringify(res && res.data || {}).slice(0, 2000)))
    return {usable: false, blocked: true, error: ret};
  if (!res || !res.data || !res.data.module) return {usable: false, error: ret || JSON.stringify(res).slice(0, 200)};
  let mod;
  try { mod = typeof res.data.module === 'string' ? JSON.parse(res.data.module) : res.data.module; }
  catch (e) { return {usable: false, error: 'bad module json'}; }
  const f = (((mod || {}).data || {}).root || {}).fields || (mod.fields || {});
  const sku = String(pk.skuId || (f.primaryKey || {}).skuId || '');
  const info = (f.skuInfos || {})[sku] || (f.skuInfos || {})['0'] || null;
  const skuBase = ((f.productOption || {}).skuBase || {}).skus || [];
  const baseEntry = skuBase.find(s => String(s.skuId) === sku || String(s.cartSkuId) === sku) || null;
  if (!info && !baseEntry) return {usable: false, error: 'sku not found in response'};
  const signal = JSON.stringify({info, baseEntry, stock: f.stock || null, quantity: f.quantity || null});
  return {
    usable: true, signal,
    oos: /out of stock|sold ?out|"stock":0[,}]|"soldOut":true/i.test(signal),
    itemId: String(pk.itemId || (info || {}).itemId || ''), skuId: sku,
  };
}
"""


@dataclass
class ApiSnapshot:
    usable: bool
    blocked: bool = False
    signal: str | None = None   # stock-relevant part of the response; changes when stock changes
    oos: bool = False           # response explicitly says sold out
    item_id: str = ""
    sku_id: str = ""
    error: str = ""


async def poll_api(page: Page, timeout_ms: int = 4000) -> ApiSnapshot:
    try:
        d = await page.evaluate(_API_POLL_JS, timeout_ms)
    except Exception as exc:
        return ApiSnapshot(usable=False, error=str(exc)[:200])
    return ApiSnapshot(
        usable=bool(d.get("usable")), blocked=bool(d.get("blocked")), signal=d.get("signal"),
        oos=bool(d.get("oos")), item_id=d.get("itemId", ""), sku_id=d.get("skuId", ""),
        error=d.get("error", ""),
    )
