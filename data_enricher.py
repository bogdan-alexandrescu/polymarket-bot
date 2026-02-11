"""
Data Enricher - Gathers additional data for market analysis.

Enriches market data with:
- Historical price data from Polymarket activity API
- Related markets from Gamma API
- Price trends and volatility calculations
"""

import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from typing import Optional
import re


class DataEnricher:
    """Enriches market data with historical prices and related markets."""

    def __init__(self, lookback_hours: int = 24, max_related: int = 5):
        self.lookback_hours = lookback_hours
        self.max_related = max_related

    async def enrich_market(self, market: dict) -> dict:
        """
        Add historical data and related markets to a market dict.

        Args:
            market: Market dict with conditionId, question, etc.

        Returns:
            Market dict with added enrichment fields:
            - _price_trend: UP, DOWN, or STABLE
            - _price_volatility: 0-1 volatility score
            - _price_history: list of recent prices
            - _related_markets: list of related market summaries
            - _correlation_risk: 0-1 risk from correlated markets
        """
        condition_id = market.get("conditionId", "")
        question = market.get("question", "")

        # Fetch data concurrently
        price_task = self.get_price_history(condition_id)
        related_task = self.find_related_markets(question, condition_id)

        price_history, related_markets = await asyncio.gather(
            price_task, related_task, return_exceptions=True
        )

        # Handle price history
        if isinstance(price_history, Exception):
            price_history = []

        if price_history:
            market["_price_history"] = price_history
            market["_price_trend"] = self.calculate_trend(price_history)
            market["_price_volatility"] = self.calculate_volatility(price_history)
        else:
            market["_price_history"] = []
            market["_price_trend"] = "STABLE"
            market["_price_volatility"] = 0.0

        # Handle related markets
        if isinstance(related_markets, Exception):
            related_markets = []

        market["_related_markets"] = related_markets
        market["_correlation_risk"] = self.assess_correlation_risk(related_markets)

        return market

    async def enrich_batch(
        self,
        markets: list[dict],
        max_concurrent: int = 5,
    ) -> list[dict]:
        """Enrich multiple markets concurrently."""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def enrich_with_limit(market: dict) -> dict:
            async with semaphore:
                return await self.enrich_market(market)

        tasks = [enrich_with_limit(m) for m in markets]
        return await asyncio.gather(*tasks)

    async def get_price_history(
        self,
        condition_id: str,
        hours: int = None,
    ) -> list[dict]:
        """
        Get recent price history from Polymarket activity API.

        Returns list of price points:
        [{"timestamp": epoch, "price": 0.0-1.0}, ...]
        """
        if not condition_id:
            return []

        hours = hours or self.lookback_hours

        try:
            async with aiohttp.ClientSession() as session:
                # Use the Polymarket data API to get trade activity
                async with session.get(
                    "https://data-api.polymarket.com/activity",
                    params={
                        "market": condition_id,
                        "limit": 200,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return []

                    activities = await resp.json()

            if not activities:
                return []

            # Filter to trades within our lookback window
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()

            price_history = []
            for activity in activities:
                if activity.get("type") != "TRADE":
                    continue

                timestamp = activity.get("timestamp", 0)
                if timestamp < cutoff:
                    continue

                price = float(activity.get("price", 0))
                if 0 < price < 1:  # Valid price range
                    price_history.append({
                        "timestamp": timestamp,
                        "price": price,
                    })

            # Sort by timestamp ascending
            price_history.sort(key=lambda x: x["timestamp"])
            return price_history

        except Exception as e:
            print(f"Error fetching price history for {condition_id}: {e}")
            return []

    def calculate_trend(self, price_history: list[dict]) -> str:
        """
        Calculate price trend from history.

        Returns: "UP", "DOWN", or "STABLE"
        """
        if len(price_history) < 2:
            return "STABLE"

        # Compare first quarter to last quarter of prices
        n = len(price_history)
        quarter = max(1, n // 4)

        early_prices = [p["price"] for p in price_history[:quarter]]
        late_prices = [p["price"] for p in price_history[-quarter:]]

        early_avg = sum(early_prices) / len(early_prices)
        late_avg = sum(late_prices) / len(late_prices)

        change = late_avg - early_avg

        # Threshold for trend detection (5% change)
        if change > 0.05:
            return "UP"
        elif change < -0.05:
            return "DOWN"
        else:
            return "STABLE"

    def calculate_volatility(self, price_history: list[dict]) -> float:
        """
        Calculate price volatility from history.

        Returns: 0-1 volatility score (0 = stable, 1 = highly volatile)
        """
        if len(price_history) < 3:
            return 0.0

        prices = [p["price"] for p in price_history]

        # Calculate standard deviation of price changes
        changes = []
        for i in range(1, len(prices)):
            change = abs(prices[i] - prices[i-1])
            changes.append(change)

        if not changes:
            return 0.0

        avg_change = sum(changes) / len(changes)

        # Also consider max swing
        max_price = max(prices)
        min_price = min(prices)
        swing = max_price - min_price

        # Combine average change and swing for volatility score
        # Normalize: avg_change of 0.05 = 0.5 volatility, swing of 0.20 = 0.5
        vol_from_changes = min(1.0, avg_change / 0.10)
        vol_from_swing = min(1.0, swing / 0.40)

        return (vol_from_changes + vol_from_swing) / 2

    async def find_related_markets(
        self,
        question: str,
        exclude_condition_id: str = None,
    ) -> list[dict]:
        """
        Find markets related to the given question.

        Returns list of related market summaries:
        [{
            "conditionId": str,
            "question": str,
            "yes_price": float,
            "similarity": float  # 0-1
        }, ...]
        """
        if not question:
            return []

        # Extract key terms from question
        search_terms = self._extract_search_terms(question)
        if not search_terms:
            return []

        try:
            async with aiohttp.ClientSession() as session:
                # Search Gamma API for related markets
                async with session.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={
                        "_s": search_terms,
                        "closed": "false",
                        "limit": 20,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return []

                    markets = await resp.json()

            if not markets:
                return []

            # Filter and score related markets
            related = []
            question_lower = question.lower()

            for m in markets:
                cond_id = m.get("conditionId", "")

                # Skip the market itself
                if cond_id == exclude_condition_id:
                    continue

                m_question = m.get("question", "")

                # Calculate similarity
                similarity = self._calculate_similarity(question_lower, m_question.lower())

                # Only include if somewhat similar
                if similarity > 0.2:
                    # Parse price
                    try:
                        prices_str = m.get("outcomePrices", "[]")
                        import json
                        prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
                        yes_price = float(prices[0]) if prices else 0.5
                    except:
                        yes_price = 0.5

                    related.append({
                        "conditionId": cond_id,
                        "question": m_question,
                        "yes_price": yes_price,
                        "similarity": similarity,
                    })

            # Sort by similarity and limit
            related.sort(key=lambda x: x["similarity"], reverse=True)
            return related[:self.max_related]

        except Exception as e:
            print(f"Error finding related markets: {e}")
            return []

    def _extract_search_terms(self, question: str) -> str:
        """Extract key search terms from a question."""
        # Remove common words
        stop_words = {
            'will', 'the', 'be', 'by', 'in', 'on', 'at', 'to', 'for', 'of',
            'a', 'an', 'is', 'are', 'was', 'were', 'this', 'that', 'before',
            'after', 'during', 'than', 'or', 'and', 'if', 'when', 'how', 'what',
            'who', 'which', 'yes', 'no', '2024', '2025', '2026',
        }

        # Extract words
        words = re.findall(r'\b[a-zA-Z]{3,}\b', question.lower())

        # Filter stop words and keep important terms
        key_terms = [w for w in words if w not in stop_words]

        # Return top 4 terms
        return ' '.join(key_terms[:4])

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """Calculate simple similarity between two texts."""
        # Extract word sets
        words1 = set(re.findall(r'\b[a-zA-Z]{3,}\b', text1))
        words2 = set(re.findall(r'\b[a-zA-Z]{3,}\b', text2))

        if not words1 or not words2:
            return 0.0

        # Jaccard similarity
        intersection = len(words1 & words2)
        union = len(words1 | words2)

        return intersection / union if union > 0 else 0.0

    def assess_correlation_risk(self, related_markets: list[dict]) -> float:
        """
        Assess risk from correlated markets.

        Returns: 0-1 correlation risk score
        """
        if not related_markets:
            return 0.0

        # Higher similarity = higher correlation risk
        # More related markets with high similarity = higher risk

        total_similarity = sum(m["similarity"] for m in related_markets)
        avg_similarity = total_similarity / len(related_markets)

        # Risk increases with:
        # 1. Number of related markets
        # 2. Average similarity
        count_factor = min(1.0, len(related_markets) / 5)  # Max at 5 related
        similarity_factor = avg_similarity

        return (count_factor + similarity_factor) / 2
