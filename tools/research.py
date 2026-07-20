"""Research tools — real-time news headlines via NewsAPI, fallback to LLM."""

import os
import json
import logging
import httpx
from dotenv import load_dotenv
from infra.llm import acall_llm

load_dotenv()
logger = logging.getLogger(__name__)

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
NEWSAPI_URL = "https://newsapi.org/v2/top-headlines"


async def fetch_headlines(country: str = "us", count: int = 10) -> list[dict]:
    """Fetch real-time headlines from NewsAPI.

    Returns list of {title, source, url} dicts.
    Falls back to LLM-based news on API failure or missing key.
    """
    if not NEWSAPI_KEY or NEWSAPI_KEY.startswith("your-"):
        logger.info("NEWSAPI_KEY not configured, falling back to LLM news")
        return await _llm_headlines(count)

    try:
        params = {
            "apiKey": NEWSAPI_KEY,
            "country": country,
            "pageSize": count,
            "language": "zh" if country == "cn" else "en",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(NEWSAPI_URL, params=params)
            if resp.status_code != 200:
                logger.warning(f"NewsAPI returned {resp.status_code}: {resp.text[:200]}")
                return await _llm_headlines(count)

            data = resp.json()
            articles = data.get("articles", [])
            headlines = []
            for a in articles[:count]:
                headlines.append({
                    "title": a.get("title", ""),
                    "source": a.get("source", {}).get("name", ""),
                    "url": a.get("url", ""),
                })
            logger.info(f"Fetched {len(headlines)} headlines from NewsAPI")
            return headlines

    except Exception as e:
        logger.warning(f"NewsAPI fetch failed: {e}, falling back to LLM")
        return await _llm_headlines(count)


async def _llm_headlines(count: int = 10) -> list[dict]:
    """LLM-based headline generation as fallback."""
    prompt = """列出当前全球最受关注的10条新闻标题。每条的source注明出处。

输出JSON：[{"title":"新闻标题","source":"Reuters/CNN/36氪/财新..."}]"""

    try:
        resp = await acall_llm(prompt, "当前全球头条新闻", provider="deepseek",
                               temperature=0.3, max_tokens=1024, json_mode=True,
                               trace_stage="llm_headlines")
        results = json.loads(resp)
        if isinstance(results, dict):
            results = results.get("headlines", results.get("articles", results.get("items", [])))
        return results[:count] if isinstance(results, list) else []
    except Exception as e:
        logger.warning(f"LLM headlines failed: {e}")
        return []


async def search_trending(keyword: str = "", count: int = 5) -> list:
    """Get trending topics. Uses NewsAPI headlines as anchors."""
    headlines = await fetch_headlines(count=count * 2)
    if not headlines:
        return []

    topics = []
    for h in headlines:
        topics.append({
            "topic": h.get("title", ""),
            "score": 0.9,
            "source": h.get("source", ""),
        })
    return topics[:count]


async def search_news(topic: str = "", freshness: str = "week") -> list:
    """Search for news on a specific topic. Uses NewsAPI everything endpoint."""
    if not topic or not NEWSAPI_KEY or NEWSAPI_KEY.startswith("your-"):
        return []

    try:
        params = {
            "apiKey": NEWSAPI_KEY,
            "q": topic,
            "pageSize": 5,
            "sortBy": "publishedAt",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get("https://newsapi.org/v2/everything", params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            articles = data.get("articles", [])
            return [
                {"title": a.get("title", ""), "source": a.get("source", {}).get("name", ""),
                 "key_fact": a.get("description", "")[:120], "credibility": "medium"}
                for a in articles[:5]
            ]
    except Exception as e:
        logger.warning(f"search_news failed: {e}")
        return []


async def search_web(query: str, count: int = 5) -> list[dict]:
    """Search the web using DuckDuckGo (free, no API key needed).

    Returns list of {title, snippet, url, source} dicts.
    """
    try:
        from ddgs import DDGS

        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=count):
                results.append({
                    "title": r.get("title", ""),
                    "snippet": r.get("body", ""),
                    "url": r.get("href", ""),
                    "source": r.get("href", "").split("/")[2] if r.get("href") else "",
                })

        logger.info(f"DuckDuckGo: '{query}' → {len(results)} results")
        return results

    except ImportError:
        logger.warning("ddgs not installed. Run: pip install ddgs")
        return []
    except Exception as e:
        logger.warning(f"DuckDuckGo search failed: {e}")
        return []
