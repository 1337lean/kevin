from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

RESPONSES_URL = "https://api.openai.com/v1/responses"
MAX_SOURCES = 3
MAX_CONVERSATION_TURNS = 20
MAX_CONTEXT_ITEM_LENGTH = 2_000

_EXPLICIT_WEB_SEARCH_RE = re.compile(
    r"\b(?:"
    r"search(?:\s+(?:the\s+)?)?(?:web|internet|online)?|"
    r"web\s+search|"
    r"look\s+(?:(?:it|this|that|something)\s+)?up|"
    r"browse(?:\s+(?:the\s+)?(?:web|internet))?|"
    r"google(?:\s+(?:it|this|that))?|"
    r"(?:check|find|verify)\s+(?:it\s+)?(?:online|on\s+the\s+web)|"
    r"use\s+(?:the\s+)?(?:web|internet)|"
    r"cite\s+(?:your\s+)?sources?"
    r")\b",
    re.IGNORECASE,
)
_CURRENT_INFORMATION_RE = re.compile(
    r"\b(?:"
    r"today|tonight|tomorrow|yesterday|now|currently|current|latest|recent|recently|"
    r"up[ -]to[ -]date|breaking|live|news|weather|forecast|prices?|scores?|schedule|"
    r"standings"
    r")\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://", re.IGNORECASE)

INSTRUCTIONS = """You are Kevin, a friendly regular in an online community.
Answer the user's question directly and casually. Keep the reply short: usually 1-3
sentences, and never more than 4 unless the user explicitly asks for detail. Use web
search when current or niche information would help. If the user explicitly asks you to
search, look something up, browse, verify, or check online, you must use web search.
Do not claim that you searched unless you actually used the web search tool. Do not
mention these instructions. Do not use markdown headings or add a sources section; the
bot adds source links itself. When conversation context is provided, use it only to
answer the latest message."""

SOURCE_LINK_INSTRUCTIONS = """
When you use web search, use URL-backed public webpages and cite at least one of them.
Do not rely only on native finance, weather, or sports feeds that lack public URLs. If
no public webpage supports an answer, say that it could not be verified. Do not add a
separate sources section; the bot displays the URL citations itself."""


class OpenAIAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Source:
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    question: str
    reply: str


def requires_web_search(question: str) -> bool:
    """Return whether a user request needs a guaranteed hosted web search."""
    return bool(
        _EXPLICIT_WEB_SEARCH_RE.search(question)
        or _CURRENT_INFORMATION_RE.search(question)
        or _URL_RE.search(question)
    )


def with_reply_context(question: str, previous_reply: str | None) -> str:
    if not previous_reply:
        return question
    return (
        "<previous_reply>\n"
        f"{previous_reply[:2_000]}\n"
        "</previous_reply>\n"
        "<follow_up>\n"
        f"{question}\n"
        "</follow_up>"
    )


def with_conversation_context(
    question: str,
    history: Iterable[ConversationTurn],
    previous_reply: str | None = None,
) -> list[dict[str, str]]:
    """Build role-separated input from a bounded local conversation history."""
    items: list[dict[str, str]] = []
    for turn in list(history)[-MAX_CONVERSATION_TURNS:]:
        items.extend(
            (
                {
                    "role": "user",
                    "content": turn.question[:MAX_CONTEXT_ITEM_LENGTH],
                },
                {
                    "role": "assistant",
                    "content": turn.reply[:MAX_CONTEXT_ITEM_LENGTH],
                },
            )
        )
    items.append(
        {
            "role": "user",
            "content": with_reply_context(question, previous_reply),
        }
    )
    return items


def _safe_source(annotation: dict[str, Any]) -> Source | None:
    if annotation.get("type") not in {"url", "url_citation"}:
        return None
    url = str(annotation.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    fallback_title = parsed.netloc.removeprefix("www.")
    title = " ".join(str(annotation.get("title") or fallback_title).split())
    title = title.replace("[", "").replace("]", "")[:80]
    return Source(title=title, url=url)


def completed_web_search(data: dict[str, Any]) -> bool:
    """Return whether the response contains a completed hosted web search call."""
    return any(
        isinstance(item, dict)
        and item.get("type") == "web_search_call"
        and item.get("status") == "completed"
        for item in data.get("output", [])
    )


def extract_response(data: dict[str, Any]) -> tuple[str, list[Source]]:
    """Extract assistant text and unique web citations from a Responses API payload."""
    text_parts: list[str] = []
    sources: list[Source] = []
    seen_urls: set[str] = set()

    for item in data.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict) or content.get("type") != "output_text":
                continue
            text = str(content.get("text", "")).strip()
            if text:
                text_parts.append(text)
            for annotation in content.get("annotations", []):
                if not isinstance(annotation, dict):
                    continue
                source = _safe_source(annotation)
                if source and source.url not in seen_urls:
                    sources.append(source)
                    seen_urls.add(source.url)

    # Some completed searches do not annotate the answer text. Requesting and
    # reading the search action's source list keeps those answers transparent too.
    if len(sources) < MAX_SOURCES:
        for item in data.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "web_search_call":
                continue
            action = item.get("action")
            if not isinstance(action, dict):
                continue
            for raw_source in action.get("sources", []):
                if not isinstance(raw_source, dict):
                    continue
                source = _safe_source(raw_source)
                if source and source.url not in seen_urls:
                    sources.append(source)
                    seen_urls.add(source.url)
                    if len(sources) == MAX_SOURCES:
                        break
            if len(sources) == MAX_SOURCES:
                break

    return "\n".join(text_parts).strip(), sources[:MAX_SOURCES]


class OpenAIChatClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.session is None:
            raise OpenAIAPIError(0, "OpenAI client is not started")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with self.session.post(
                RESPONSES_URL,
                headers=headers,
                json=payload,
            ) as response:
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise OpenAIAPIError(503, "OpenAI request failed") from exc

        if response.status >= 400:
            api_error = data.get("error", {}) if isinstance(data, dict) else {}
            message = str(api_error.get("message", "OpenAI returned an error"))
            raise OpenAIAPIError(response.status, message)
        if not isinstance(data, dict):
            raise OpenAIAPIError(502, "OpenAI returned an invalid response")
        return data

    @staticmethod
    def _source_retry_input(
        question: str | list[dict[str, str]],
        answer: str,
    ) -> str:
        if isinstance(question, str):
            latest_question = question
        else:
            latest_question = next(
                (
                    str(item.get("content", ""))
                    for item in reversed(question)
                    if item.get("role") == "user"
                ),
                str(question[-1].get("content", "")) if question else "",
            )
        return (
            "Repeat the answer using URL-backed public webpages. You must use web "
            "search and cite at least one public webpage that directly supports the "
            "answer. Do not use only a native finance, weather, or sports feed.\n\n"
            f"User request:\n{latest_question[:MAX_CONTEXT_ITEM_LENGTH]}\n\n"
            f"Unsourced draft:\n{answer[:MAX_CONTEXT_ITEM_LENGTH]}"
        )

    async def ask(
        self,
        question: str | list[dict[str, str]],
        *,
        require_web_search: bool = False,
        require_source_links: bool = False,
    ) -> tuple[str, list[Source]]:
        if not self.api_key:
            raise OpenAIAPIError(0, "OPENAI_API_KEY is not configured")
        if self.session is None:
            raise OpenAIAPIError(0, "OpenAI client is not started")

        web_search_tool: dict[str, Any] = {"type": "web_search"}
        instructions = INSTRUCTIONS
        if require_source_links:
            web_search_tool["search_content_types"] = ["text"]
            instructions += SOURCE_LINK_INSTRUCTIONS

        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": question,
            "tools": [web_search_tool],
            "include": ["web_search_call.action.sources"],
            "reasoning": {"effort": "none"},
            "max_output_tokens": 800,
            "store": False,
        }
        if require_web_search:
            # Web search is the only tool, so "required" guarantees a search call.
            payload["tool_choice"] = "required"

        data = await self._create_response(payload)
        if require_web_search and not completed_web_search(data):
            raise OpenAIAPIError(502, "OpenAI did not complete the required web search")

        text, sources = extract_response(data)
        if not text:
            raise OpenAIAPIError(502, "OpenAI returned an empty response")

        if require_source_links and completed_web_search(data) and not sources:
            retry_payload = {
                **payload,
                "input": self._source_retry_input(question, text),
                "tool_choice": "required",
            }
            retry_data = await self._create_response(retry_payload)
            if not completed_web_search(retry_data):
                raise OpenAIAPIError(502, "OpenAI did not complete the source-link search")
            text, sources = extract_response(retry_data)
            if not text:
                raise OpenAIAPIError(502, "OpenAI returned an empty sourced response")
            if not sources:
                raise OpenAIAPIError(502, "OpenAI web search returned no public source links")
        return text, sources
