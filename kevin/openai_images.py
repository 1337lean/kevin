from __future__ import annotations

import base64
import binascii
from typing import Any

import aiohttp

from kevin.openai_chat import OpenAIAPIError

IMAGES_URL = "https://api.openai.com/v1/images/generations"
DOWNLOAD_TIMEOUT = 60


def extract_image(data: dict[str, Any]) -> bytes | str:
    """Return PNG bytes from b64_json, or a URL to download when the API gave one."""
    items = data.get("data")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise OpenAIAPIError(502, "OpenAI returned no image data")
    first = items[0]

    b64 = first.get("b64_json")
    if isinstance(b64, str) and b64:
        try:
            return base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise OpenAIAPIError(502, "OpenAI returned invalid image data") from exc

    url = str(first.get("url", "")).strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    raise OpenAIAPIError(502, "OpenAI returned no image data")


class OpenAIImageClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session is None:
            # Image generation regularly takes a minute or more.
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _download(self, url: str) -> bytes:
        assert self.session is not None
        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
            ) as response:
                if response.status >= 400:
                    raise OpenAIAPIError(response.status, "OpenAI image download failed")
                return await response.read()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise OpenAIAPIError(503, "OpenAI image download failed") from exc

    async def generate(
        self, prompt: str, *, size: str = "1024x1024", quality: str = "high"
    ) -> bytes:
        if not self.api_key:
            raise OpenAIAPIError(0, "OPENAI_API_KEY is not configured")
        if self.session is None:
            raise OpenAIAPIError(0, "OpenAI image client is not started")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality,
        }
        try:
            async with self.session.post(
                IMAGES_URL,
                headers=headers,
                json=payload,
            ) as response:
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise OpenAIAPIError(503, "OpenAI image request failed") from exc

        if response.status >= 400:
            api_error = data.get("error", {}) if isinstance(data, dict) else {}
            message = str(api_error.get("message", "OpenAI returned an error"))
            raise OpenAIAPIError(response.status, message)
        if not isinstance(data, dict):
            raise OpenAIAPIError(502, "OpenAI returned an invalid response")

        result = extract_image(data)
        if isinstance(result, str):
            return await self._download(result)
        return result
