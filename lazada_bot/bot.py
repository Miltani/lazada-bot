"""Orchestration: open one tab per product during the daily window and buy on restock."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from . import lazada
from .config import Config, Product
from .lazada import CheckoutResult
from .notify import Notifier
from .schedule import next_window

log = logging.getLogger("lazada_bot")

# Results that mean "we got it (or the user is finishing it) - stop for today".
DONE_RESULTS = {CheckoutResult.PLACED, CheckoutResult.AWAITING_PAYMENT, CheckoutResult.AT_CHECKOUT}
LINGER_AFTER_WINDOW = dt.timedelta(minutes=15)  # keep tabs open so the user can finish payment


class State:
    """Per-day record of which products were bought, so restarts don't double-buy."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def done(self, day: dt.date, url: str) -> bool:
        return self.data.get(day.isoformat(), {}).get(url) in DONE_RESULTS

    def record(self, day: dt.date, url: str, result: str) -> None:
        self.data.setdefault(day.isoformat(), {})[url] = result
        self.path.write_text(json.dumps(self.data, indent=2))


async def open_context(pw, cfg: Config, block_media: bool = True) -> BrowserContext:
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    ctx = await pw.chromium.launch_persistent_context(
        str(cfg.profile_dir),
        headless=cfg.headless,
        channel=cfg.browser_channel,
        locale="en-SG",
        timezone_id=str(cfg.timezone),
        viewport={"width": 1280, "height": 900},
    )
    if block_media:
        # Product polling only needs the DOM; skipping images makes each reload much faster.
        async def _route(route):
            if route.request.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()
        await ctx.route("**/*", _route)
    return ctx


class Bot:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.notifier = Notifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
        self.state = State(cfg.state_file)

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.cfg.timezone)

    async def _sleep_poll(self) -> None:
        delay = self.cfg.poll_seconds + random.uniform(-self.cfg.poll_jitter, self.cfg.poll_jitter)
        await asyncio.sleep(max(0.5, delay))

    async def _wait_until_clear(self, page: Page, product: Product, until: dt.datetime, what: str) -> None:
        """Captcha or login wall: alert the user and wait for them to resolve it in the browser."""
        self.notifier.send(f"{what} on {product.label()} - please resolve it in the browser window.", urgent=True)
        while self.now() < until:
            await asyncio.sleep(2)
            try:
                st = await lazada.read_status(page)
            except Exception:
                continue  # page navigating
            if not st.blocked and not st.login:
                self.notifier.send(f"{what} cleared for {product.label()}, resuming.")
                return

    async def monitor(self, page: Page, product: Product, until: dt.datetime) -> str | None:
        day = self.now().date()
        announced_oos = False
        target = product.url
        while self.now() < until:
            try:
                st = await lazada.load_product(page, target)
            except Exception as exc:
                log.debug("load failed for %s: %s", product.label(), exc)
                await self._sleep_poll()
                continue

            if st.blocked:
                await self._wait_until_clear(page, product, until, "Verification/captcha")
                continue
            if st.login:
                await self._wait_until_clear(page, product, until, "Login required")
                continue
            if not product.name and st.title:
                product.name = st.title[:60]
            if st.loaded and "/products/" in page.url:
                target = page.url.split("?")[0]  # skip the share-link redirect on later reloads

            if not st.in_stock:
                if not announced_oos:
                    log.info("Watching %s (%s)", product.label(), st.warning or "not buyable yet")
                    announced_oos = True
                await self._sleep_poll()
                continue

            if product.max_price is not None and st.price is not None and st.price > product.max_price:
                log.info("%s in stock but $%.2f > max $%.2f, skipping", product.label(), st.price, product.max_price)
                await self._sleep_poll()
                continue

            self.notifier.send(f"IN STOCK: {product.label()} @ ${st.price} - buying {product.quantity}", urgent=True)
            try:
                result = await lazada.checkout(
                    page, product.quantity, st.max_qty,
                    place_order=self.cfg.auto_place_order and not self.dry_run,
                    pay=self.cfg.auto_pay and not self.dry_run,
                )
            except Exception as exc:
                log.exception("checkout crashed")
                result = CheckoutResult.FAILED
                self.notifier.send(f"Checkout error for {product.label()}: {exc}", urgent=True)

            if result in DONE_RESULTS:
                self.state.record(day, product.url, result)
                msg = {
                    CheckoutResult.PLACED: "ORDER PLACED",
                    CheckoutResult.AWAITING_PAYMENT: "Order created - FINISH PAYMENT in the browser now",
                    CheckoutResult.AT_CHECKOUT: "At checkout page (dry run / auto_place_order off) - finish manually",
                }[result]
                self.notifier.send(f"{msg}: {product.label()}", urgent=True)
                return result
            if result == CheckoutResult.BLOCKED:
                await self._wait_until_clear(page, product, until, "Verification/captcha at checkout")
            elif result == CheckoutResult.LOGIN_REQUIRED:
                await self._wait_until_clear(page, product, until, "Login required at checkout")
            else:
                self.notifier.send(f"Checkout failed for {product.label()} (likely sold out again) - retrying", urgent=True)
        return None

    async def run_window(self, start: dt.datetime, until: dt.datetime) -> None:
        wait = (start - self.now()).total_seconds()
        if wait > 0:
            log.info("Next window opens %s (in %.0f min). Sleeping.", start.strftime("%a %d %b %H:%M %Z"), wait / 60)
            await asyncio.sleep(wait)

        day = self.now().date()
        todo = [p for p in self.cfg.products if not self.state.done(day, p.url)]
        if not todo:
            log.info("All products already bought today.")
            await asyncio.sleep(max(0, (until - self.now()).total_seconds()))
            return

        log.info("Window open until %s - monitoring %d product(s)", until.strftime("%H:%M"), len(todo))
        async with async_playwright() as pw:
            ctx = await open_context(pw, self.cfg)
            try:
                pages = [await ctx.new_page() for _ in todo]
                results = await asyncio.gather(*(self.monitor(pg, p, until) for pg, p in zip(pages, todo)))
                if any(r in (CheckoutResult.AWAITING_PAYMENT, CheckoutResult.AT_CHECKOUT) for r in results):
                    log.info("Keeping the browser open %d min so you can finish payment.",
                             LINGER_AFTER_WINDOW.seconds // 60)
                    await asyncio.sleep(LINGER_AFTER_WINDOW.total_seconds())
            finally:
                await ctx.close()
        log.info("Window closed.")

    async def run(self, once: bool = False, now_minutes: int | None = None) -> None:
        if now_minutes is not None:  # test mode: monitor immediately for N minutes
            start = self.now()
            await self.run_window(start, start + dt.timedelta(minutes=now_minutes))
            return
        while True:
            start, until = next_window(self.now(), self.cfg.window_start, self.cfg.window_end,
                                       self.cfg.prewarm_minutes)
            await self.run_window(start, until)
            if once:
                return


async def check(cfg: Config) -> None:
    """Print current status of each product once."""
    async with async_playwright() as pw:
        ctx = await open_context(pw, cfg)
        try:
            page = await ctx.new_page()
            for p in cfg.products:
                st = await lazada.load_product(page, p.url)
                state = ("BLOCKED (captcha)" if st.blocked else
                         "IN STOCK" if st.in_stock else st.warning or "not buyable")
                print(f"- {st.title or p.url}\n    price=${st.price}  status={state}  max_qty={st.max_qty}")
        finally:
            await ctx.close()


async def login(cfg: Config) -> None:
    """Open a visible browser so the user can log in; the session is saved in profile_dir."""
    async with async_playwright() as pw:
        cfg.headless = False
        ctx = await open_context(pw, cfg, block_media=False)
        page = await ctx.new_page()
        await page.goto("https://member.lazada.sg/user/login")
        print("Log in to Lazada in the browser window (and check your saved address/payment).")
        await asyncio.get_running_loop().run_in_executor(None, input, "Press Enter here when done... ")
        await ctx.close()
