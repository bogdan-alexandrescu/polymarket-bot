"""
Market Analyzer - Uses Claude AI to analyze prediction markets.

Provides intelligent analysis of market questions, descriptions,
and available data to estimate probabilities and make recommendations.
"""

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import anthropic

# Import cache and rate limiter
from api_cache import get_cache, get_rate_limiter


@dataclass
class MarketAnalysis:
    """Result of Claude's market analysis."""
    probability_yes: float  # 0-100, Claude's estimate of YES probability
    confidence: float  # 0-100, how confident Claude is in its estimate
    recommendation: str  # "BUY_YES", "BUY_NO", "SKIP"
    reasoning: str  # Key reasoning points
    risk_factors: list[str]  # Identified risks
    market_efficiency: str  # "UNDERPRICED", "OVERPRICED", "FAIR"
    edge_estimate: float  # Estimated edge vs market price (-100 to +100)


class MarketAnalyzer:
    """Uses Claude to analyze prediction markets."""

    def __init__(
        self,
        model: str = "claude-haiku-4-20250514",
        rate_limit_per_minute: int = 15,  # Conservative rate limit
        max_retries: int = 3,
    ):
        self.client = anthropic.Anthropic()
        self.model = model
        self.max_retries = max_retries

        # Use shared rate limiter and cache
        self.rate_limiter = get_rate_limiter(requests_per_minute=rate_limit_per_minute)
        self.cache = get_cache(ttl_hours=2.0)  # Use persistent cache

    def _build_analysis_prompt(
        self,
        title: str,
        description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        liquidity: float,
        hours_to_expiry: float,
        end_date: str,
        event_title: str = "",
    ) -> str:
        """Build the analysis prompt for Claude."""

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        return f"""You are an expert prediction market analyst. Analyze this market and provide your assessment.

## Market Information
- **Question**: {title}
- **Event**: {event_title}
- **Description**: {description or "No description provided"}
- **End Date**: {end_date}
- **Hours Until Resolution**: {hours_to_expiry:.1f} hours
- **Current YES Price**: {yes_price*100:.1f}% (${yes_price:.2f})
- **Current NO Price**: {no_price*100:.1f}% (${no_price:.2f})
- **24h Volume**: ${volume_24h:,.0f}
- **Liquidity**: ${liquidity:,.0f}
- **Current Date/Time**: {today}

## Your Task
Analyze this prediction market and estimate the TRUE probability that the event resolves YES, regardless of what the market currently shows.

Consider:
1. What exactly is being asked? What are the resolution criteria?
2. Based on your knowledge, what is the likely outcome?
3. Is the current market price reasonable or mispriced?
4. What could cause this prediction to be wrong?
5. Is there enough time for the event to occur/not occur?

## Response Format
Respond with a JSON object (no other text):
{{
    "probability_yes": <number 0-100>,
    "confidence": <number 0-100>,
    "recommendation": "<BUY_YES|BUY_NO|SKIP>",
    "reasoning": "<2-3 sentence explanation>",
    "risk_factors": ["<risk1>", "<risk2>"],
    "market_efficiency": "<UNDERPRICED|OVERPRICED|FAIR>",
    "edge_estimate": <number -100 to +100, positive means market underestimates YES>
}}

Guidelines:
- probability_yes: Your TRUE estimate (not the market price)
- confidence: How sure you are in your estimate (account for uncertainty)
- recommendation: BUY_YES if you think YES is underpriced, BUY_NO if NO is underpriced, SKIP if fair or too uncertain
- edge_estimate: (your probability - market price). Positive = YES underpriced, Negative = NO underpriced
- Only recommend BUY if |edge_estimate| > 5 and confidence > 60"""

    async def analyze_market(
        self,
        condition_id: str,
        title: str,
        description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        liquidity: float,
        hours_to_expiry: float,
        end_date: str,
        event_title: str = "",
    ) -> Optional[MarketAnalysis]:
        """Analyze a market using Claude."""

        # Check cache first (using persistent cache)
        cache_key = f"{condition_id}_{int(hours_to_expiry)}"
        cached = self.cache.get("analysis", cache_key)
        if cached:
            print(f"  [Cache hit] Analysis for: {title[:50]}...")
            return MarketAnalysis(**cached)

        # Check if rate limited
        if self.rate_limiter.is_rate_limited():
            stats = self.rate_limiter.get_stats()
            print(f"  [Rate limited] Skipping analysis for: {title[:50]}... (cooldown: {stats.get('cooldown_remaining', 0):.0f}s)")
            return None

        prompt = self._build_analysis_prompt(
            title=title,
            description=description,
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=volume_24h,
            liquidity=liquidity,
            hours_to_expiry=hours_to_expiry,
            end_date=end_date,
            event_title=event_title,
        )

        # Retry with exponential backoff
        for attempt in range(self.max_retries):
            try:
                # Wait for rate limit slot
                await self.rate_limiter.acquire()

                # Run sync API call in thread pool to not block
                response = await asyncio.to_thread(
                    lambda: self.client.messages.create(
                        model=self.model,
                        max_tokens=500,
                        messages=[{"role": "user", "content": prompt}],
                    )
                )

                # Parse the response
                content = response.content[0].text.strip()

                # Handle potential markdown code blocks
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                    content = content.strip()

                data = json.loads(content)

                analysis = MarketAnalysis(
                    probability_yes=float(data.get("probability_yes", 50)),
                    confidence=float(data.get("confidence", 50)),
                    recommendation=data.get("recommendation", "SKIP"),
                    reasoning=data.get("reasoning", ""),
                    risk_factors=data.get("risk_factors", []),
                    market_efficiency=data.get("market_efficiency", "FAIR"),
                    edge_estimate=float(data.get("edge_estimate", 0)),
                )

                # Report success and cache the result
                self.rate_limiter.report_success()
                self.cache.set("analysis", cache_key, {
                    "probability_yes": analysis.probability_yes,
                    "confidence": analysis.confidence,
                    "recommendation": analysis.recommendation,
                    "reasoning": analysis.reasoning,
                    "risk_factors": analysis.risk_factors,
                    "market_efficiency": analysis.market_efficiency,
                    "edge_estimate": analysis.edge_estimate,
                })
                return analysis

            except anthropic.RateLimitError as e:
                # Report to rate limiter
                retry_after = None
                if hasattr(e, 'response') and e.response:
                    retry_after = e.response.headers.get('retry-after')
                    if retry_after:
                        try:
                            retry_after = float(retry_after)
                        except (ValueError, TypeError):
                            retry_after = None

                self.rate_limiter.report_rate_limit_error(retry_after)

                wait_time = (2 ** attempt) * 10  # 10s, 20s, 40s
                print(f"Rate limit (attempt {attempt + 1}/{self.max_retries}) for {title[:30]}..., waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue

            except json.JSONDecodeError as e:
                print(f"Failed to parse Claude response for {title[:50]}: {e}")
                return None

            except anthropic.APIStatusError as e:
                if e.status_code >= 500:
                    wait_time = (2 ** attempt) * 5
                    print(f"API error {e.status_code} (attempt {attempt + 1}/{self.max_retries}), waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue
                print(f"API error analyzing market {title[:50]}: {e}")
                return None

            except Exception as e:
                print(f"Error analyzing market {title[:50]}: {e}")
                return None

        print(f"Rate limit exhausted for {title[:50]}")
        return None

    async def analyze_markets_batch(
        self,
        markets: list[dict],
        max_concurrent: int = 2,  # Reduced from 10 to avoid rate limits
    ) -> dict[str, MarketAnalysis]:
        """Analyze multiple markets concurrently."""

        semaphore = asyncio.Semaphore(max_concurrent)
        results = {}

        async def analyze_with_semaphore(market: dict):
            async with semaphore:
                condition_id = market.get("conditionId", "")
                analysis = await self.analyze_market(
                    condition_id=condition_id,
                    title=market.get("question", ""),
                    description=market.get("description", ""),
                    yes_price=market.get("_yes_price", 0.5),
                    no_price=market.get("_no_price", 0.5),
                    volume_24h=market.get("_volume_24h", 0),
                    liquidity=market.get("_liquidity", 0),
                    hours_to_expiry=market.get("_hours_to_expiry", 24),
                    end_date=market.get("endDate", ""),
                    event_title=market.get("_event_title", ""),
                )
                if analysis:
                    results[condition_id] = analysis

        tasks = [analyze_with_semaphore(m) for m in markets]
        await asyncio.gather(*tasks)

        return results

    def clear_cache(self):
        """Clear the analysis cache."""
        # Note: This now uses shared cache, so it clears analysis entries only
        self.cache.clear_all()
