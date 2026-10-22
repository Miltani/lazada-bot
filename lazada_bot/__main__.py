"""CLI entry point: python -m lazada_bot {login,check,run}."""
from __future__ import annotations

import argparse
import asyncio
import logging

from . import bot
from .config import load


def main() -> None:
    ap = argparse.ArgumentParser(prog="lazada_bot", description="Lazada daily drop purchase bot")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="open a browser to log in (session is saved)")
    sub.add_parser("check", help="print current stock status of each product")
    run = sub.add_parser("run", help="monitor during the daily window and buy on restock")
    run.add_argument("--dry-run", action="store_true", help="stop at the checkout page, never place an order")
    run.add_argument("--once", action="store_true", help="exit after the next window instead of running daily")
    run.add_argument("--now", type=int, metavar="MINUTES",
                     help="ignore the schedule and monitor immediately for MINUTES (for testing)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = load(args.config)

    try:
        if args.cmd == "login":
            asyncio.run(bot.login(cfg))
        elif args.cmd == "check":
            asyncio.run(bot.check(cfg))
        else:
            asyncio.run(bot.Bot(cfg, dry_run=args.dry_run).run(once=args.once, now_minutes=args.now))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
