import pytest

from kevin.market_data import (
    COINBASE_SPOT_URL,
    YAHOO_QUOTE_PAGE,
    Asset,
    MarketDataError,
    _reset_crypto_index_for_tests,
    coinbase_spot_price,
    format_amount,
    format_quote,
    live_price_reply,
    match_crypto,
    requested_price_lookup,
    yahoo_quote,
)
from kevin.openai_chat import Source


@pytest.fixture(autouse=True)
def _fresh_crypto_index():
    """The Coinbase index is cached module-wide; keep tests order-independent."""
    _reset_crypto_index_for_tests()
    yield
    _reset_crypto_index_for_tests()

INDEX = {
    "btc": Asset("BTC", "Bitcoin"),
    "bitcoin": Asset("BTC", "Bitcoin"),
    "eth": Asset("ETH", "Ethereum"),
    "ethereum": Asset("ETH", "Ethereum"),
    "pepe": Asset("PEPE", "Pepe"),
    "shiba inu": Asset("SHIB", "Shiba Inu"),
    "meta": Asset("META", "Metadao"),
    "home": Asset("HOME", "Defi App"),
    "t": Asset("T", "Threshold"),
}


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        return self._payload


class _FakeSession:
    """Routes requests by URL so one session can serve both providers."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("params")))
        for fragment, payload in self.routes.items():
            if fragment in url:
                status = 404 if payload is None else 200
                return _FakeResponse(payload, status=status)
        raise AssertionError(f"unexpected request to {url}")


def _chart(price, previous=None, name="Apple Inc.", symbol="AAPL", currency="USD"):
    meta = {
        "symbol": symbol,
        "currency": currency,
        "regularMarketPrice": price,
        "longName": name,
    }
    if previous is not None:
        meta["chartPreviousClose"] = previous
    return {"chart": {"result": [{"meta": meta}]}}


def _search(symbol, quote_type="EQUITY", shortname="Apple Inc."):
    return {"quotes": [{"symbol": symbol, "quoteType": quote_type, "shortname": shortname}]}


# --------------------------------------------------------------------------
# Question detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "what's ethereum's current price",
        "how much is BTC worth?",
        "whats bitcoin at right now",
        "tesla stock price",
        "price of $NVDA",
    ],
)
def test_requested_price_lookup_detects_live_price_questions(question) -> None:
    assert requested_price_lookup(question) is not None


@pytest.mark.parametrize(
    "question",
    [
        "what was ETH worth in 2021?",
        "explain Ethereum staking",
        "what will bitcoin be worth next year",
        "how much was it worth a year ago",
    ],
)
def test_requested_price_lookup_ignores_history_and_forecasts(question) -> None:
    assert requested_price_lookup(question) is None


def test_match_crypto_prefers_the_longest_name() -> None:
    request = requested_price_lookup("how much is shiba inu worth")
    assert match_crypto(request, INDEX) == Asset("SHIB", "Shiba Inu")


def test_match_crypto_skips_everyday_words_without_an_explicit_signal() -> None:
    vague = requested_price_lookup("what's the price of a home")
    assert match_crypto(vague, INDEX) is None

    explicit = requested_price_lookup("what's the price of the HOME token")
    assert match_crypto(explicit, INDEX) == Asset("HOME", "Defi App")


def test_match_crypto_skips_short_tickers_without_an_explicit_signal() -> None:
    assert match_crypto(requested_price_lookup("what's the price of t"), INDEX) is None
    assert match_crypto(requested_price_lookup("price of $T"), INDEX) == Asset("T", "Threshold")


def test_match_crypto_defers_to_the_equity_for_a_shared_ticker() -> None:
    assert match_crypto(requested_price_lookup("what's the price of META"), INDEX) is None
    assert match_crypto(requested_price_lookup("whats meta coin worth"), INDEX) == Asset(
        "META", "Metadao"
    )


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def test_format_amount_keeps_significant_digits_for_sub_cent_assets() -> None:
    from decimal import Decimal

    assert format_amount(Decimal("68574.879")) == "68,574.88"
    assert format_amount(Decimal("1.065")) == "1.06"
    assert format_amount(Decimal("0.0695")) == "0.0695"
    # The old two-decimal format rendered this as 0.00.
    assert format_amount(Decimal("0.000002715")) == "0.000002715"


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


async def test_coinbase_spot_price_returns_a_quote_citing_the_exact_endpoint() -> None:
    session = _FakeSession({"api.coinbase.com": {"data": {"amount": "1910.175", "currency": "USD"}}})

    quote = await coinbase_spot_price(session, Asset("ETH", "Ethereum"))
    text, sources = format_quote(quote)

    expected_url = COINBASE_SPOT_URL.format(symbol="ETH")
    assert session.calls[0][0] == expected_url
    assert text == "Ethereum (ETH) is **$1,910.18** on Coinbase."
    assert sources == [Source("Coinbase ETH quote", expected_url)]


async def test_coinbase_rejects_a_non_usd_or_empty_payload() -> None:
    session = _FakeSession({"api.coinbase.com": {"data": {"amount": "1", "currency": "EUR"}}})
    with pytest.raises(MarketDataError):
        await coinbase_spot_price(session, Asset("ETH", "Ethereum"))


async def test_yahoo_quote_reports_price_and_daily_change() -> None:
    session = _FakeSession({"query1.finance.yahoo.com": _chart(315.0, previous=300.0)})

    quote = await yahoo_quote(session, "AAPL")
    text, sources = format_quote(quote)

    assert text == "Apple Inc. (AAPL) is **$315.00** on Yahoo Finance, +5.00% on the day."
    assert sources == [Source("Yahoo Finance AAPL quote", YAHOO_QUOTE_PAGE.format(symbol="AAPL"))]


async def test_yahoo_quote_uses_the_listing_currency() -> None:
    session = _FakeSession(
        {"query1.finance.yahoo.com": _chart(12.5, name="Foo PLC", symbol="FOO", currency="GBP")}
    )

    text, _sources = format_quote(await yahoo_quote(session, "FOO"))

    assert text == "Foo PLC (FOO) is **12.50 GBP** on Yahoo Finance."


async def test_yahoo_quote_raises_when_the_symbol_is_unknown() -> None:
    session = _FakeSession({"query1.finance.yahoo.com": {"chart": {"result": None}}})
    with pytest.raises(MarketDataError):
        await yahoo_quote(session, "ZZZZ")


# --------------------------------------------------------------------------
# End-to-end routing
# --------------------------------------------------------------------------


async def test_live_price_reply_routes_a_coin_to_coinbase() -> None:
    session = _FakeSession(
        {
            "api.exchange.coinbase.com": [
                {"id": "PEPE", "name": "Pepe", "status": "online", "details": {"type": "crypto"}}
            ],
            "api.coinbase.com/v2": {"data": {"amount": "0.000002715", "currency": "USD"}},
        }
    )

    text, sources = await live_price_reply(session, requested_price_lookup("price of pepe"))

    assert text == "Pepe is **$0.000002715** on Coinbase."
    assert sources[0].url == COINBASE_SPOT_URL.format(symbol="PEPE")


async def test_live_price_reply_routes_a_company_name_to_yahoo() -> None:
    session = _FakeSession(
        {
            "api.exchange.coinbase.com": [],
            "/v1/finance/search": _search("AAPL"),
            "/v8/finance/chart": _chart(315.0, previous=300.0),
        }
    )

    text, sources = await live_price_reply(session, requested_price_lookup("apple stock price"))

    assert "Apple Inc. (AAPL) is **$315.00**" in text
    assert sources[0].url == YAHOO_QUOTE_PAGE.format(symbol="AAPL")


async def test_live_price_reply_returns_none_for_an_unrecognised_asset() -> None:
    # Yahoo answers every search, so a weak match must be rejected rather than
    # reported: "a house" should not resolve to Full House Resorts.
    session = _FakeSession(
        {
            "api.exchange.coinbase.com": [],
            "/v1/finance/search": _search("FLL", shortname="Full House Resorts, Inc."),
        }
    )

    assert await live_price_reply(session, requested_price_lookup("price of a house")) is None
