"""
arb_scanner.py — Kalshi × Polymarket arbitrage scanner
Uses only public, no-auth-required data sources:
  • Kalshi  : public REST API (no key needed for market listings)
  • Polymarket : Gamma REST API (fully public)

Python 3.10+
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from rapidfuzz import fuzz
from rich.console import Console
from rich.table import Table

# ──────────────────────────────────────────────────────────────
#  Constants  (edit here or pass via CLI flags)
# ──────────────────────────────────────────────────────────────

# Kalshi public REST API — no authentication required for reads
KALSHI_BASE_URL     = "https://trading-api.kalshi.com/trade-api/v2"

# Polymarket Gamma API — fully public, no key needed
POLYMARKET_BASE_URL = "https://gamma-api.polymarket.com"

MATCH_THRESHOLD = 0.82   # fuzzy-match minimum score (0–1)
TOLERANCE       = 0.02   # arb-gap threshold
PAGE_SIZE       = 200    # markets per page

MAX_RETRIES     = 4      # HTTP retry attempts
BACKOFF_BASE    = 1.5    # exponential back-off multiplier (seconds)
REQUEST_TIMEOUT = 20     # per-request timeout (seconds)

# Realistic browser headers to avoid bot blocks
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

console = Console()


# ──────────────────────────────────────────────────────────────
#  Data models
# ──────────────────────────────────────────────────────────────

@dataclass
class KalshiMarket:
    ticker:    str
    title:     str
    yes_price: float   # implied probability 0.0–1.0


@dataclass
class PolyMarket:
    condition_id: str
    question:     str
    yes_price:    float   # implied probability 0.0–1.0


@dataclass
class MatchedPair:
    kalshi:     KalshiMarket
    poly:       PolyMarket
    similarity: float


@dataclass
class ArbResult:
    event:           str
    kalshi_yes:      float
    poly_yes:        float
    total:           float
    arb_gap:         float
    status:          str    # ARB_OPPORTUNITY | OVER_ROUND | EFFICIENT
    kalshi_ticker:   str
    poly_condition_id: str


# ──────────────────────────────────────────────────────────────
#  HTTP helper — retries with exponential back-off
# ──────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None, extra_headers: dict | None = None) -> Any:
    """GET with retries. Returns parsed JSON or raises RuntimeError."""
    hdrs = {**HEADERS, **(extra_headers or {})}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = httpx.get(url, params=params, headers=hdrs, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = BACKOFF_BASE ** attempt
                console.log(f"[yellow]Rate-limited ({url}). Sleeping {wait:.1f}s…[/yellow]")
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403):
                console.log(f"[red]Access denied ({resp.status_code}) — {url}[/red]")
                raise RuntimeError(f"HTTP {resp.status_code}: access denied for {url}")
            console.log(f"[yellow]HTTP {resp.status_code} on attempt {attempt} — {url}[/yellow]")
            resp.raise_for_status()
        except httpx.TimeoutException:
            wait = BACKOFF_BASE ** attempt
            console.log(f"[yellow]Timeout attempt {attempt}. Retrying in {wait:.1f}s…[/yellow]")
            time.sleep(wait)
        except httpx.HTTPStatusError as exc:
            if attempt == MAX_RETRIES:
                raise RuntimeError(str(exc)) from exc
            time.sleep(BACKOFF_BASE ** attempt)
    raise RuntimeError(f"All {MAX_RETRIES} attempts failed for {url}")


# ──────────────────────────────────────────────────────────────
#  Fetch Kalshi markets  (public endpoint, no auth needed)
# ──────────────────────────────────────────────────────────────

def fetch_kalshi_markets() -> list[KalshiMarket]:
    """
    Pages through Kalshi's public /markets endpoint.

    Authentication is NOT required for listing open markets —
    only for placing orders or accessing account data.

    Price field: yes_ask (cents 0–100)  →  divide by 100 for probability.
    Falls back to yes_bid if yes_ask absent.
    """
    markets: list[KalshiMarket] = []
    cursor: str | None = None
    console.log("[cyan]Fetching Kalshi markets (public API)…[/cyan]")

    while True:
        params: dict[str, Any] = {"status": "open", "limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor

        try:
            data = _get(f"{KALSHI_BASE_URL}/markets", params=params)
        except RuntimeError as exc:
            console.log(f"[red]Kalshi fetch failed: {exc}[/red]")
            break

        raw = data.get("markets", [])
        for m in raw:
            # Prefer ask price; fall back to mid of bid/ask
            yes_ask = m.get("yes_ask")
            yes_bid = m.get("yes_bid")
            if yes_ask is not None:
                cents = yes_ask
            elif yes_bid is not None:
                cents = yes_bid
            else:
                continue

            price = cents / 100.0
            if price <= 0.0 or price >= 1.0:   # skip zero-liquidity extremes
                continue

            markets.append(KalshiMarket(
                ticker    = m.get("ticker", ""),
                title     = m.get("title", ""),
                yes_price = price,
            ))

        cursor = data.get("cursor")
        if not cursor or len(raw) < PAGE_SIZE:
            break

    console.log(f"[cyan]Kalshi: {len(markets)} usable markets.[/cyan]")
    return markets


# ──────────────────────────────────────────────────────────────
#  Fetch Polymarket markets  (Gamma API — fully public)
# ──────────────────────────────────────────────────────────────

def fetch_polymarket_markets() -> list[PolyMarket]:
    """
    Uses Polymarket's public Gamma API (gamma-api.polymarket.com).
    No API key or wallet connection required.

    Each market has an `outcomePrices` JSON-string array e.g. ["0.62","0.38"]
    aligned with the `outcomes` array e.g. ["Yes","No"].
    """
    markets: list[PolyMarket] = []
    offset   = 0
    console.log("[cyan]Fetching Polymarket markets (Gamma API)…[/cyan]")

    while True:
        params: dict[str, Any] = {
            "active":   "true",
            "closed":   "false",
            "limit":    PAGE_SIZE,
            "offset":   offset,
        }
        try:
            raw = _get(f"{POLYMARKET_BASE_URL}/markets", params=params)
        except RuntimeError as exc:
            console.log(f"[red]Polymarket fetch failed: {exc}[/red]")
            break

        # Gamma API returns a plain list
        if not isinstance(raw, list):
            raw = raw.get("data", []) if isinstance(raw, dict) else []

        for m in raw:
            question = m.get("question", "")
            outcomes_raw = m.get("outcomes", "[]")
            prices_raw   = m.get("outcomePrices", "[]")

            # Both fields are sometimes JSON strings, sometimes already lists
            if isinstance(outcomes_raw, str):
                import json
                try:
                    outcomes = json.loads(outcomes_raw)
                    prices   = json.loads(prices_raw)
                except Exception:
                    continue
            else:
                outcomes = outcomes_raw
                prices   = prices_raw

            if not outcomes or not prices or len(outcomes) != len(prices):
                continue

            # Find the YES outcome index
            yes_idx = next(
                (i for i, o in enumerate(outcomes) if str(o).lower() == "yes"),
                None,
            )
            if yes_idx is None:
                continue

            try:
                price = float(prices[yes_idx])
            except (ValueError, TypeError):
                continue

            if price <= 0.0 or price >= 1.0:
                continue

            markets.append(PolyMarket(
                condition_id = m.get("conditionId", m.get("condition_id", "")),
                question     = question,
                yes_price    = price,
            ))

        if len(raw) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    console.log(f"[cyan]Polymarket: {len(markets)} usable markets.[/cyan]")
    return markets


# ──────────────────────────────────────────────────────────────
#  String normalisation
# ──────────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[^\w\s]")

def _normalise(text: str) -> str:
    text = text.lower()
    text = _PUNCT.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ──────────────────────────────────────────────────────────────
#  Market matching
# ──────────────────────────────────────────────────────────────

def match_markets(
    kalshi_markets: list[KalshiMarket],
    poly_markets:   list[PolyMarket],
    threshold:      float = MATCH_THRESHOLD,
) -> list[MatchedPair]:
    """
    For each Kalshi market find the single best Polymarket match using
    RapidFuzz token_sort_ratio (handles word-order differences).
    """
    pairs: list[MatchedPair] = []
    poly_norm = [(_normalise(p.question), p) for p in poly_markets]

    for k in kalshi_markets:
        k_norm     = _normalise(k.title)
        best_score = 0.0
        best_poly: PolyMarket | None = None

        for p_norm, p in poly_norm:
            score = fuzz.token_sort_ratio(k_norm, p_norm) / 100.0
            if score > best_score:
                best_score = score
                best_poly  = p

        if best_poly is not None and best_score >= threshold:
            pairs.append(MatchedPair(kalshi=k, poly=best_poly, similarity=best_score))

    console.log(f"[cyan]{len(pairs)} matched pairs (threshold={threshold}).[/cyan]")
    return pairs


# ──────────────────────────────────────────────────────────────
#  Arbitrage computation
# ──────────────────────────────────────────────────────────────

def compute_arb(
    pairs:     list[MatchedPair],
    tolerance: float = TOLERANCE,
) -> list[ArbResult]:
    results: list[ArbResult] = []

    for pair in pairs:
        k_yes = pair.kalshi.yes_price
        p_yes = pair.poly.yes_price
        total = k_yes + p_yes
        gap   = 1.0 - total

        if gap > tolerance:
            status = "ARB_OPPORTUNITY"    # buy YES on both → guaranteed profit
        elif gap < -tolerance:
            status = "OVER_ROUND"         # house edge > tolerance, skip
        else:
            status = "EFFICIENT"

        results.append(ArbResult(
            event            = pair.kalshi.title or pair.poly.question,
            kalshi_yes       = round(k_yes, 4),
            poly_yes         = round(p_yes, 4),
            total            = round(total, 4),
            arb_gap          = round(gap, 4),
            status           = status,
            kalshi_ticker    = pair.kalshi.ticker,
            poly_condition_id= pair.poly.condition_id,
        ))

    return results


# ──────────────────────────────────────────────────────────────
#  Display + CSV output
# ──────────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "event", "kalshi_ticker", "poly_condition_id",
    "kalshi_yes", "poly_yes", "total", "arb_gap", "status",
]


def display_results(results: list[ArbResult], tolerance: float = TOLERANCE) -> None:
    """Print ARB_OPPORTUNITY rows via Rich; write all rows to CSV."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path  = f"arb_results_{timestamp}.csv"

    # ── CSV (all statuses) ──────────────────────────────────────
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for r in results:
            writer.writerow({
                "event":             r.event,
                "kalshi_ticker":     r.kalshi_ticker,
                "poly_condition_id": r.poly_condition_id,
                "kalshi_yes":        r.kalshi_yes,
                "poly_yes":          r.poly_yes,
                "total":             r.total,
                "arb_gap":           r.arb_gap,
                "status":            r.status,
            })

    console.print(f"\n[dim]All results saved to[/dim] [bold]{csv_path}[/bold]")

    # ── Terminal table (ARB_OPPORTUNITY only) ───────────────────
    opps = [r for r in results if r.status == "ARB_OPPORTUNITY"]

    if not opps:
        console.print(
            f"\n[bold yellow]No arbitrage opportunities found "
            f"(tolerance=±{tolerance}).[/bold yellow]\n"
        )
        return

    table = Table(
        title=f"⚡  Arbitrage Opportunities  (tolerance ±{tolerance})",
        show_lines=True,
        header_style="bold magenta",
    )
    table.add_column("Event",              style="white",      max_width=48, overflow="fold")
    table.add_column("Kalshi p(Yes)",      style="cyan",       justify="right")
    table.add_column("Polymarket p(Yes)",  style="green",      justify="right")
    table.add_column("Sum",                style="yellow",     justify="right")
    table.add_column("Arb Gap",            style="bold green", justify="right")
    table.add_column("Status",             style="bold white", justify="center")

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


# ──────────────────────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scan Kalshi & Polymarket for arbitrage (no API keys required)."
    )
    p.add_argument("--tolerance", type=float, default=TOLERANCE,
                   help=f"Arb-gap threshold (default: {TOLERANCE})")
    p.add_argument("--threshold", type=float, default=MATCH_THRESHOLD,
                   help=f"Fuzzy-match minimum 0–1 (default: {MATCH_THRESHOLD})")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    console.rule("[bold blue]Kalshi × Polymarket Arbitrage Scanner[/bold blue]")
    console.print("[dim]No API keys required — using public endpoints only.[/dim]\n")

    try:
        kalshi_markets = fetch_kalshi_markets()
        poly_markets   = fetch_polymarket_markets()
    except Exception as exc:
        console.print(f"[bold red]Fatal error: {exc}[/bold red]")
        raise SystemExit(1)

    if not kalshi_markets:
        console.print("[yellow]Warning: zero Kalshi markets returned.[/yellow]")
    if not poly_markets:
        console.print("[yellow]Warning: zero Polymarket markets returned.[/yellow]")

    pairs   = match_markets(kalshi_markets, poly_markets, threshold=args.threshold)
    results = compute_arb(pairs, tolerance=args.tolerance)
    display_results(results, tolerance=args.tolerance)


if __name__ == "__main__":
    main()
