"""
ticker_extractor.py
===================
Extracts stock ticker symbols from financial news text.

Three-pass approach:
  1. Pattern matching  — catches $AAPL, (AAPL), NYSE: AAPL  (high precision)
  2. Company name map  — major company names + unambiguous subsidiary/brand
                         names read through to the listed parent (YouTube→GOOGL)
  3. EDGAR CIK lookup  — resolves the 10-digit CIK in filing titles (needs cik_map)

Returns a sorted, deduplicated list of ticker strings.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_DOLLAR_PATTERN   = re.compile(r'\$([A-Z]{1,5})\b')
_PAREN_PATTERN    = re.compile(r'\(([A-Z]{1,5})\)')
_EXCHANGE_PATTERN = re.compile(r'(?:NYSE|NASDAQ|AMEX):\s*([A-Z]{1,5})\b')
# EDGAR filing titles embed the filer's CIK zero-padded to 10 digits, e.g.
# "8-K - GENCO SHIPPING & TRADING LTD (0001326200) (Filer)". A 10-digit number in
# parens is a near-unambiguous EDGAR signature — years like "(2024)" or other
# parenthesised numbers won't match — so resolving it via a CIK map is safe.
_CIK_PATTERN      = re.compile(r'\((\d{10})\)')

# Words that look like tickers but are not — filtered out after extraction.
_FALSE_POSITIVES: frozenset[str] = frozenset({
    # Abbreviations / acronyms common in financial text
    "THE", "AND", "FOR", "ARE", "NOT", "NEW", "ALL", "INC", "LLC", "LTD",
    "PLC", "ETF", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FDA", "FED",
    "GDP", "CPI", "USD", "EUR", "GBP", "JPY", "YOY", "QOQ", "MOM",
    "ESG", "AI", "ML", "EV", "API", "ATH", "ATL", "YTD", "OTC",
    # Exchange / market names
    "NYSE", "NASDAQ", "AMEX", "LSE", "TSX",
    # Common financial terms that appear uppercased
    "HOLD", "SELL", "BUY", "RATE", "BOND", "DEBT", "CASH", "RISK",
    "LOSS", "GAIN", "FUND", "RATE", "NOTE", "BILL", "SWAP",
})

# Major crypto symbols. These are NOT in SEC's equity universe, so a strict
# "must be an SEC-listed ticker" check would wrongly drop legitimate crypto
# cashtags ($BTC, $ETH) from social posts. Callers building a validation set for
# social feeds union this in so real crypto mentions survive while meme/garbage
# cashtags ($YOLO, $MOON) are still rejected. Kept deliberately small and
# well-known — obscure alt-coins are out of scope.
CRYPTO_TICKERS: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "DOT", "AVAX", "LINK",
    "MATIC", "LTC", "BCH", "SHIB", "TRX", "UNI", "ATOM", "XLM", "ETC",
    "NEAR", "APT", "ARB", "OP", "PEPE", "USDT", "USDC", "BNB",
    "AAVE", "TAO",
})

# Market-index tickers. Like crypto, these are heavily discussed on social feeds
# but are not listed securities, so they'd be stripped by a listed-only check.
# Kept small and unambiguous; extend as needed (NDX, DJI, RUT, …).
INDEX_TICKERS: frozenset[str] = frozenset({
    "SPX", "VIX",
})

# ---------------------------------------------------------------------------
# Company name → ticker map
# ---------------------------------------------------------------------------

_COMPANY_TICKERS: dict[str, str] = {
    # Big Tech / MAMAA
    "apple":                    "AAPL",
    "microsoft":                "MSFT",
    "amazon":                   "AMZN",
    "alphabet":                 "GOOGL",
    "google":                   "GOOGL",
    "meta":                     "META",
    "netflix":                  "NFLX",
    "nvidia":                   "NVDA",
    "amd":                      "AMD",
    "intel":                    "INTC",
    "qualcomm":                 "QCOM",
    "broadcom":                 "AVGO",
    "tsmc":                     "TSM",
    "taiwan semiconductor":     "TSM",
    "arm holdings":             "ARM",
    # Finance
    "jpmorgan":                 "JPM",
    "jp morgan":                "JPM",
    "goldman sachs":            "GS",
    "morgan stanley":           "MS",
    "bank of america":          "BAC",
    "wells fargo":              "WFC",
    "citigroup":                "C",
    "berkshire hathaway":       "BRK-B",
    "blackrock":                "BLK",
    "charles schwab":           "SCHW",
    "american express":         "AXP",
    "visa":                     "V",
    "mastercard":               "MA",
    # EV / Auto
    "tesla":                    "TSLA",
    "ford":                     "F",
    "general motors":           "GM",
    "toyota":                   "TM",
    "rivian":                   "RIVN",
    "lucid":                    "LCID",
    # Pharma / Biotech
    "pfizer":                   "PFE",
    "moderna":                  "MRNA",
    "johnson & johnson":        "JNJ",
    "johnson and johnson":      "JNJ",
    "eli lilly":                "LLY",
    "merck":                    "MRK",
    "abbvie":                   "ABBV",
    "bristol-myers":            "BMY",
    "bristol myers":            "BMY",
    "astrazeneca":              "AZN",
    "novartis":                 "NVS",
    "regeneron":                "REGN",
    "gilead":                   "GILD",
    # Energy
    "exxonmobil":               "XOM",
    "exxon":                    "XOM",
    "chevron":                  "CVX",
    "conocophillips":           "COP",
    "shell":                    "SHEL",
    # Retail / Consumer
    "walmart":                  "WMT",
    # "target" alone collides with "price target"; require the corporate form.
    "target corp":              "TGT",
    "target corporation":       "TGT",
    "home depot":               "HD",
    "costco":                   "COST",
    "starbucks":                "SBUX",
    "mcdonald's":               "MCD",
    "mcdonalds":                "MCD",
    "nike":                     "NKE",
    # Telecom
    "at&t":                     "T",
    "verizon":                  "VZ",
    "t-mobile":                 "TMUS",
    # Aerospace / Defense
    "boeing":                   "BA",
    "lockheed martin":          "LMT",
    "raytheon":                 "RTX",
    "northrop grumman":         "NOC",
    # Crypto-adjacent
    "coinbase":                 "COIN",
    "microstrategy":            "MSTR",
    "robinhood":                "HOOD",
    # Software / Cloud / SaaS
    "salesforce":               "CRM",
    "oracle":                   "ORCL",
    "ibm":                      "IBM",
    "adobe":                    "ADBE",
    "servicenow":               "NOW",
    "workday":                  "WDAY",
    "snowflake":                "SNOW",
    "palantir":                 "PLTR",
    "crowdstrike":              "CRWD",
    "datadog":                  "DDOG",
    # Payments / Fintech
    "paypal":                   "PYPL",
    # "block"/"square" alone collide with "block trade", "Palmer Square", etc.;
    # require the corporate form (cashtag $SQ still catches explicit mentions).
    "block inc":                "SQ",
    "block, inc":               "SQ",
    # Consumer / Entertainment
    "disney":                   "DIS",
    "comcast":                  "CMCSA",
    "spotify":                  "SPOT",
    "uber":                     "UBER",
    "lyft":                     "LYFT",
    "airbnb":                   "ABNB",
    "shopify":                  "SHOP",
    "zoom":                     "ZM",
    # Semiconductors / hardware
    "micron":                   "MU",
    "texas instruments":        "TXN",
    "applied materials":        "AMAT",
    "lam research":             "LRCX",
    "asml":                     "ASML",
    "marvell":                  "MRVL",
    "analog devices":           "ADI",
    "super micro":              "SMCI",
    "supermicro":               "SMCI",
    "western digital":          "WDC",
    "seagate":                  "STX",
    "hewlett packard":          "HPQ",
    "hp enterprise":            "HPE",
    "dell":                     "DELL",
    "cisco":                    "CSCO",
    "corning":                  "GLW",
    # Mega-cap / misc tech
    "qualtrics":                "XM",
    "atlassian":                "TEAM",
    "twilio":                   "TWLO",
    "cloudflare":               "NET",
    "mongodb":                  "MDB",
    "okta":                     "OKTA",
    "zscaler":                  "ZS",
    "fortinet":                 "FTNT",
    "palo alto networks":       "PANW",
    "intuit":                   "INTU",
    "autodesk":                 "ADSK",
    "roku":                     "ROKU",
    "pinterest":                "PINS",
    "snap inc":                 "SNAP",
    "doordash":                 "DASH",
    "instacart":                "CART",
    "unity software":           "U",
    "dropbox":                  "DBX",
    "asana":                    "ASAN",
    "samsara":                  "IOT",
    # Finance / banks / insurance
    "us bancorp":               "USB",
    "pnc financial":            "PNC",
    "truist":                   "TFC",
    "capital one":              "COF",
    "ally financial":           "ALLY",
    "synchrony":                "SYF",
    "fifth third":              "FITB",
    "regions financial":        "RF",
    "kkr":                      "KKR",
    "blackstone":               "BX",
    "apollo global":            "APO",
    "carlyle":                  "CG",
    "ares management":          "ARES",
    "marsh & mclennan":         "MMC",
    "chubb":                    "CB",
    "metlife":                  "MET",
    "prudential financial":     "PRU",
    "aig":                      "AIG",
    "travelers":                "TRV",
    "progressive":              "PGR",
    "allstate":                 "ALL",
    "intercontinental exchange": "ICE",
    "cme group":                "CME",
    "nasdaq inc":               "NDAQ",
    "fiserv":                   "FI",
    "fidelity national":        "FIS",
    "global payments":          "GPN",
    # Healthcare / pharma / biotech
    "unitedhealth":             "UNH",
    "cvs health":               "CVS",
    "cvs":                      "CVS",
    "cigna":                    "CI",
    "humana":                   "HUM",
    "elevance":                 "ELV",
    "centene":                  "CNC",
    "thermo fisher":            "TMO",
    "danaher":                  "DHR",
    "abbott":                   "ABT",
    "medtronic":                "MDT",
    "intuitive surgical":       "ISRG",
    "stryker":                  "SYK",
    "boston scientific":        "BSX",
    "becton dickinson":         "BDX",
    "amgen":                    "AMGN",
    "vertex pharmaceuticals":   "VRTX",
    "biogen":                   "BIIB",
    "novo nordisk":             "NVO",
    "sanofi":                   "SNY",
    "glaxosmithkline":          "GSK",
    "gsk":                      "GSK",
    "zoetis":                   "ZTS",
    "mckesson":                 "MCK",
    "cencora":                  "COR",
    "hca healthcare":           "HCA",
    "iqvia":                    "IQV",
    # Energy / industrials / materials
    "occidental":               "OXY",
    "schlumberger":             "SLB",
    "halliburton":              "HAL",
    "phillips 66":              "PSX",
    "valero":                   "VLO",
    "marathon petroleum":       "MPC",
    "kinder morgan":            "KMI",
    "williams companies":       "WMB",
    "nextera energy":           "NEE",
    "duke energy":              "DUK",
    "southern company":         "SO",
    "dominion energy":          "D",
    "caterpillar":              "CAT",
    "deere":                    "DE",
    "john deere":               "DE",
    "general electric":         "GE",
    "honeywell":                "HON",
    "3m":                       "MMM",
    "emerson electric":         "EMR",
    "illinois tool works":      "ITW",
    "eaton":                    "ETN",
    "parker hannifin":          "PH",
    "union pacific":            "UNP",
    "norfolk southern":         "NSC",
    "csx":                      "CSX",
    "ups":                      "UPS",
    "united parcel":            "UPS",
    "fedex":                    "FDX",
    "delta air lines":          "DAL",
    "united airlines":          "UAL",
    "american airlines":        "AAL",
    "southwest airlines":       "LUV",
    "general dynamics":         "GD",
    "l3harris":                 "LHX",
    "freeport-mcmoran":         "FCX",
    "freeport mcmoran":         "FCX",
    "nucor":                    "NUE",
    "dow inc":                  "DOW",
    "dupont":                   "DD",
    "linde":                    "LIN",
    "air products":             "APD",
    "sherwin-williams":         "SHW",
    "sherwin williams":         "SHW",
    # Consumer / retail / staples / media
    "procter & gamble":         "PG",
    "procter and gamble":       "PG",
    "coca-cola":                "KO",
    "coca cola":                "KO",
    "pepsico":                  "PEP",
    "pepsi":                    "PEP",
    "mondelez":                 "MDLZ",
    "kraft heinz":              "KHC",
    "general mills":            "GIS",
    "kellanova":                "K",
    "colgate":                  "CL",
    "kimberly-clark":           "KMB",
    "estee lauder":             "EL",
    "philip morris":            "PM",
    "altria":                   "MO",
    "lowe's":                   "LOW",
    "lowes":                    "LOW",
    "tj maxx":                  "TJX",
    "tjx":                      "TJX",
    "dollar general":           "DG",
    "dollar tree":              "DLTR",
    "kroger":                   "KR",
    "ulta beauty":              "ULTA",
    "lululemon":                "LULU",
    "chipotle":                 "CMG",
    "yum brands":               "YUM",
    "darden":                   "DRI",
    "marriott":                 "MAR",
    "hilton":                   "HLT",
    "booking holdings":         "BKNG",
    "expedia":                  "EXPE",
    "carnival":                 "CCL",
    "royal caribbean":          "RCL",
    "las vegas sands":          "LVS",
    "general motors company":   "GM",
    "stellantis":               "STLA",
    "ferrari":                  "RACE",
    "paramount":                "PARA",
    "warner bros":              "WBD",
    "warner brothers":          "WBD",
    "fox corporation":          "FOXA",
    "the trade desk":           "TTD",
    "trade desk":               "TTD",
    # Real estate / telecom / utilities
    "american tower":           "AMT",
    "prologis":                 "PLD",
    "crown castle":             "CCI",
    "equinix":                  "EQIX",
    "realty income":            "O",
    "simon property":           "SPG",
    "charter communications":   "CHTR",
    # Crypto / fintech / new economy
    "marathon digital":         "MARA",
    "riot platforms":           "RIOT",
    "sofi":                     "SOFI",
    "affirm":                   "AFRM",
    "carvana":                  "CVNA",
    "draftkings":               "DKNG",
    "roblox":                   "RBLX",
}

# ---------------------------------------------------------------------------
# Subsidiary / brand → listed-parent read-through
# ---------------------------------------------------------------------------
# News about a subsidiary or flagship brand moves the listed parent, but the
# parent's name often never appears ("YouTube pulls ads…" moves GOOGL). Names
# here must be unambiguous on a word boundary — no generic English collisions
# ("ring", "windows", "target") and no standalone-listed names (Instacart,
# Chewy have their own tickers). Merged into the extractor's name map.
_SUBSIDIARY_TICKERS: dict[str, str] = {
    # Meta
    "facebook":                 "META",
    "instagram":                "META",
    "whatsapp":                 "META",
    "oculus":                   "META",
    # Alphabet
    "youtube":                  "GOOGL",
    "waymo":                    "GOOGL",
    "deepmind":                 "GOOGL",
    "android":                  "GOOGL",
    "waze":                     "GOOGL",
    "fitbit":                   "GOOGL",
    # Amazon
    "aws":                      "AMZN",
    "whole foods":              "AMZN",
    "twitch":                   "AMZN",
    "audible":                  "AMZN",
    "zappos":                   "AMZN",
    # Microsoft
    "linkedin":                 "MSFT",
    "github":                   "MSFT",
    "xbox":                     "MSFT",
    "azure":                    "MSFT",
    "activision blizzard":      "MSFT",
    # Disney
    "espn":                     "DIS",
    "hulu":                     "DIS",
    "pixar":                    "DIS",
    "marvel studios":           "DIS",
    # Berkshire
    "geico":                    "BRK-B",
    "bnsf":                     "BRK-B",
    # Consumer parents
    "frito-lay":                "PEP",
    "gatorade":                 "PEP",
    "kfc":                      "YUM",
    "taco bell":                "YUM",
    "pizza hut":                "YUM",
    "sam's club":               "WMT",
    "sams club":                "WMT",
    "old navy":                 "GPS",
    "banana republic":          "GPS",
    # Payments / software
    "venmo":                    "PYPL",
    "cash app":                 "SQ",
    "slack":                    "CRM",
    "tableau":                  "CRM",
    "red hat":                  "IBM",
    "vmware":                   "AVGO",
}


# ---------------------------------------------------------------------------
# Extractor class
# ---------------------------------------------------------------------------

class TickerExtractor:
    """
    Extracts stock tickers from news title + description text.

    Parameters
    ----------
    extra_mappings:
        Optional additional {company_name: ticker} pairs to merge with the
        built-in dictionary.
    cik_map:
        Optional {cik:int -> ticker} from SEC's company_tickers.json. When given,
        a third pass resolves the 10-digit CIK embedded in EDGAR filing titles to
        a ticker — the path that lets the regulatory lane turn filings (which
        carry a CIK, not a symbol) into ranked candidates.
    valid_tickers:
        Optional set of real, tradable ticker symbols (e.g. SEC's ~10k-row
        universe from ``edgar_tickers.load_company_names`` ∪ ``CRYPTO_TICKERS``).
        When provided, the pattern pass (cashtags / (TICKER) / NYSE: TICKER) is
        gated against it so garbage cashtags like ``$YOLO`` / ``$MOON`` — the bulk
        of "fake tickers" on Reddit and Bluesky — never get tagged. The name-map
        and CIK passes are NOT gated: those resolve to real tickers by
        construction. Absent (or empty) → no gating, so a failed universe fetch
        degrades to today's behavior rather than dropping every ticker.
    """

    def __init__(
        self,
        extra_mappings: Optional[dict[str, str]] = None,
        cik_map: Optional[dict[int, str]] = None,
        include_subsidiaries: bool = True,
        valid_tickers: Optional[Iterable[str]] = None,
    ) -> None:
        self._mappings: dict[str, str] = dict(_COMPANY_TICKERS)
        if include_subsidiaries:
            self._mappings.update(_SUBSIDIARY_TICKERS)
        if extra_mappings:
            self._mappings.update({k.lower(): v for k, v in extra_mappings.items()})
        self._cik_map: dict[int, str] = cik_map or {}
        self._valid_tickers: Optional[frozenset[str]] = None
        self.set_valid_tickers(valid_tickers)

    def set_valid_tickers(self, valid_tickers: Optional[Iterable[str]]) -> None:
        """
        Install (or clear) the real-ticker universe used to gate the pattern pass.
        Lets callers construct the extractor synchronously and attach the universe
        later, once the async SEC fetch has resolved. Empty/None clears gating.
        """
        self._valid_tickers = (
            frozenset(t.upper() for t in valid_tickers) if valid_tickers else None
        )

    @property
    def valid_tickers(self) -> Optional[frozenset[str]]:
        """The installed real-ticker universe, or None when gating is off."""
        return self._valid_tickers

    def extract(
        self, title: str, description: str, *, validate: Optional[bool] = None
    ) -> tuple[str, ...]:
        """
        Return a sorted tuple of unique ticker symbols found in the text.

        validate:
            Controls the real-ticker gate on the pattern pass. ``None`` (default)
            gates iff a ``valid_tickers`` universe is installed. ``True`` forces
            gating on (no-op without a universe); ``False`` forces it off even
            when a universe is present — used for structured feeds, where we keep
            today's un-gated behavior and only validate social items.
        """
        text = f"{title} {description}"
        do_validate = (
            self._valid_tickers is not None
            if validate is None
            else bool(validate) and self._valid_tickers is not None
        )

        # Pass 1 — explicit patterns ($TICKER, (TICKER), NYSE: TICKER). These are
        # the only pass that can surface an arbitrary/fake symbol, so it's the one
        # the universe gate is applied to.
        pattern_hits: set[str] = set()
        for pattern in (_DOLLAR_PATTERN, _PAREN_PATTERN, _EXCHANGE_PATTERN):
            for m in pattern.finditer(text):
                pattern_hits.add(m.group(1).upper())
        if do_validate:
            pattern_hits &= self._valid_tickers  # drop unrecognised cashtags

        # Passes 2 & 3 resolve to real tickers by construction (curated name map,
        # authoritative SEC CIK map) — trusted, never gated.
        trusted: set[str] = set()

        # Pass 2 — company name lookup (word-boundary matched, case-insensitive)
        text_lower = text.lower()
        for name, ticker in self._mappings.items():
            if re.search(r'\b' + re.escape(name) + r'\b', text_lower):
                trusted.add(ticker)

        # Pass 3 — EDGAR CIK lookup (resolves filings to their ticker)
        if self._cik_map:
            for m in _CIK_PATTERN.finditer(text):
                ticker = self._cik_map.get(int(m.group(1)))
                if ticker:
                    trusted.add(ticker)

        found = (trusted | pattern_hits) - _FALSE_POSITIVES
        return tuple(sorted(found))


# ---------------------------------------------------------------------------
# Social helper
# ---------------------------------------------------------------------------

def extract_social_tickers(
    extractor: TickerExtractor,
    title: str,
    description: str,
    extra: Optional[dict] = None,
) -> tuple[str, ...]:
    """
    Ticker set for a *social* item: validated text extraction plus any
    platform-resolved symbols (StockTwits ``extra['ticker']`` / ``extra['symbols']``,
    with the ``.X`` crypto suffix stripped). Platform symbols are validated against
    the same universe as text cashtags when the extractor has one installed, so a
    StockTwits-only / delisted symbol can't slip through the side door either.

    Shared by the live social agent (``UnstructuredModule``) and the ticker
    backfill so both produce identical results for the same document.
    """
    found: set[str] = set(extractor.extract(title, description, validate=True))
    extra = extra or {}
    universe = extractor.valid_tickers

    candidates: list[str] = []
    wl_ticker = extra.get("ticker")
    if wl_ticker:
        candidates.append(str(wl_ticker))
    for sym in extra.get("symbols") or []:
        if sym:
            candidates.append(str(sym))

    for raw in candidates:
        sym = raw.replace(".X", "").upper().strip()
        if not sym:
            continue
        if universe is None or sym in universe:
            found.add(sym)

    return tuple(sorted(found))
