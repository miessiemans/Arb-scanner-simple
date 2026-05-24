"""
arb_scanner.py — Kalshi / Polymarket cross-platform arbitrage scanner
Python 3.10+
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from rapidfuzz import fuzz
from rich.console import Console
from rich.table import Table
from tabulate import tabulate

# ─────────────────────────────────────────────
#  Top-level constants  (tweak here or via CLI)
# ─────────────────────────────────────────────
KALSHI_BASE_URL      = "https://trading-api.kalshi.com/trade-api/v2"
POLYMARKET_BASE_URL  = "https://clob.polymarket.com"

MATCH_THRESHOLD      = 0.82   # minimum fuzzy-match score (0–1)
TOLERANCE            = 0.02   # arb-gap threshold
PAGE_SIZE            = 200    # markets per API page

MAX_RETRIES          = 4      # HTTP retry attempts
BACKOFF_BASE         = 1.5    # exponential back-off base (seconds)
REQUEST_TIMEOUT      = 15     # seconds

console = Console()


# ─────────────────────────────────────────────
#  Data models
# ─────────────────────────────────────────────
@dataclass
class KalshiMarket:
    ticker: str
    title: str
    yes_price: float          # 0.0 – 1.0 implied probability


@dataclass
class PolyMarket:
    condition_id: str
    question: str
    yes_price: float          # 0.0 – 1.0 implied probability


@dataclass
class MatchedPair:
    kalshi: KalshiMarket
    poly: PolyMarket
    similarity: float         # fuzzy score 0–1


@dataclass
class ArbResult:
    event: str
    kalshi_yes: float
    poly_yes: float
    total: float
    arb_gap: float
    status: str               # ARB_OPPORTUNITY | OVER_ROUND | EFFICIENT
    kalshi_ticker: str
    poly_condition_id: str


# ─────────────────────────────────────────────
#  HTTP helper with retry / back-off
# ─────────────────────────────────────────────
def _get(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    """GET with exponential back-off.  Returns parsed JSON or raises."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = httpx.get(
                url,
                params=params,
                headers=headers or {},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:              # rate-limited
                wait = BACKOFF_BASE ** attempt
                console.log(f"[yellow]Rate-limited by {url}. Waiting {wait:.1f}s…[/yellow]")
                time.sleep(wait)
                continue
            console.log(f"[red]HTTP {resp.status_code} from {url}[/red]")
            resp.raise_for_status()
        except httpx.TimeoutException:
            wait = BACKOFF_BASE ** attempt
            console.log(f"[yellow]Timeout on attempt {attempt}. Retrying in {wait:.1f}s…[/yellow]")
            time.sleep(wait)
        except httpx.HTTPStatusError as exc:
            console.log(f"[red]HTTP error: {exc}[/red]")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(BACKOFF_BASE ** attempt)
    raise RuntimeError(f"All {MAX_RETRIES} attempts failed for {url}")


# ─────────────────────────────────────────────
#  Fetch markets
# ─────────────────────────────────────────────
def fetch_kalshi_markets() -> list[KalshiMarket]:
    """Page through Kalshi /markets and return open markets with valid prices."""
    api_key = os.environ.get("KALSHI_API_KEY", "")
    headers = {"Authorization": f"kalshi-api-key {api_key}"} if api_key else {}

    markets: list[KalshiMarket] = []
    cursor: str | None = None

    console.log("[cyan]Fetching Kalshi markets…[/cyan]")
    while True:
        params: dict[str, Any] = {"status": "open", "limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor

        data = _get(f"{KALSHI_BASE_URL}/markets", params=params, headers=headers)
        raw = data.get("markets", [])

        for m in raw:
            yes_ask = m.get("yes_ask")
            if yes_ask is None:
                continue
            price = yes_ask / 100.0
            # Discard zero-liquidity extremes
            if price in (0.0, 1.0):
                continue
            markets.append(
                KalshiMarket(
                    ticker=m["ticker"],
                    title=m.get("title", ""),
                    yes_price=price,
                )
            )

        cursor = data.get("cursor")
        if not cursor or len(raw) < PAGE_SIZE:
            break

    console.log(f"[cyan]Kalshi: {len(markets)} usable markets fetched.[/cyan]")
    return markets


def fetch_polymarket_markets() -> list[PolyMarket]:
    """Page through Polymarket /markets and return active markets with valid prices."""
    markets: list[PolyMarket] = []
    next_cursor: str | None = None

    console.log("[cyan]Fetching Polymarket markets…[/cyan]")
    while True:
        params: dict[str, Any] = {"active": "true"}
        if next_cursor:
            params["next_cursor"] = next_cursor

        data = _get(f"{POLYMARKET_BASE_URL}/markets", params=params)
        raw = data if isinstance(data, list) else data.get("data", [])

        for m in raw:
            tokens = m.get("tokens", [])
            yes_token = next((t for t in tokens if t.get("outcome", "").lower() == "yes"), None)
            if yes_token is None:
                continue
            price = float(yes_token.get("price", 0))
            if price in (0.0, 1.0):
                continue
            markets.append(
                PolyMarket(
                    condition_id=m.get("condition_id", ""),
                    question=m.get("question", ""),
                    yes_price=price,
                )
            )

        # Polymarket paginates via next_cursor string; "LTE=" signals last page
        next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not next_cursor or next_cursor == "LTE=" or isinstance(data, list):
            break

    console.log(f"[cyan]Polymarket: {len(markets)} usable markets fetched.[/cyan]")
    return markets


# ─────────────────────────────────────────────
#  String normalisation for fuzzy matching
# ─────────────────────────────────────────────
_PUNCT = re.compile(r"[^\w\s]")

def _normalise(text: str) -> str:
    text = text.lower()
    text = _PUNCT.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ─────────────────────────────────────────────
#  Market matching
# ─────────────────────────────────────────────
def match_markets(
    kalshi_markets: list[KalshiMarket],
    poly_markets: list[PolyMarket],
    threshold: float = MATCH_THRESHOLD,
) -> list[MatchedPair]:
    """
    For each Kalshi market find the single best-matching Polymarket market.
    Uses RapidFuzz token_sort_ratio for order-insensitive title comparison.
    """
    pairs: list[MatchedPair] = []

    # Pre-normalise Polymarket questions once
    poly_norm = [(_normalise(p.question), p) for p in poly_markets]

    for k in kalshi_markets:
        k_norm = _normalise(k.title)
        best_score = 0.0
        best_poly: PolyMarket | None = None

        for p_norm, p in poly_norm:
            score = fuzz.token_sort_ratio(k_norm, p_norm) / 100.0
            if score > best_score:
                best_score = score
                best_poly = p

        if best_poly is not None and best_score >= threshold:
            pairs.append(MatchedPair(kalshi=k, poly=best_poly, similarity=best_score))

    console.log(f"[cyan]{len(pairs)} matched pairs found (threshold={threshold}).[/cyan]")
    return pairs


# ─────────────────────────────────────────────
#  Arbitrage computation
# ─────────────────────────────────────────────
def compute_arb(
    pairs: list[MatchedPair],
    tolerance: float = TOLERANCE,
) -> list[ArbResult]:
    results: list[ArbResult] = []

    for pair in pairs:
        k_yes = pair.kalshi.yes_price
        p_yes = pair.poly.yes_price
        total = k_yes + p_yes
        gap   = 1.0 - total

        if gap > tolerance:
            status = "ARB_OPPORTUNITY"
        elif gap < -tolerance:
            status = "OVER_ROUND"
        else:
            status = "EFFICIENT"

        # Use the Kalshi title as the canonical event name
        event = pair.kalshi.title or pair.poly.question

        results.append(
            ArbResult(
                event=event,
                kalshi_yes=round(k_yes, 4),
                poly_yes=round(p_yes, 4),
                total=round(total, 4),
                arb_gap=round(gap, 4),
                status=status,
                kalshi_ticker=pair.kalshi.ticker,
                poly_condition_id=pair.poly.condition_id,
            )
        )

    return results


# ─────────────────────────────────────────────
#  Display + CSV output
# ─────────────────────────────────────────────
_CSV_FIELDS = [
    "event",
    "kalshi_ticker",
    "poly_condition_id",
    "kalshi_yes",
    "poly_yes",
    "total",
    "arb_gap",
    "status",
]


def display_results(results: list[ArbResult], tolerance: float = TOLERANCE) -> None:
    """Print ARB_OPPORTUNITY rows to stdout and write full CSV."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path  = f"arb_results_{timestamp}.csv"

    # ── Write CSV (all rows) ──────────────────
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "event":            r.event,
                    "kalshi_ticker":    r.kalshi_ticker,
                    "poly_condition_id":r.poly_condition_id,
                    "kalshi_yes":       r.kalshi_yes,
                    "poly_yes":         r.poly_yes,
                    "total":            r.total,
                    "arb_gap":          r.arb_gap,
                    "status":           r.status,
                }
            )

    console.print(f"\n[dim]Full results written to:[/dim] [bold]{csv_path}[/bold]")

    # ── Filter to opportunities ───────────────
    opps = [r for r in results if r.status == "ARB_OPPORTUNITY"]

    if not opps:
        console.print(
            f"\n[bold yellow]No arbitrage opportunities found "
            f"(tolerance={tolerance}).[/bold yellow]\n"
        )
        return

    # ── Rich table ────────────────────────────
    table = Table(
        title=f"⚡  Arbitrage Opportunities  (tolerance ±{tolerance})",
        show_lines=True,
        header_style="bold magenta",
    )
    table.add_column("Event",               style="white",        max_width=50, overflow="fold")
    table.add_column("Kalshi p(Yes)",        style="cyan",         justify="right")
    table.add_column("Polymarket p(Yes)",    style="green",        justify="right")
    table.add_column("Sum",                  style="yellow",       justify="right")
    table.add_column("Arb Gap",              style="bold green",   justify="right")
    table.add_column("Status",               style="bold white",   justify="center")

    for r in opps:
        table.add_row(
            r.event,
            f"{r.kalshi_yes:.4f}",
            f"{r.poly_yes:.4f}",
            f"{r.total:.4f}",
            f"{r.arb_gap:+.4f}",
            "✓ ARB",
        )

    console.print(table)
    console.print(
        f"\n[bold green]{len(opps)} opportunity(-ies) found "
        f"out of {len(results)} matched pairs.[/bold green]\n"
    )


# ─────────────────────────────────────────────
#  CLI entry point
# ─────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan Kalshi & Polymarket for cross-platform arbitrage."
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=TOLERANCE,
        help=f"Arb-gap threshold (default: {TOLERANCE})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=MATCH_THRESHOLD,
        help=f"Fuzzy-match minimum score 0-1 (default: {MATCH_THRESHOLD})",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    console.rule("[bold blue]Kalshi × Polymarket Arbitrage Scanner[/bold blue]")

    try:
        kalshi_markets  = fetch_kalshi_markets()
        poly_markets    = fetch_polymarket_markets()
    except Exception as exc:
        console.print(f"[bold red]Fatal error fetching markets: {exc}[/bold red]")
        raise SystemExit(1)

    if not kalshi_markets:
        console.print("[yellow]No Kalshi markets returned — check your KALSHI_API_KEY.[/yellow]")
    if not poly_markets:
        console.print("[yellow]No Polymarket markets returned.[/yellow]")

    pairs   = match_markets(kalshi_markets, poly_markets, threshold=args.threshold)
    results = compute_arb(pairs, tolerance=args.tolerance)
    display_results(results, tolerance=args.tolerance)


if __name__ == "__main__":
    main()
