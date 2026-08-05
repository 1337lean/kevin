from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp

from kevin.openai_chat import Source

COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{symbol}-USD/spot"


class MarketDataError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CryptoAsset:
    symbol: str
    name: str


_CRYPTO_ASSETS = {
    "bitcoin": CryptoAsset("BTC", "Bitcoin"),
    "btc": CryptoAsset("BTC", "Bitcoin"),
    "ethereum": CryptoAsset("ETH", "Ethereum"),
    "ether": CryptoAsset("ETH", "Ethereum"),
    "eth": CryptoAsset("ETH", "Ethereum"),
    "solana": CryptoAsset("SOL", "Solana"),
    "sol": CryptoAsset("SOL", "Solana"),
    "dogecoin": CryptoAsset("DOGE", "Dogecoin"),
    "doge": CryptoAsset("DOGE", "Dogecoin"),
    "litecoin": CryptoAsset("LTC", "Litecoin"),
    "ltc": CryptoAsset("LTC", "Litecoin"),
}
_CRYPTO_NAME_RE = re.compile(
    rf"\b({'|'.join(sorted(_CRYPTO_ASSETS, key=len, reverse=True))})\b",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"\b(?:price|worth|value|trading\s+at|cost)\b", re.IGNORECASE)
_HISTORICAL_RE = re.compile(
    r"\b(?:yesterday|last\s+(?:week|month|year)|historical|history|in\s+(?:19|20)\d{2})\b",
    re.IGNORECASE,
)


def requested_crypto_spot_price(question: str) -> CryptoAsset | None:
    """Return a supported asset for a present-tense crypto price request."""
    match = _CRYPTO_NAME_RE.search(question)
    if not match or not _PRICE_RE.search(question) or _HISTORICAL_RE.search(question):
        return None
    return _CRYPTO_ASSETS[match.group(1).casefold()]


async def coinbase_spot_price(
    session: aiohttp.ClientSession,
    asset: CryptoAsset,
) -> tuple[str, list[Source]]:
    """Fetch a live USD spot quote directly from Coinbase's public endpoint."""
    url = COINBASE_SPOT_URL.format(symbol=asset.symbol)
    try:
        async with session.get(url, headers={"Accept": "application/json"}) as response:
            data: Any = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise MarketDataError("Coinbase price request failed") from exc

    if response.status >= 400 or not isinstance(data, dict):
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

    formatted = f"{amount:,.2f}"
    text = f"{asset.name} is currently **${formatted} USD** on Coinbase."
    source = Source(f"Coinbase {asset.symbol}-USD spot price", url)
    return text, [source]
