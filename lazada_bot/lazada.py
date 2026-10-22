"""Lazada page logic: read stock status from a product page and run checkout.

Selectors are based on the lazada.sg product page as of Oct 2026. If Lazada
changes its markup, this is the only file that should need updating.
"""
from __future__ import annotations

import asyncio
import logging
import re
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
    await asyncio.sleep(0.5)
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
