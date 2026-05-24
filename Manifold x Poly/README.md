# Kalshi × Polymarket Arbitrage Scanner

Scans two prediction-market platforms for the same event priced differently —
flagging cases where buying YES on both sides costs less than $1, guaranteeing
a risk-free return.

**No API keys or accounts required.** Both data sources are fully public.

---

## How the arbitrage works

Each YES contract pays $1 if an event happens. If you can buy YES on Kalshi for
40 ¢ and YES on Polymarket for 52 ¢, you spend 92 ¢ total. One platform must
pay out $1, so you pocket 8 ¢ guaranteed — regardless of the outcome.

The scanner flags a pair when:

```
arb_gap = 1.0 − (p_kalshi_yes + p_polymarket_yes) > TOLERANCE
```

A **positive gap** → buy both sides for a locked-in profit.  
A **negative gap** → the platforms are over-priced in aggregate (skip).

---

## Data sources (no keys needed)

| Platform    | Endpoint                             | Auth |
|-------------|--------------------------------------|------|
| Kalshi      | `trading-api.kalshi.com/trade-api/v2/markets` | None (public read) |
| Polymarket  | `gamma-api.polymarket.com/markets`   | None |

Kalshi's trading API allows unauthenticated reads for market listings — only
order placement requires an account. Polymarket's Gamma API is fully public.

---

## Setup

```bash
# 1. Python 3.10+ required
python --version

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

No environment variables or API keys needed.

---

## Usage

```bash
# Default run
python arb_scanner.py

# Looser tolerance — flag gaps > 5 %
python arb_scanner.py --tolerance 0.05

# Stricter market matching
python arb_scanner.py --threshold 0.90

# Combined
python arb_scanner.py --tolerance 0.03 --threshold 0.85
```

---

## Output

Terminal — ARB_OPPORTUNITY rows only:

```
          ⚡  Arbitrage Opportunities  (tolerance ±0.02)
┌─────────────────────────────┬───────────────┬───────────────────┬────────┬──────────┬────────┐
│ Event                       │ Kalshi p(Yes) │ Polymarket p(Yes) │ Sum    │ Arb Gap  │ Status │
├─────────────────────────────┼───────────────┼───────────────────┼────────┼──────────┼────────┤
│ Fed rate cut in July 2025?  │ 0.3800        │ 0.5400            │ 0.9200 │ +0.0800  │ ✓ ARB  │
└─────────────────────────────┴───────────────┴───────────────────┴────────┴──────────┴────────┘
```

CSV — all matched pairs written to `arb_results_YYYYMMDD_HHMMSS.csv`.

---

## Configuration

All tunable constants live at the top of `arb_scanner.py`:

| Constant         | Default | Purpose                                        |
|------------------|---------|------------------------------------------------|
| `TOLERANCE`      | 0.02    | Minimum arb gap to flag as an opportunity      |
| `MATCH_THRESHOLD`| 0.82    | Fuzzy-match score required to pair two markets |
| `PAGE_SIZE`      | 200     | Markets fetched per API page                   |
| `MAX_RETRIES`    | 4       | HTTP retry attempts before giving up           |
| `REQUEST_TIMEOUT`| 20      | Per-request timeout in seconds                 |

---

## Disclaimer

For research and educational purposes only. Market prices can move between
detection and execution. Verify prices manually before committing capital.
