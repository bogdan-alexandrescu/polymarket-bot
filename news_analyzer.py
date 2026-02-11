"""
News and Sentiment Analyzer for market opportunities.

Uses web search and news APIs to assess likelihood of market outcomes.
"""

import asyncio
import aiohttp
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass


@dataclass
class NewsAnalysis:
    """Result of news analysis for a market."""
    query: str
    articles_found: int
    relevant_headlines: list[str]
    summary: str
    event_occurred: bool  # Has the triggering event happened?
    event_likelihood: float  # 0-1, likelihood event will occur
    confidence: float  # 0-1, confidence in this analysis
    sentiment: float  # 0=bearish on event, 1=bullish on event
    sources: list[str]


class NewsAnalyzer:
    """Analyzes news and sentiment for Polymarket events."""

    # Keywords indicating an event has occurred
    EVENT_OCCURRED_KEYWORDS = [
        "confirmed", "happened", "occurred", "struck", "attacked", "launched",
        "announced", "declared", "broke out", "started", "began", "invaded",
        "signed", "passed", "approved", "enacted", "died", "resigned",
        "arrested", "indicted", "convicted", "won", "lost", "defeated",
    ]

    # Keywords indicating event is unlikely
    EVENT_UNLIKELY_KEYWORDS = [
        "denied", "rejected", "unlikely", "no evidence", "failed", "postponed",
        "cancelled", "canceled", "dismissed", "ruled out", "no plans",
        "not expected", "remains stable", "unchanged", "no change",
    ]

    # Keywords indicating uncertainty
    UNCERTAINTY_KEYWORDS = [
        "may", "might", "could", "possibly", "potential", "rumor", "speculation",
        "unconfirmed", "alleged", "reportedly", "sources say",
    ]

    def __init__(self):
        self.cache = {}  # Simple cache for repeated queries

    def extract_search_terms(self, market_title: str, event_title: str) -> str:
        """Extract key search terms from market/event title."""
        combined = f"{event_title} {market_title}"

        # Remove common filler words and dates
        stopwords = [
            "will", "the", "by", "before", "after", "in", "on", "at", "to", "of",
            "be", "is", "are", "was", "were", "been", "being", "have", "has", "had",
            "do", "does", "did", "a", "an", "and", "or", "but", "if", "then",
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
            "2024", "2025", "2026", "2027",
        ]

        words = combined.lower().split()
        filtered = [w for w in words if w not in stopwords and len(w) > 2]

        # Take first 5-6 meaningful words
        return " ".join(filtered[:6])

    async def search_news(self, query: str, max_results: int = 5) -> list[dict]:
        """Search for recent news articles."""
        articles = []

        try:
            async with aiohttp.ClientSession() as session:
                # Try Google News RSS
                google_url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
                async with session.get(
                    google_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        # Parse RSS titles
                        import re
                        titles = re.findall(r'<title>(?:<!\[CDATA\[)?([^<\]]+)', text)
                        for title in titles[1:max_results+1]:  # Skip feed title
                            articles.append({
                                "title": title.strip(),
                                "source": "Google News",
                            })
        except Exception as e:
            pass

        return articles

    def analyze_text_sentiment(self, text: str) -> dict:
        """Analyze sentiment and event indicators in text."""
        text_lower = text.lower()

        # Check for event occurred
        occurred_count = sum(1 for kw in self.EVENT_OCCURRED_KEYWORDS if kw in text_lower)

        # Check for unlikely
        unlikely_count = sum(1 for kw in self.EVENT_UNLIKELY_KEYWORDS if kw in text_lower)

        # Check for uncertainty
        uncertainty_count = sum(1 for kw in self.UNCERTAINTY_KEYWORDS if kw in text_lower)

        # Calculate scores
        event_occurred = occurred_count >= 2
        event_likelihood = 0.5  # Base

        if occurred_count > unlikely_count:
            event_likelihood = min(0.9, 0.5 + occurred_count * 0.1)
        elif unlikely_count > occurred_count:
            event_likelihood = max(0.1, 0.5 - unlikely_count * 0.1)

        # Adjust for uncertainty
        confidence = max(0.3, 1.0 - uncertainty_count * 0.15)

        return {
            "event_occurred": event_occurred,
            "event_likelihood": event_likelihood,
            "confidence": confidence,
            "occurred_signals": occurred_count,
            "unlikely_signals": unlikely_count,
            "uncertainty_signals": uncertainty_count,
        }

    async def analyze(self, market_title: str, event_title: str) -> NewsAnalysis:
        """Perform full news analysis for a market."""
        # Generate search query
        query = self.extract_search_terms(market_title, event_title)

        # Check cache
        cache_key = query.lower()
        if cache_key in self.cache:
            return self.cache[cache_key]

        # Search for news
        articles = await self.search_news(query)

        # Combine all text for analysis
        all_text = " ".join([a["title"] for a in articles])

        # Analyze sentiment
        sentiment_result = self.analyze_text_sentiment(all_text)

        # Build result
        result = NewsAnalysis(
            query=query,
            articles_found=len(articles),
            relevant_headlines=[a["title"] for a in articles[:3]],
            summary=articles[0]["title"] if articles else "No recent news found",
            event_occurred=sentiment_result["event_occurred"],
            event_likelihood=sentiment_result["event_likelihood"],
            confidence=sentiment_result["confidence"],
            sentiment=sentiment_result["event_likelihood"],  # Higher = more likely event happens
            sources=[a.get("source", "Unknown") for a in articles],
        )

        # Cache result
        self.cache[cache_key] = result

        return result

    async def quick_check(self, market_title: str) -> dict:
        """Quick check if an event has likely occurred."""
        query = self.extract_search_terms(market_title, "")
        articles = await self.search_news(query, max_results=3)

        if not articles:
            return {"event_likely_occurred": False, "confidence": 0.3}

        all_text = " ".join([a["title"] for a in articles])
        result = self.analyze_text_sentiment(all_text)

        return {
            "event_likely_occurred": result["event_occurred"],
            "confidence": result["confidence"],
            "headlines": [a["title"] for a in articles],
        }
