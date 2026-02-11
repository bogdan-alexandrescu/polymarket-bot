"""
Web Researcher - Fetches real-world information for market verification.

Uses web search to gather context about market topics and detect
whether predicted events have already occurred.
"""

import asyncio
import aiohttp
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus


class WebResearcher:
    """Researches web for market-relevant information."""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    async def research_market(
        self,
        title: str,
        description: str = "",
        event_title: str = "",
    ) -> dict:
        """
        Search web for relevant information about market topic.

        Args:
            title: Market question/title
            description: Market description
            event_title: Parent event title

        Returns:
            {
                "search_query": str,
                "top_results": [{"title": str, "snippet": str, "url": str}],
                "summary": str,
                "event_status": "OCCURRED" | "NOT_OCCURRED" | "UNKNOWN",
                "confidence": float,  # 0-1 confidence in status
                "keywords_found": [str],
            }
        """
        # Build search query
        search_query = self._build_search_query(title, event_title)

        # Perform search
        results = await self._web_search(search_query)

        # Analyze results
        event_status, confidence, keywords = self._analyze_results(results, title)

        # Build summary
        summary = self._summarize_results(results)

        return {
            "search_query": search_query,
            "top_results": results[:5],
            "summary": summary,
            "event_status": event_status,
            "confidence": confidence,
            "keywords_found": keywords,
        }

    async def research_batch(
        self,
        markets: list[dict],
        max_concurrent: int = 5,
    ) -> dict[str, dict]:
        """
        Research multiple markets concurrently.

        Returns: {condition_id: research_result}
        """
        semaphore = asyncio.Semaphore(max_concurrent)
        results = {}

        async def research_with_limit(market: dict):
            condition_id = market.get("conditionId", "")
            async with semaphore:
                try:
                    research = await self.research_market(
                        title=market.get("question", ""),
                        description=market.get("description", ""),
                        event_title=market.get("_event_title", ""),
                    )
                    results[condition_id] = research
                except Exception as e:
                    print(f"Error researching {condition_id}: {e}")
                    results[condition_id] = self._empty_result()

        tasks = [research_with_limit(m) for m in markets]
        await asyncio.gather(*tasks)

        return results

    def _build_search_query(self, title: str, event_title: str = "") -> str:
        """Build an effective search query from market info."""
        # Combine title and event
        text = f"{event_title} {title}" if event_title else title

        # Remove question marks and common words
        text = text.replace("?", "")

        # Remove time references that might confuse search
        text = re.sub(r'\b(by|before|after)\s+(January|February|March|April|May|June|July|August|September|October|November|December)\b', '', text, flags=re.I)
        text = re.sub(r'\b(by|before|after)\s+\d{1,2}(st|nd|rd|th)?\b', '', text, flags=re.I)
        text = re.sub(r'\b202\d\b', '', text)

        # Remove common prediction market words
        stop_phrases = [
            'will', 'be', 'the', 'by', 'before', 'after', 'in', 'on', 'at',
            'this', 'that', 'or more', 'or less', 'at least', 'more than',
            'less than', 'greater than',
        ]
        for phrase in stop_phrases:
            text = re.sub(rf'\b{phrase}\b', '', text, flags=re.I)

        # Clean up whitespace
        text = ' '.join(text.split())

        # Limit length
        words = text.split()[:8]
        return ' '.join(words)

    async def _web_search(self, query: str) -> list[dict]:
        """
        Perform web search using DuckDuckGo instant answers API.

        Returns list of results: [{"title": str, "snippet": str, "url": str}]
        """
        if not query:
            return []

        results = []

        try:
            async with aiohttp.ClientSession() as session:
                # DuckDuckGo instant answers API
                async with session.get(
                    "https://api.duckduckgo.com/",
                    params={
                        "q": query,
                        "format": "json",
                        "no_html": "1",
                        "skip_disambig": "1",
                    },
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        return []

                    data = await resp.json()

                # Extract abstract
                if data.get("Abstract"):
                    results.append({
                        "title": data.get("Heading", ""),
                        "snippet": data.get("Abstract", ""),
                        "url": data.get("AbstractURL", ""),
                        "source": "abstract",
                    })

                # Extract related topics
                for topic in data.get("RelatedTopics", [])[:5]:
                    if isinstance(topic, dict) and topic.get("Text"):
                        results.append({
                            "title": topic.get("FirstURL", "").split("/")[-1].replace("_", " "),
                            "snippet": topic.get("Text", ""),
                            "url": topic.get("FirstURL", ""),
                            "source": "related",
                        })

                # Extract news/infobox if available
                if data.get("Infobox"):
                    infobox = data["Infobox"]
                    for item in infobox.get("content", [])[:3]:
                        if item.get("value"):
                            results.append({
                                "title": item.get("label", ""),
                                "snippet": str(item.get("value", "")),
                                "url": "",
                                "source": "infobox",
                            })

        except Exception as e:
            print(f"Web search error for '{query}': {e}")

        return results

    def _analyze_results(
        self,
        results: list[dict],
        title: str,
    ) -> tuple[str, float, list[str]]:
        """
        Analyze search results to detect event status.

        Returns: (status, confidence, keywords_found)
        """
        if not results:
            return "UNKNOWN", 0.0, []

        # Combine all text
        all_text = " ".join(
            f"{r.get('title', '')} {r.get('snippet', '')}"
            for r in results
        ).lower()

        # Keywords indicating event occurred
        occurred_keywords = [
            "confirmed", "happened", "occurred", "announced", "declared",
            "broke out", "launched", "struck", "killed", "injured",
            "passed", "signed", "enacted", "approved", "won", "lost",
            "defeated", "elected", "resigned", "fired", "hired",
            "released", "published", "revealed", "discovered",
        ]

        # Keywords indicating event did not occur
        not_occurred_keywords = [
            "denied", "rejected", "failed", "postponed", "cancelled",
            "delayed", "unlikely", "no evidence", "not expected",
            "remains", "still", "yet to", "waiting", "expected to",
            "may", "might", "could", "planned", "scheduled",
        ]

        # Count keyword matches
        occurred_matches = []
        not_occurred_matches = []

        for kw in occurred_keywords:
            if kw in all_text:
                occurred_matches.append(kw)

        for kw in not_occurred_keywords:
            if kw in all_text:
                not_occurred_matches.append(kw)

        # Determine status
        occurred_score = len(occurred_matches)
        not_occurred_score = len(not_occurred_matches)

        if occurred_score > not_occurred_score and occurred_score >= 2:
            return "OCCURRED", min(0.8, occurred_score * 0.2), occurred_matches
        elif not_occurred_score > occurred_score and not_occurred_score >= 2:
            return "NOT_OCCURRED", min(0.8, not_occurred_score * 0.2), not_occurred_matches
        else:
            all_matches = occurred_matches + not_occurred_matches
            return "UNKNOWN", 0.3, all_matches

    def _summarize_results(self, results: list[dict]) -> str:
        """Create a brief summary from search results."""
        if not results:
            return ""

        # Use the first abstract/snippet
        for r in results:
            snippet = r.get("snippet", "")
            if snippet and len(snippet) > 50:
                # Truncate to ~200 chars at sentence boundary
                if len(snippet) > 200:
                    cut = snippet[:200].rfind(".")
                    if cut > 100:
                        return snippet[:cut + 1]
                    return snippet[:200] + "..."
                return snippet

        return ""

    def _empty_result(self) -> dict:
        """Return empty research result."""
        return {
            "search_query": "",
            "top_results": [],
            "summary": "",
            "event_status": "UNKNOWN",
            "confidence": 0.0,
            "keywords_found": [],
        }
