---
name: trader-quant
description: >-
  Quant Signal. A 60-day trend rule on daily closes: today's close against the
  average close of the previous 60 sessions. Above the average is BUY, below is
  SELL (exit or stay out, long side only). Prints the percent gap, where today's
  close ranks among the last 60, and a backtest of the same rule over about two
  years (next-day holding, a cost on every switch) against simply holding. Works
  on any Yahoo ticker, crypto included (XMR-USD). Use when the user says "run my
  quant model", "60-day signal", "is TSLA a buy on my model", "trend signal",
  "backtest my rule", or wants a daily buy/sell read on a watchlist.
argument-hint: "[TICKER ...] [--window 60] [--cost-bps 5]"
---

# trader-quant

The user's own model. One rule, read on daily closes:

- **Gap** = today's close / average of the previous 60 closes - 1.
- Gap above 0: **BUY**. Below 0: **SELL**. Exactly 0: FLAT.
- **Rank** = the share of the last 60 closes at or below today's close, shown beside it.

`${CLAUDE_SKILL_DIR}` is this skill's folder.

```bash
python3 "${CLAUDE_SKILL_DIR}/quant.py" TSLA NVDA XMR-USD
python3 "${CLAUDE_SKILL_DIR}/quant.py" TSLA --window 60 --cost-bps 5 --json
python3 "${CLAUDE_SKILL_DIR}/quant.py" TSLA --offline TSLA-2y.json
```

It reads Yahoo's daily chart (2 years, `interval=1d`), free and unofficial. During
the session today's bar is still moving, so a signal near 0 can flip by the close.
It writes nothing.

## The backtest

The same rule on every past day: BUY means hold the next day, SELL means cash.
Each switch costs `--cost-bps` (5 by default). It prints the rule's return, buy
and hold's return, the number of switches, the share of days in the market and
the worst drawdown. Always show the backtest line next to the signal, and say
plainly when the rule lost to holding.

## Steps

1. Run it for the tickers the user names (or `trader/watchlist.json` hits).
2. One line per ticker: signal, gap, rank, then the backtest line as printed.
3. A BUY is a read, not a trade. If the user wants to act, it goes the usual way:
   `/trader-news`, `/trader-exit` for a stop, `/trader-budget`, `/trader-risk`,
   `/trader-broker` only after their yes. A SELL on something they hold goes to
   trader-broker's flatten, with the same checks.

## Rules

- Never change the window or the rule to make a backtest look better unless the
  user asks; if they do, say that tuning on the same history overstates it.
- Long side only: SELL never means short.
- No predictions and no promised returns. A past backtest is not a forecast.

## Safety

- Education and tooling, not financial advice. A signal is a rule's output, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
