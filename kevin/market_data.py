from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import aiohttp

from kevin.openai_chat import Source

log = logging.getLogger(__name__)

COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{symbol}-USD/spot"
COINBASE_CURRENCIES_URL = "https://api.exchange.coinbase.com/currencies"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
YAHOO_QUOTE_PAGE = "https://finance.yahoo.com/quote/{symbol}"

# aiohttp's default User-Agent is rejected by Yahoo with HTTP 429; any other value
# is accepted. Keep this honest rather than impersonating a browser.
YAHOO_HEADERS = {"User-Agent": "KevinBot/1.0", "Accept": "application/json"}

CRYPTO_INDEX_TTL_SECONDS = 6 * 60 * 60
TRADEABLE_QUOTE_TYPES = frozenset({"EQUITY", "ETF", "INDEX", "MUTUALFUND"})
STALE_QUOTE_SECONDS = 20 * 60
MAX_SYMBOL_LOOKUPS = 3

QuoteKind = Literal["crypto", "stock"]


class MarketDataError(RuntimeError):
    """Raised when an asset was identified but no verified price could be fetched."""


@dataclass(frozen=True, slots=True)
class Asset:
    symbol: str
    name: str


@dataclass(frozen=True, slots=True)
class Quote:
    name: str
    symbol: str
    price: Decimal
    currency: str
    venue: str
    url: str
    change_pct: Decimal | None = None
    as_of: int | None = None


@dataclass(frozen=True, slots=True)
class PriceRequest:
    """A detected present-tense price question and the terms worth resolving."""

    question: str
    subject: tuple[str, ...]
    cashtags: tuple[str, ...]
    upper_tokens: frozenset[str]
    prefer: QuoteKind | None


# --------------------------------------------------------------------------
# Question parsing
# --------------------------------------------------------------------------

_PRICE_RE = re.compile(
    r"\b(?:price|prices|worth|value|valued|trading\s+at|cost|costs|quote|quotes)\b",
    re.IGNORECASE,
)
_HOW_MUCH_RE = re.compile(r"\bhow\s+much\s+(?:is|are|for)\b", re.IGNORECASE)
# "what's bitcoin at", "whats AAPL at right now"
_TRADING_AT_RE = re.compile(
    r"\bat\s*(?:right\s+now|now|rn|today|currently)?\s*[?!.]*$", re.IGNORECASE
)
_HISTORICAL_RE = re.compile(
    r"\b(?:yesterday|last\s+(?:week|month|year)|historical|history|ago|back\s+in|"
    r"in\s+(?:19|20)\d{2}|will|forecast|predict\w*|going\s+to\s+be|"
    r"next\s+(?:week|month|year)|end\s+of\s+(?:the\s+)?year)\b",
    re.IGNORECASE,
)
_CRYPTO_HINT_RE = re.compile(
    r"\b(?:crypto|cryptos|coin|coins|token|tokens|blockchain|onchain)\b", re.IGNORECASE
)
_STOCK_HINT_RE = re.compile(
    r"\b(?:stock|stocks|share|shares|equity|equities|etf|etfs|ticker|nasdaq|nyse)\b",
    re.IGNORECASE,
)
_CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9.\-]{0,9})\b")
_UPPER_TOKEN_RE = re.compile(r"\b([A-Z][A-Z0-9.\-]{0,9})\b")

_SUBJECT_STOPWORDS = frozenset(
    """
    a an the what whats is are s how much many any about
    price prices priced worth value valued cost costs costing quote quotes
    trading trade trades at of for in on to from by with
    right now currently current today tonight latest live real time
    please hey hi yo kevin k tell me us you your i im
    do does did go going doing look check get give say know think
    one 1 per each unit units usd dollar dollars usdollar
    and or but its it this that these those there here
    coin coins crypto cryptos token tokens stock stocks share shares
    etf etfs ticker equity equities market markets nasdaq nyse
    """.split()
)

# Coinbase lists tokens whose ticker or name is an everyday English word. For these
# we only accept a crypto match when the user gave an explicit signal (ALL CAPS, a
# $cashtag, or a "crypto"/"coin"/"token" hint), so "what's the price of a home" does
# not get answered with the HOME token.
_NEEDS_EXPLICIT_SIGNAL = frozenset(
    {
        "home", "prime", "safe", "trust", "index", "check", "bill", "time",
        "cap", "high", "red", "well", "wet", "cow", "fox", "tree", "honey",
        "gas", "oil", "gold", "silver", "food", "rent", "land", "water",
    }
)

# Tickers where the equity is overwhelmingly the intended reading even though a
# Coinbase asset shares the symbol.
_EQUITY_FIRST_TICKERS = frozenset({"META"})


def _normalise_tokens(question: str) -> list[str]:
    cleaned = re.sub(r"[^\w\s.\-]", " ", question.replace("$", " "))
    return [token.strip(".-").casefold() for token in cleaned.split() if token.strip(".-")]


def requested_price_lookup(question: str) -> PriceRequest | None:
    """Detect a live price question and collect the terms worth resolving.

    Returns None for anything that is not a present-tense price request, so the
    caller falls through to the normal web-search path.
    """
    if not (
        _PRICE_RE.search(question)
        or _HOW_MUCH_RE.search(question)
        or _TRADING_AT_RE.search(question.strip())
    ):
        return None
    if _HISTORICAL_RE.search(question):
        return None

    subject = tuple(
        token for token in _normalise_tokens(question) if token not in _SUBJECT_STOPWORDS
    )
    if not subject:
        return None

    cashtags = tuple(match.group(1).upper() for match in _CASHTAG_RE.finditer(question))
    upper_tokens = frozenset(
        match.group(1) for match in _UPPER_TOKEN_RE.finditer(question)
    )

    prefer: QuoteKind | None = None
    if _STOCK_HINT_RE.search(question):
        prefer = "stock"
    elif _CRYPTO_HINT_RE.search(question):
        prefer = "crypto"

    return PriceRequest(
        question=question,
        subject=subject,
        cashtags=cashtags,
        upper_tokens=upper_tokens,
        prefer=prefer,
    )


# --------------------------------------------------------------------------
# Crypto: Coinbase
# --------------------------------------------------------------------------

_SEED_CRYPTO: dict[str, Asset] = {
    "btc": Asset("BTC", "Bitcoin"),
    "bitcoin": Asset("BTC", "Bitcoin"),
    "eth": Asset("ETH", "Ethereum"),
    "ether": Asset("ETH", "Ethereum"),
    "ethereum": Asset("ETH", "Ethereum"),
    "sol": Asset("SOL", "Solana"),
    "solana": Asset("SOL", "Solana"),
    "xrp": Asset("XRP", "XRP"),
    "ripple": Asset("XRP", "XRP"),
    "doge": Asset("DOGE", "Dogecoin"),
    "dogecoin": Asset("DOGE", "Dogecoin"),
    "ada": Asset("ADA", "Cardano"),
    "cardano": Asset("ADA", "Cardano"),
    "avax": Asset("AVAX", "Avalanche"),
    "avalanche": Asset("AVAX", "Avalanche"),
    "ltc": Asset("LTC", "Litecoin"),
    "litecoin": Asset("LTC", "Litecoin"),
    "link": Asset("LINK", "Chainlink"),
    "chainlink": Asset("LINK", "Chainlink"),
    "shib": Asset("SHIB", "Shiba Inu"),
    "shiba inu": Asset("SHIB", "Shiba Inu"),
    "pepe": Asset("PEPE", "Pepe"),
    "usdc": Asset("USDC", "USDC"),
}

_crypto_index: dict[str, Asset] = dict(_SEED_CRYPTO)
_crypto_index_fetched_at = 0.0
_crypto_index_lock = asyncio.Lock()


def _reset_crypto_index_for_tests() -> None:
    global _crypto_index, _crypto_index_fetched_at
    _crypto_index = dict(_SEED_CRYPTO)
    _crypto_index_fetched_at = 0.0


async def crypto_index(session: aiohttp.ClientSession) -> dict[str, Asset]:
    """Return a cached ticker/name -> asset index of Coinbase's live currencies.

    Falls back to the seeded majors if the listing cannot be refreshed, so common
    coins keep working during a Coinbase outage.
    """
    global _crypto_index, _crypto_index_fetched_at

    if time.monotonic() - _crypto_index_fetched_at < CRYPTO_INDEX_TTL_SECONDS:
        return _crypto_index

    async with _crypto_index_lock:
        if time.monotonic() - _crypto_index_fetched_at < CRYPTO_INDEX_TTL_SECONDS:
            return _crypto_index
        try:
            async with session.get(
                COINBASE_CURRENCIES_URL, headers={"Accept": "application/json"}
            ) as response:
                if response.status >= 400:
                    raise MarketDataError("Coinbase currency listing failed")
                listing: Any = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError, MarketDataError) as exc:
            log.warning("Could not refresh the Coinbase currency index: %s", exc)
            _crypto_index_fetched_at = time.monotonic()
            return _crypto_index

        index = dict(_SEED_CRYPTO)
        if isinstance(listing, list):
            for entry in listing:
                if not isinstance(entry, dict) or entry.get("status") != "online":
                    continue
                details = entry.get("details")
                if not isinstance(details, dict) or details.get("type") != "crypto":
                    continue
                symbol = str(entry.get("id", "")).upper()
                name = str(entry.get("name", "")).strip() or symbol
                if not symbol:
                    continue
                asset = Asset(symbol, name)
                index.setdefault(symbol.casefold(), asset)
                index.setdefault(name.casefold(), asset)

        _crypto_index = index
        _crypto_index_fetched_at = time.monotonic()
        return _crypto_index


def _has_explicit_signal(term: str, request: PriceRequest) -> bool:
    upper = term.upper()
    return (
        upper in request.cashtags
        or upper in request.upper_tokens
        or request.prefer is not None
    )


def match_crypto(request: PriceRequest, index: dict[str, Asset]) -> Asset | None:
    """Find the longest ticker/name in the request that Coinbase quotes."""
    candidates: list[str] = [tag.casefold() for tag in request.cashtags]
    tokens = request.subject
    for size in range(min(4, len(tokens)), 0, -1):
        for start in range(len(tokens) - size + 1):
            candidates.append(" ".join(tokens[start : start + size]))

    for term in candidates:
        asset = index.get(term)
        if asset is None:
            continue
        if asset.symbol in _EQUITY_FIRST_TICKERS and request.prefer != "crypto":
            continue
        ambiguous = len(term) <= 2 or term in _NEEDS_EXPLICIT_SIGNAL
        if ambiguous and not _has_explicit_signal(term, request):
            continue
        return asset
    return None


async def coinbase_spot_price(session: aiohttp.ClientSession, asset: Asset) -> Quote:
    """Fetch a live USD spot quote directly from Coinbase's public endpoint."""
    url = COINBASE_SPOT_URL.format(symbol=asset.symbol)
    try:
        async with session.get(url, headers={"Accept": "application/json"}) as response:
            status = response.status
            data: Any = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise MarketDataError("Coinbase price request failed") from exc

    if status >= 400 or not isinstance(data, dict):
        raise MarketDataError("Coinbase returned an invalid price response")
    raw = data.get("data")
    if not isinstance(raw, dict) or raw.get("currency") != "USD":
        raise MarketDataError("Coinbase returned an incomplete price response")
    try:
        amount = Decimal(str(raw.get("amount", "")))
    except InvalidOperation as exc:
        raise MarketDataError("Coinbase returned an invalid price") from exc
    if not amount.is_finite() or amount <= 0:
        raise MarketDataError("Coinbase returned an invalid price")

    return Quote(
        name=asset.name,
        symbol=asset.symbol,
        price=amount,
        currency="USD",
        venue="Coinbase",
        url=url,
    )


# --------------------------------------------------------------------------
# Stocks, ETFs and indices: Yahoo Finance
# --------------------------------------------------------------------------


async def _yahoo_search_symbol(session: aiohttp.ClientSession, term: str) -> str | None:
    """Resolve a name or ticker to a Yahoo symbol, rejecting weak matches.

    Yahoo always returns something, so an unmatched query like "a house" resolves to
    an unrelated foreign listing. Requiring the result to echo the search term - as
    an exact ticker, or as a whole word in the company name on a primary US listing
    - keeps those out.
    """
    params = {"q": term, "quotesCount": "6", "newsCount": "0"}
    try:
        async with session.get(
            YAHOO_SEARCH_URL, params=params, headers=YAHOO_HEADERS
        ) as response:
            if response.status >= 400:
                return None
            data: Any = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None

    if not isinstance(data, dict):
        return None
    quotes = [
        quote
        for quote in (data.get("quotes") or [])
        if isinstance(quote, dict)
        and quote.get("quoteType") in TRADEABLE_QUOTE_TYPES
        and str(quote.get("symbol", "")).strip()
    ]

    wanted = term.casefold()
    for quote in quotes:
        if str(quote["symbol"]).casefold() == wanted:
            return str(quote["symbol"])

    # Anchor to the start of the company name: "apple" should find Apple Inc.,
    # but "house" must not find Full House Resorts.
    name_re = re.compile(rf"{re.escape(wanted)}\b")
    for quote in quotes:
        symbol = str(quote["symbol"])
        if "." in symbol:  # foreign secondary listing
            continue
        names = (quote.get("shortname") or "", quote.get("longname") or "")
        if any(name_re.match(str(name).casefold()) for name in names):
            return symbol
    return None


async def yahoo_quote(session: aiohttp.ClientSession, symbol: str) -> Quote:
    """Fetch a live quote for a stock, ETF or index from Yahoo's chart endpoint."""
    url = YAHOO_CHART_URL.format(symbol=symbol)
    try:
        async with session.get(
            url, params={"interval": "1d", "range": "1d"}, headers=YAHOO_HEADERS
        ) as response:
            status = response.status
            data: Any = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise MarketDataError("Yahoo Finance price request failed") from exc

    if status >= 400 or not isinstance(data, dict):
        raise MarketDataError("Yahoo Finance returned an invalid price response")
    chart = data.get("chart")
    results = chart.get("result") if isinstance(chart, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise MarketDataError("Yahoo Finance returned no price data")
    meta = results[0].get("meta")
    if not isinstance(meta, dict):
        raise MarketDataError("Yahoo Finance returned no price data")

    try:
        price = Decimal(str(meta.get("regularMarketPrice", "")))
    except InvalidOperation as exc:
        raise MarketDataError("Yahoo Finance returned an invalid price") from exc
    if not price.is_finite() or price <= 0:
        raise MarketDataError("Yahoo Finance returned an invalid price")

    change_pct: Decimal | None = None
    try:
        previous = Decimal(str(meta.get("chartPreviousClose", "")))
        if previous.is_finite() and previous > 0:
            change_pct = (price - previous) / previous * 100
    except InvalidOperation:
        change_pct = None

    resolved = str(meta.get("symbol") or symbol).upper()
    as_of = meta.get("regularMarketTime")
    return Quote(
        name=str(meta.get("longName") or meta.get("shortName") or resolved),
        symbol=resolved,
        price=price,
        currency=str(meta.get("currency") or "USD").upper(),
        venue="Yahoo Finance",
        url=YAHOO_QUOTE_PAGE.format(symbol=resolved),
        change_pct=change_pct,
        as_of=as_of if isinstance(as_of, int) else None,
    )


async def match_stock(
    session: aiohttp.ClientSession, request: PriceRequest
) -> str | None:
    terms: list[str] = list(request.cashtags)
    if request.subject:
        terms.append(" ".join(request.subject))
    terms.extend(token for token in request.subject if len(token) >= 2)

    seen: set[str] = set()
    for term in terms:
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > MAX_SYMBOL_LOOKUPS:  # keep one question to a few round trips
            return None
        symbol = await _yahoo_search_symbol(session, term)
        if symbol:
            return symbol
    return None


# --------------------------------------------------------------------------
# Formatting and the public entry point
# --------------------------------------------------------------------------


def format_amount(amount: Decimal) -> str:
    """Format a price without rounding sub-cent assets away to 0.00."""
    if amount >= 1:
        return f"{amount:,.2f}"
    if amount >= Decimal("0.01"):
        return f"{amount:,.4f}"
    # Keep four significant digits for micro-cap tokens such as PEPE.
    places = min(-amount.adjusted() + 3, 18)
    quantised = amount.quantize(Decimal(1).scaleb(-places))
    return f"{quantised:,.{places}f}"


def format_quote(quote: Quote) -> tuple[str, list[Source]]:
    """Render a fetched quote plus the exact endpoint the number came from."""
    label = quote.name if quote.name.upper() == quote.symbol else f"{quote.name} ({quote.symbol})"
    price = f"**${format_amount(quote.price)}**" if quote.currency == "USD" else (
        f"**{format_amount(quote.price)} {quote.currency}**"
    )
    text = f"{label} is {price} on {quote.venue}"
    if quote.change_pct is not None:
        direction = "+" if quote.change_pct >= 0 else ""
        text += f", {direction}{quote.change_pct:.2f}% on the day"
    text += "."
    # Outside market hours the last trade can be days old; say so rather than
    # letting a stale close read as a live quote.
    if quote.as_of is not None and time.time() - quote.as_of > STALE_QUOTE_SECONDS:
        text += f" Last trade {time.strftime('%b %d %H:%M UTC', time.gmtime(quote.as_of))}."

    title = f"{quote.venue} {quote.symbol} quote"
    return text, [Source(title, quote.url)]


async def live_price_reply(
    session: aiohttp.ClientSession, request: PriceRequest
) -> tuple[str, list[Source]] | None:
    """Resolve a price request against Coinbase and Yahoo.

    Returns None when neither source confidently recognises the asset, so the
    caller can fall back to a normal sourced web search. Raises MarketDataError
    when the asset was recognised but its live price could not be fetched.
    """
    index = await crypto_index(session)
    crypto = match_crypto(request, index)

    order: list[QuoteKind] = ["crypto", "stock"]
    if request.prefer == "stock" or (crypto is None and request.prefer != "crypto"):
        order = ["stock", "crypto"]

    for kind in order:
        if kind == "crypto":
            if crypto is not None:
                return format_quote(await coinbase_spot_price(session, crypto))
            continue
        symbol = await match_stock(session, request)
        if symbol is not None:
            return format_quote(await yahoo_quote(session, symbol))
    return None
