from kevin.market_data import (
    COINBASE_SPOT_URL,
    CryptoAsset,
    coinbase_spot_price,
    requested_crypto_spot_price,
)
from kevin.openai_chat import Source


class _FakeCoinbaseResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        return {"data": {"amount": "1910.175", "currency": "USD"}}


class _FakeCoinbaseSession:
    def __init__(self) -> None:
        self.url = ""

    def get(self, url, **_kwargs):
        self.url = url
        return _FakeCoinbaseResponse()


def test_requested_crypto_spot_price_detects_current_not_historical_queries() -> None:
    assert requested_crypto_spot_price("what's ethereum's current price") == CryptoAsset(
        "ETH", "Ethereum"
    )
    assert requested_crypto_spot_price("how much is BTC worth?") == CryptoAsset("BTC", "Bitcoin")
    assert requested_crypto_spot_price("what was ETH worth in 2021?") is None
    assert requested_crypto_spot_price("explain Ethereum staking") is None


async def test_coinbase_spot_price_returns_a_verified_quote_and_exact_source() -> None:
    session = _FakeCoinbaseSession()

    text, sources = await coinbase_spot_price(session, CryptoAsset("ETH", "Ethereum"))

    expected_url = COINBASE_SPOT_URL.format(symbol="ETH")
    assert session.url == expected_url
    assert text == "Ethereum is currently **$1,910.18 USD** on Coinbase."
    assert sources == [Source("Coinbase ETH-USD spot price", expected_url)]
