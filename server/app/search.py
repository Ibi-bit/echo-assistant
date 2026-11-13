"""Web lookup helpers for the assistant's ``search`` tool."""
from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

_WEATHER = re.compile(r"\b(?:weather|forecast|temperature|rain|snow|sunny|cloudy)\b")
_TIME_WORDS = {
    "today", "tonight", "tomorrow", "now", "morning", "afternoon", "evening",
    "night", "week", "weekend", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "please",
}
_DDG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
}


def _clean_place(raw: str) -> str | None:
    place = re.split(
        r"\b(?:today|tonight|tomorrow|right\s+now|this|next|for|on|at|in|near|please)\b",
        raw,
        maxsplit=1,
    )[0]
    place = re.sub(r"^(?:the|a|an)\s+", "", place.strip())
    place = place.strip(" ,.'")
    if not place or place.split()[0] in _TIME_WORDS:
        return None
    return place[:80]


def _extract_location(query: str) -> str | None:
    """Return an explicitly named place, ignoring 'in the morning' style words."""
    for match in re.finditer(
        r"\b(?:in|at|near|for)\s+([a-z][a-z .'-]{0,60})", query.lower()
    ):
        place = _clean_place(match.group(1))
        if place:
            return place
    return None


async def fetch_wttr(http: httpx.AsyncClient, location: str) -> str | None:
    """Fetch concise current weather from wttr.in (no API key, JSON)."""
    try:
        response = await http.get(
            f"https://wttr.in/{quote(location)}?format=j1", timeout=8.0
        )
        response.raise_for_status()
        data = response.json()
        current = data.get("current_condition", [{}])[0]
        area = data.get("nearest_area", [{}])[0]
        temp_c = current.get("temp_C")
        temp_f = current.get("temp_F")
        desc = current.get("weatherDesc", [{}])[0].get("value", "").lower()
        humidity = current.get("humidity")
        wind_kph = current.get("windspeedKmph")
        wind_dir = current.get("winddir16Point")
        place = area.get("areaName", [{}])[0].get("value", location)
        if temp_c is None:
            return None
        parts = [f"{place}: {temp_c}°C ({temp_f}°F)", desc]
        if humidity:
            parts.append(f"humidity {humidity}%")
        if wind_kph:
            parts.append(f"wind {wind_kph} km/h {wind_dir}")
        return "Right now, " + ", ".join(parts) + "."
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        log.debug("wttr.in failed for %r", location)
        return None


def parse_ddg_results(page: str) -> list[tuple[str, str]]:
    def clean(raw: str) -> str:
        return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()

    titles = re.findall(
        r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', page, flags=re.DOTALL | re.IGNORECASE
    )
    snippets = re.findall(
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', page, flags=re.DOTALL | re.IGNORECASE
    ) or re.findall(
        r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', page, flags=re.DOTALL | re.IGNORECASE
    )
    results: list[tuple[str, str]] = []
    for index, title in enumerate(titles[:4]):
        snippet = snippets[index] if index < len(snippets) else ""
        results.append((clean(title), clean(snippet)))
    return results


def format_results(query: str, results: list[tuple[str, str]]) -> str:
    lines = [f"Results for '{query}':"]
    for title, snippet in results:
        line = f"{title}: {snippet}" if title and snippet else title or snippet
        lines.append(f"- {line}")
    return "\n".join(lines)[:1200]


async def web_search(
    http: httpx.AsyncClient, query: str, home_location: str
) -> dict:
    """Server-side web lookup with a fast wttr.in path for weather queries."""
    if _WEATHER.search(query.lower()):
        location = _extract_location(query) or home_location.strip()
        if location:
            wttr = await fetch_wttr(http, location)
            if wttr:
                return {"ok": True, "message": wttr}

    try:
        response = await http.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=_DDG_HEADERS,
            timeout=10.0,
        )
        if response.status_code == 200:
            results = parse_ddg_results(response.text)
            if results:
                return {"ok": True, "message": format_results(query, results)}
        response = await http.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1},
            timeout=10.0,
        )
        response.raise_for_status()
        abstract = str(response.json().get("AbstractText") or "").strip()
        if abstract:
            return {"ok": True, "message": f"Answer: {abstract}"}
    except Exception:
        log.exception("web search failed")
    return {"ok": False, "message": "I could not reach the web right now."}
