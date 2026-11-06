# region imports
from AlgorithmImports import *
# endregion
"""Factor strategy (docs/FACTOR_STRATEGY.md) on QuantConnect's LEAN.

An independent replication of variants 2 and 4 on QuantConnect's data:
every US equity since 1998, dead companies included, with Morningstar
fundamentals. That answers the two data limits our own backtest states up
front: survivorship (only 4% of dead companies priced) and no data before
2009. It is a second implementation on different data, so its numbers will
not match ours to the decimal. Agreement in direction is what to look for.

Parameters (QuantConnect project parameters, or edited below):
  variant   2 or 4 (default 4)
  start     first year traded (default 2015; 1999 covers the dot-com bust)
  end       last year (default 2025)

Differences from our engine, stated rather than hidden:
  - Rebalances on the first trading day of each month, filling at the next
    daily bar, not at the prior month-end close.
  - Industry limit uses Morningstar industry groups, not 2-digit SIC.
  - Costs are QuantConnect's default fee and slippage models, not our
    liquidity tiers; dividends are reinvested in full (no 30% withholding).
  - Earnings growth is Morningstar's one-year diluted EPS growth; share
    growth and momentum are computed here as registered.
  - Money not in picks sits in SPY (our engine parks it in the S&P 500 TR).
"""

import math
from collections import defaultdict, deque


RULES = {
    2: dict(holdings=30, buffer=60, max_weight=None, max_per_industry=None),
    4: dict(holdings=100, buffer=200, max_weight=0.02, max_per_industry=15),
}
MIN_MARKET_CAP = 300e6
MIN_DOLLAR_VOLUME = 2e6


def _num(getter, fallback=None):
    """A Morningstar field as a float, or None when missing or zero-filled."""
    try:
        value = getter()
    except Exception:
        value = None
    if value is None and fallback is not None:
        try:
            value = fallback()
        except Exception:
            return None
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or value == 0.0:
        return None
    return value


def _ranks(values):
    """Percentile ranks 0..1, ties sharing the average rank."""
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 0.5}
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        rank = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            out[items[k][0]] = rank
        i = j + 1
    return out


class FactorStrategy(QCAlgorithm):

    def initialize(self):
        # Prime Directive 1 (README): this project never places a real order.
        # QuantConnect can trade live through a linked broker; this algorithm
        # refuses to, so it can only ever be a backtest.
        if self.live_mode:
            raise RuntimeError("backtest only: this algorithm never trades live")
        self.variant = int(self.get_parameter("variant") or 4)
        start = int(self.get_parameter("start") or 2015)
        end = int(self.get_parameter("end") or 2025)
        self.rules = RULES[self.variant]

        # A year of warm-up before the first trade, so share growth has a
        # year-ago count to compare with.
        self.set_start_date(start - 1, 1, 1)
        if end == 2025:
            self.set_end_date(2025, 9, 30)  # our backtest's last month-end
        else:
            self.set_end_date(end, 12, 31)
        self.trade_from = datetime(start, 1, 1)
        self.set_cash(100_000)

        self.spy = self.add_equity("SPY", Resolution.DAILY).symbol
        self.set_benchmark(self.spy)
        self.universe_settings.resolution = Resolution.DAILY
        self.add_universe(self.select)

        excluded = []
        for name in dir(MorningstarIndustryGroupCode):
            upper = name.upper()
            if "BANK" in upper or "INSURANCE" in upper:
                value = getattr(MorningstarIndustryGroupCode, name)
                if isinstance(value, int):
                    excluded.append(value)
        self.excluded_groups = set(excluded)

        self.last_month = None
        self.shares_history = defaultdict(lambda: deque(maxlen=13))
        self.targets = None
        self.industry = {}
        self.trims = 0
        self.rebalances = 0

        # Two trading days after the month's selection, so names added to
        # the universe have a daily bar (and a price) before they are bought.
        self.schedule.on(self.date_rules.month_start(self.spy, 2),
                         self.time_rules.after_market_open(self.spy, 30),
                         self.rebalance)

    # ------------------------------------------------------------- selection

    def select(self, fundamentals):
        month = (self.time.year, self.time.month)
        if month == self.last_month:
            return Universe.UNCHANGED
        self.last_month = month

        eligible = {}
        for f in fundamentals:
            if not f.has_fundamental_data:
                continue
            shares = _num(lambda: f.company_profile.shares_outstanding)
            if shares:
                self.shares_history[f.symbol].append(shares)
            cap = _num(lambda: f.market_cap)
            dv = _num(lambda: f.dollar_volume)
            if cap is None or cap < MIN_MARKET_CAP or dv is None or dv < MIN_DOLLAR_VOLUME:
                continue
            try:
                if f.company_reference.is_reit or f.security_reference.is_depositary_receipt:
                    continue
                if not f.security_reference.is_primary_share:
                    continue
                group = f.asset_classification.morningstar_industry_group_code
            except Exception:
                continue
            if group in self.excluded_groups:
                continue
            eligible[f.symbol] = (f, cap, group)

        if self.time < self.trade_from - timedelta(days=40):
            return [self.spy]

        inputs = {}
        for symbol, (f, cap, group) in eligible.items():
            fs = f.financial_statements
            ebit = _num(lambda: fs.income_statement.ebit.twelve_months)
            gross = _num(lambda: fs.income_statement.gross_profit.twelve_months)
            ni = _num(lambda: fs.income_statement.net_income.twelve_months)
            ocf = _num(lambda: fs.cash_flow_statement.operating_cash_flow.twelve_months)
            fcf = _num(lambda: fs.cash_flow_statement.free_cash_flow.twelve_months)
            assets = _num(lambda: fs.balance_sheet.total_assets.twelve_months,
                          lambda: fs.balance_sheet.total_assets.value)
            ev = _num(lambda: f.company_profile.enterprise_value)
            eps_growth = _num(lambda: f.earning_ratios.diluted_eps_growth.one_year)
            history = self.shares_history.get(symbol)
            share_growth = None
            if history and len(history) >= 13 and history[0]:
                share_growth = history[-1] / history[0] - 1
            row = {}
            if ev and ev > 0:
                if ebit is not None:
                    row["ebit_ev"] = ebit / ev
                if fcf is not None:
                    row["fcf_ev"] = fcf / ev
            if assets and assets > 0:
                if gross is not None:
                    row["gross_prof"] = gross / assets
                if ni is not None and ocf is not None:
                    row["accruals"] = -(ni - ocf) / assets
            if share_growth is not None:
                row["buyback"] = -share_growth
            if eps_growth is not None:
                row["eps_growth"] = eps_growth
            inputs[symbol] = row
            self.industry[symbol] = group

        # 12-1 month momentum for everything eligible, in one history call.
        symbols = list(inputs)
        if symbols:
            closes = self.history(symbols, 260, Resolution.DAILY)
            if not closes.empty and "close" in closes:
                closes = closes["close"].unstack(level=0)
                if len(closes) >= 240:
                    then, now = closes.iloc[0], closes.iloc[-21]
                    for symbol in symbols:
                        try:
                            a, b = float(then[symbol]), float(now[symbol])
                        except Exception:
                            continue
                        if a > 0 and not math.isnan(a) and not math.isnan(b):
                            inputs[symbol]["momentum"] = b / a - 1

        themes = [("ebit_ev", "fcf_ev"), ("gross_prof", "accruals"), ("buyback",),
                  ("momentum", "eps_growth")]
        ranks = {}
        for theme in themes:
            for key in theme:
                ranks[key] = _ranks({s: r[key] for s, r in inputs.items() if key in r})
        scores = {}
        for symbol in inputs:
            theme_scores = []
            for theme in themes:
                parts = [ranks[k][symbol] for k in theme if symbol in ranks[k]]
                if parts:
                    theme_scores.append(sum(parts) / len(parts))
            if len(theme_scores) >= 3:
                scores[symbol] = sum(theme_scores) / len(theme_scores)

        ranked = sorted(scores, key=lambda s: -scores[s])
        self.targets = ranked
        keep = set(ranked[: self.rules["buffer"]])
        held = {kv.key for kv in self.portfolio if kv.value.invested}
        return list(keep | held | {self.spy})

    # ------------------------------------------------------------- rebalance

    def rebalance(self):
        if self.targets is None or self.time < self.trade_from:
            return
        ranked, self.targets = self.targets, None
        rules = self.rules
        rank_of = {s: i for i, s in enumerate(ranked)}
        value = self.portfolio.total_portfolio_value
        if value <= 0:
            return

        held = [kv.key for kv in self.portfolio
                if kv.value.invested and kv.key != self.spy]
        weights = {}
        for symbol in held:
            rank = rank_of.get(symbol)
            if rank is None or rank >= rules["buffer"]:
                weights[symbol] = 0.0  # left the buffer or the universe
                continue
            w = self.portfolio[symbol].holdings_value / value
            if rules["max_weight"] and w > rules["max_weight"]:
                w = 1.0 / rules["holdings"]
                self.trims += 1
            weights[symbol] = w

        kept = [s for s, w in weights.items() if w > 0]
        per_group = defaultdict(int)
        for s in kept:
            per_group[self.industry.get(s)] += 1
        slots = rules["holdings"] - len(kept)
        new = []
        for symbol in ranked:
            if slots <= 0:
                break
            if symbol in weights or not self.securities.contains_key(symbol):
                continue
            if not self.securities[symbol].has_data or self.securities[symbol].price <= 0:
                continue
            group = self.industry.get(symbol)
            cap = rules["max_per_industry"]
            if cap and group is not None and per_group[group] >= cap:
                continue
            new.append(symbol)
            per_group[group] += 1
            slots -= 1

        room = max(0.0, 0.99 - sum(w for w in weights.values()))
        each = min(1.0 / rules["holdings"], room / len(new)) if new else 0.0
        for symbol in new:
            weights[symbol] = each
        weights[self.spy] = max(0.0, 0.99 - sum(weights.values()))

        targets = [PortfolioTarget(s, w) for s, w in sorted(weights.items(), key=lambda kv: kv[1])]
        self.set_holdings(targets)
        self.rebalances += 1

    def on_end_of_algorithm(self):
        self.log(f"variant {self.variant}: {self.rebalances} rebalances, "
                 f"{self.trims} trims")
        self.set_runtime_statistic("Variant", str(self.variant))
        self.set_runtime_statistic("Trims", str(self.trims))
