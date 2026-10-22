"""Load and validate the YAML config."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


@dataclass
class Product:
    url: str
    quantity: int = 1
    max_price: float | None = None  # skip if the live price is above this
    name: str = ""

    def label(self) -> str:
        return self.name or self.url.split("?")[0][-60:]


@dataclass
class Config:
    products: list[Product]
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Asia/Singapore"))
    window_start: dt.time = dt.time(13, 0)
    window_end: dt.time = dt.time(14, 0)
    prewarm_minutes: int = 2          # open tabs / start polling this early
    poll_seconds: float = 3.0         # reload interval per product tab
    poll_jitter: float = 1.0          # +/- random seconds added to each poll
    auto_place_order: bool = True     # click "Place Order" on the checkout page
    auto_pay: bool = False            # also click the final "Pay Now" button
    headless: bool = False
    browser_channel: str | None = None  # e.g. "chrome" to use installed Google Chrome
    profile_dir: Path = Path("browser-profile")
    state_file: Path = Path("state.json")
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


def _time(value) -> dt.time:
    if isinstance(value, int):  # YAML may parse 13:00 as sexagesimal int (780)
        return dt.time(value // 60, value % 60)
    return dt.time.fromisoformat(str(value))


def _str_or_none(value) -> str | None:
    return str(value) if value not in (None, "") else None


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    products = []
    for p in raw.get("products") or []:
        if isinstance(p, str):
            p = {"url": p}
        products.append(Product(
            url=p["url"],
            quantity=int(p.get("quantity", 1)),
            max_price=float(p["max_price"]) if p.get("max_price") is not None else None,
            name=p.get("name", ""),
        ))
    if not products:
        raise ValueError("config: 'products' must list at least one product URL")

    window = raw.get("window", {})
    tg = raw.get("telegram", {}) or {}
    cfg = Config(
        products=products,
        timezone=ZoneInfo(raw.get("timezone", "Asia/Singapore")),
        window_start=_time(window.get("start", "13:00")),
        window_end=_time(window.get("end", "14:00")),
        prewarm_minutes=int(raw.get("prewarm_minutes", 2)),
        poll_seconds=float(raw.get("poll_seconds", 3.0)),
        poll_jitter=float(raw.get("poll_jitter", 1.0)),
        auto_place_order=bool(raw.get("auto_place_order", True)),
        auto_pay=bool(raw.get("auto_pay", False)),
        headless=bool(raw.get("headless", False)),
        browser_channel=raw.get("browser_channel") or None,
        profile_dir=Path(raw.get("profile_dir", "browser-profile")),
        state_file=Path(raw.get("state_file", "state.json")),
        telegram_bot_token=_str_or_none(tg.get("bot_token")),
        telegram_chat_id=_str_or_none(tg.get("chat_id")),
    )
    if cfg.window_end <= cfg.window_start:
        raise ValueError("config: window.end must be after window.start")
    return cfg
