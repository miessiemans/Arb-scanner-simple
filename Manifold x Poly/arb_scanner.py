"""
arb_scanner.py — Manifold Markets × Polymarket arbitrage scanner
Both APIs are fully public — no account, API key, or region restriction.

NOTE: Manifold uses play-money (Mana), so this is purely educational.
      Polymarket uses real money but is read-only here.

Python 3.10+
"""

from __future__ import annotations

import argparse
import csv
import json
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
#  Constants
# ──────────────────────────────────────────────────────────────

MANIFOLD_BASE_URL   = "https://api.manifold.markets/v0"
POLYMARKET_BASE_URL = "https://gamma-api.polymarket.com"

MATCH_THRESHOLD = 0.82   # fuzzy-match minimum score (0–1)
TOLERANCE       = 0.02   # arb-gap threshold
PAGE_SIZE         = 500    # markets per Manifold page (max 1000)
MANIFOLD_MAX      = 8000   # stop after this many Manifold markets (most active first)
POLY_PAGE_SIZE    = 100    # markets per Polymarket page (Gamma API hard cap is 100)

MAX_RETRIES     = 4
BACKOFF_BASE    = 1.5    # seconds
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent":      "arb-scanner/1.0 (educational use)",
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

console = Console()


# ──────────────────────────────────────────────────────────────
#  Data models
# ──────────────────────────────────────────────────────────────

@dataclass
class ManifoldMarket:
    market_id: str
    question:  str
    yes_price: float   # 0.0–1.0


@dataclass
class PolyMarket:
    condition_id: str
    question:     str
    yes_price:    float   # 0.0–1.0


@dataclass
class MatchedPair:
    manifold:   ManifoldMarket
    poly:       PolyMarket
    similarity: float


@dataclass
class ArbResult:
    event:            str
    manifold_yes:     float
    poly_yes:         float
    total:            float
    arb_gap:          float
    status:           str    # ARB_OPPORTUNITY | OVER_ROUND | EFFICIENT
    manifold_id:      str
    poly_condition_id: str


# ──────────────────────────────────────────────────────────────
#  HTTP helper
# ──────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None) -> Any:
    """GET with exponential back-off retries. Returns parsed JSON."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = httpx.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = BACKOFF_BASE ** attempt
                console.log(f"[yellow]Rate-limited. Sleeping {wait:.1f}s…[/yellow]")
                time.sleep(wait)
                continue
            if resp.status_code == 422:
                # Pagination limit reached — stop cleanly, no point retrying
                raise RuntimeError(f"Pagination limit reached (HTTP 422) for {url}")
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
#  Fetch Manifold markets  (fully public, no auth)
# ──────────────────────────────────────────────────────────────

def fetch_manifold_markets() -> list[ManifoldMarket]:
    """
    Pages through Manifold's /v0/markets endpoint.
    Only BINARY (yes/no) markets are kept — they have a direct `probability` field.
    Pagination uses the `before` param (ID of last market in previous page).
    """
    markets: list[ManifoldMarket] = []
    before: str | None = None
    console.log("[cyan]Fetching Manifold Markets (public API, no key needed)…[/cyan]")

    while True:
        params: dict[str, Any] = {
            "limit":  PAGE_SIZE,
            "sort":   "last-bet-time",   # most active first
            "order":  "desc",
        }
        if before:
            params["before"] = before

        try:
            raw = _get(f"{MANIFOLD_BASE_URL}/markets", params=params)
        except RuntimeError as exc:
            console.log(f"[red]Manifold fetch failed: {exc}[/red]")
            break

        if not raw:
            break

        for m in raw:
            # Only keep binary (YES/NO) open markets with a valid probability
            if m.get("outcomeType") != "BINARY":
                continue
            if m.get("isResolved", False):
                continue
            prob = m.get("probability")
            if prob is None:
                continue
            price = float(prob)
            if price <= 0.0 or price >= 1.0:
                continue

            markets.append(ManifoldMarket(
                market_id = m.get("id", ""),
                question  = m.get("question", ""),
                yes_price = price,
            ))

        if len(raw) < PAGE_SIZE or len(markets) >= MANIFOLD_MAX:
            break
        before = raw[-1]["id"]   # cursor for next page

    console.log(f"[cyan]Manifold: {len(markets)} usable markets.[/cyan]")
    return markets


# ──────────────────────────────────────────────────────────────
#  Fetch Polymarket markets  (Gamma API — fully public)
# ──────────────────────────────────────────────────────────────

def fetch_polymarket_markets() -> list[PolyMarket]:
    """
    Uses Polymarket's public Gamma API.
    outcomePrices is a JSON-string array e.g. '["0.62","0.38"]'
    aligned with outcomes e.g. '["Yes","No"]'.
    """
    markets: list[PolyMarket] = []
    offset = 0
    console.log("[cyan]Fetching Polymarket markets (Gamma API)…[/cyan]")

    while True:
        params: dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit":  POLY_PAGE_SIZE,
            "offset": offset,
        }
        try:
            raw = _get(f"{POLYMARKET_BASE_URL}/markets", params=params)
        except RuntimeError as exc:
            if "422" in str(exc):
                console.log(f"[dim]Polymarket pagination limit reached at offset {offset} — stopping.[/dim]")
            else:
                console.log(f"[red]Polymarket fetch failed: {exc}[/red]")
            break

        if not isinstance(raw, list):
            raw = raw.get("data", []) if isinstance(raw, dict) else []

        for m in raw:
            outcomes_raw = m.get("outcomes", "[]")
            prices_raw   = m.get("outcomePrices", "[]")

            if isinstance(outcomes_raw, str):
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
                question     = m.get("question", ""),
                yes_price    = price,
            ))

        if len(raw) < POLY_PAGE_SIZE:
            break
        offset += POLY_PAGE_SIZE

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
    manifold_markets: list[ManifoldMarket],
    poly_markets:     list[PolyMarket],
    threshold:        float = MATCH_THRESHOLD,
) -> list[MatchedPair]:
    """
    For each Manifold market find the single best Polymarket match.
    RapidFuzz token_sort_ratio handles word-order differences gracefully.
    """
    pairs: list[MatchedPair] = []
    poly_norm = [(_normalise(p.question), p) for p in poly_markets]

    for m in manifold_markets:
        m_norm     = _normalise(m.question)
        best_score = 0.0
        best_poly: PolyMarket | None = None

        for p_norm, p in poly_norm:
            score = fuzz.token_sort_ratio(m_norm, p_norm) / 100.0
            if score > best_score:
                best_score = score
                best_poly  = p

        if best_poly is not None and best_score >= threshold:
            pairs.append(MatchedPair(manifold=m, poly=best_poly, similarity=best_score))

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
        m_yes = pair.manifold.yes_price
        p_yes = pair.poly.yes_price
        total = m_yes + p_yes
        gap   = 1.0 - total

        if gap > tolerance:
            status = "ARB_OPPORTUNITY"
        elif gap < -tolerance:
            status = "OVER_ROUND"
        else:
            status = "EFFICIENT"

        results.append(ArbResult(
            event             = pair.manifold.question or pair.poly.question,
            manifold_yes      = round(m_yes, 4),
            poly_yes          = round(p_yes, 4),
            total             = round(total, 4),
            arb_gap           = round(gap, 4),
            status            = status,
            manifold_id       = pair.manifold.market_id,
            poly_condition_id = pair.poly.condition_id,
        ))

    return results


# ──────────────────────────────────────────────────────────────
#  Display + CSV output
# ──────────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "event", "manifold_id", "poly_condition_id",
    "manifold_yes", "poly_yes", "total", "arb_gap", "status",
]


def display_results(results: list[ArbResult], tolerance: float = TOLERANCE) -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path  = f"arb_results_{timestamp}.csv"

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for r in results:
            writer.writerow({
                "event":             r.event,
                "manifold_id":       r.manifold_id,
                "poly_condition_id": r.poly_condition_id,
                "manifold_yes":      r.manifold_yes,
                "poly_yes":          r.poly_yes,
                "total":             r.total,
                "arb_gap":           r.arb_gap,
                "status":            r.status,
            })

    console.print(f"\n[dim]All results saved to[/dim] [bold]{csv_path}[/bold]")

    opps = [r for r in results if r.status == "ARB_OPPORTUNITY"]

    if not opps:
        console.print(
            f"\n[bold yellow]No arbitrage opportunities found "
            f"(tolerance=±{tolerance}).[/bold yellow]\n"
        )
        console.print(
            "[dim]Tip: try --tolerance 0.05 to widen the net, "
            "or --threshold 0.75 to match more markets.[/dim]\n"
        )
        return

    table = Table(
        title=f"⚡  Arbitrage Opportunities  (tolerance ±{tolerance})",
        show_lines=True,
        header_style="bold magenta",
    )
    table.add_column("Event",              style="white",      max_width=48, overflow="fold")
    table.add_column("Manifold p(Yes)",    style="cyan",       justify="right")
    table.add_column("Polymarket p(Yes)",  style="green",      justify="right")
    table.add_column("Sum",                style="yellow",     justify="right")
    table.add_column("Arb Gap",            style="bold green", justify="right")
    table.add_column("Status",             style="bold white", justify="center")

    for r in opps:
        table.add_row(
            r.event,
            f"{r.manifold_yes:.4f}",
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
    console.print(
        "[dim]Note: Manifold uses play-money (Mana). "
        "These are educational signals only.[/dim]\n"
    )


# ──────────────────────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scan Manifold & Polymarket for pricing discrepancies (no API keys required)."
    )
    p.add_argument("--tolerance", type=float, default=TOLERANCE,
                   help=f"Arb-gap threshold (default: {TOLERANCE})")
    p.add_argument("--threshold", type=float, default=MATCH_THRESHOLD,
                   help=f"Fuzzy-match minimum 0–1 (default: {MATCH_THRESHOLD})")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    console.rule("[bold blue]Manifold × Polymarket Arbitrage Scanner[/bold blue]")
    console.print("[dim]No API keys required — works from anywhere in the world.[/dim]\n")

    try:
        manifold_markets = fetch_manifold_markets()
        poly_markets     = fetch_polymarket_markets()
    except Exception as exc:
        console.print(f"[bold red]Fatal error: {exc}[/bold red]")
        raise SystemExit(1)

    if not manifold_markets:
        console.print("[yellow]Warning: zero Manifold markets returned.[/yellow]")
    if not poly_markets:
        console.print("[yellow]Warning: zero Polymarket markets returned.[/yellow]")

    pairs   = match_markets(manifold_markets, poly_markets, threshold=args.threshold)
    results = compute_arb(pairs, tolerance=args.tolerance)
    display_results(results, tolerance=args.tolerance)


if __name__ == "__main__":
    main()