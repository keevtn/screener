"""Market data: daily bars behind a swappable provider + trading-day calendar.

docs/ROADMAP.md task 0.4. The daily-bars provider (yfinance now, swappable) and
the SPY-derived trading calendar are the foundation the grader (0.5) and the
signal lab (5c) stand on. All return math uses adj_close; all horizon math uses
trading days (I10).
"""

from pipeline.marketdata.calendar import CalendarRangeError, TradingCalendar
from pipeline.marketdata.finviz import (
    FinvizAuthError,
    FinvizProvider,
    FinvizSchemaError,
    FundamentalsRow,
)
from pipeline.marketdata.provider import BAR_COLUMNS, MarketDataProvider
from pipeline.marketdata.symbol_directory import SymbolDirectoryProvider
from pipeline.marketdata.universe import (
    UniverseResult,
    apply_criteria,
    compute_diff,
    materialize,
)

__all__ = [
    "BAR_COLUMNS",
    "CalendarRangeError",
    "FinvizAuthError",
    "FinvizProvider",
    "FinvizSchemaError",
    "FundamentalsRow",
    "MarketDataProvider",
    "SymbolDirectoryProvider",
    "TradingCalendar",
    "UniverseResult",
    "apply_criteria",
    "compute_diff",
    "materialize",
]
