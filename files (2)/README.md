# Kalshi × Polymarket Arbitrage Scanner

A command-line tool that fetches live prediction-market data from **Kalshi** and
**Polymarket**, automatically matches markets that cover the same event, and flags
any pairs where a risk-free profit ("arbitrage") may be available.

---

## What is Prediction-Market Arbitrage?

A prediction market lets you buy a contract that pays $1 if an event happens
(a YES contract) or $1 if it doesn't (a NO contract). The price of a YES contract
is a direct estimate of the probability that the event occurs.

**The opportunity:** if you can buy the YES contract for an event on one platform
at, say, 40 cents, and the same event's YES contract on another platform is also
priced at 40 cents, you pay a combined 80 cents. If one of them must pay out $1,
your guaranteed return is 20 cents — no matter what happens. This is arbitrage.

More precisely, the scanner flags a pair when:

```
1 - (p_kalshi_yes + p_polymarket_yes) > TOLERANCE
```

A **positive arb gap** means the market prices sum to *less* than 1 — both sides
are cheap relative to the $1 payout. Buy YES on both platforms and collect the
difference.

A **negative arb gap** (over-round) means the sum exceeds 1 — the "house" takes a
cut and there's no free profit.

---

## Requirements

- Python 3.10 or later
- A free [Kalshi](https://kalshi.com) account and API key
- No Polymarket credentials needed (public read API)

---

## Setup

```bash
# 1. Clone / download this repo
git clone <your-repo-url>
cd <repo-folder>

# 2. Create and activate a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Export your Kalshi API key
export KALSHI_API_KEY="your-kalshi-api-key-here"
# Windows: set KALSHI_API_KEY=your-kalshi-api-key-here
```

---

## Usage

### Basic run (default settings)

```bash
python arb_scanner.py
```

### Custom tolerance

```bash
# Flag opportunities only when the arb gap exceeds 5 %
python arb_scanner.py --tolerance 0.05
```

### Tighter market matching

```bash
# Require a 90 % fuzzy-match score before pairing markets
python arb_scanner.py --threshold 0.90
```

### Combined options

```bash
python arb_scanner.py --tolerance 0.03 --threshold 0.85
```

---

## Output

The tool prints a table to the terminal showing only **ARB_OPPORTUNITY** rows:

```
          ⚡  Arbitrage Opportunities  (tolerance ±0.02)
┌──────────────────────────────┬───────────────┬───────────────────┬────────┬──────────┬────────┐
│ Event                        │ Kalshi p(Yes) │ Polymarket p(Yes) │ Sum    │ Arb Gap  │ Status │
├──────────────────────────────┼───────────────┼───────────────────┼────────┼──────────┼────────┤
│ Will X happen before July?   │ 0.3800        │ 0.5800            │ 0.9600 │ +0.0400  │ ✓ ARB  │
└──────────────────────────────┴───────────────┴───────────────────┴────────┴──────────┴────────┘
```

A timestamped CSV file (`arb_results_YYYYMMDD_HHMMSS.csv`) is also written with
**all** matched pairs (ARB, OVER_ROUND, and EFFICIENT) for auditing purposes.

---

## Configuration constants (top of `arb_scanner.py`)

| Constant          | Default | Meaning                                               |
|-------------------|---------|-------------------------------------------------------|
| `TOLERANCE`       | 0.02    | Minimum arb gap to flag as an opportunity             |
| `MATCH_THRESHOLD` | 0.82    | Minimum fuzzy-match score to accept a market pairing  |
| `PAGE_SIZE`       | 200     | Markets fetched per API page                          |
| `MAX_RETRIES`     | 4       | HTTP retry attempts before giving up                  |
| `REQUEST_TIMEOUT` | 15      | Per-request timeout in seconds                        |

---

## Project structure

```
.
├── arb_scanner.py      # Main program
├── requirements.txt    # Pip dependencies
└── README.md           # This file
```

---

## Disclaimer

This tool is for **educational and research purposes only**. Prediction-market
arbitrage involves real financial risk. Markets can move between the time you
detect an opportunity and the time you place a trade. Always verify prices
manually before committing capital.
