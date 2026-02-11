"""
Facts Gatherer - Gathers real-time, market-specific facts using Claude with web search.

For each market, identifies what specific factual data would be useful and searches
for up-to-date information to help make informed trading decisions.

Features:
- Caching to reduce API costs and avoid rate limits
- Rate limiting to stay within API limits
- Configurable TTL for cache entries
"""

import asyncio
import os
import re
import json
from datetime import datetime, timezone
from typing import Optional
import anthropic
from dataclasses import dataclass, field, asdict
from dotenv import load_dotenv

load_dotenv()

# Import cache and rate limiter
from api_cache import get_cache, get_rate_limiter, APICache, RateLimiter


@dataclass
class MarketFacts:
    """Real-time facts relevant to a specific market."""
    condition_id: str
    market_question: str

    # Key factual data points
    key_facts: list = field(default_factory=list)  # List of {"fact": str, "value": str, "source": str}

    # Current status relative to market question
    current_status: str = ""  # Brief status summary
    progress_indicator: str = ""  # e.g., "15/20 tweets", "3 days remaining", "$45,230 raised"

    # Timestamp
    gathered_at: str = ""

    # Quality indicator
    data_quality: str = "UNKNOWN"  # HIGH, MEDIUM, LOW, UNKNOWN

    # Cache status
    from_cache: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary for caching."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MarketFacts":
        """Create from dictionary (cache retrieval)."""
        return cls(**data)


class FactsGatherer:
    """Gathers real-time facts for prediction markets using Claude with web search."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",  # Sonnet for web search capability
        cache_ttl_hours: float = 2.0,
        enable_cache: bool = True,
        rate_limit_per_minute: int = 8,  # Very conservative - web search is expensive
        max_retries: int = 4,  # More retries with longer backoffs
    ):
        self.client = anthropic.Anthropic()
        self.model = model
        self.enable_cache = enable_cache
        self.cache_ttl_seconds = int(cache_ttl_hours * 3600)
        self.max_retries = max_retries

        # Get shared cache and rate limiter
        self.cache = get_cache(ttl_hours=cache_ttl_hours) if enable_cache else None
        self.rate_limiter = get_rate_limiter(requests_per_minute=rate_limit_per_minute)

    async def gather_facts(
        self,
        condition_id: str,
        market_question: str,
        market_description: str = "",
        end_date: str = "",
        skip_cache: bool = False,
    ) -> MarketFacts:
        """Gather real-time facts for a specific market."""

        now = datetime.now(timezone.utc)

        # Check cache first (unless skip_cache is True)
        if self.enable_cache and self.cache and not skip_cache:
            cached = self.cache.get("facts", market_question)
            if cached:
                try:
                    facts = MarketFacts.from_dict(cached)
                    facts.from_cache = True
                    print(f"  [Cache hit] Facts for: {market_question[:50]}...")
                    return facts
                except (TypeError, KeyError):
                    pass  # Invalid cache entry, fetch fresh

        # Check if we're currently rate limited before even trying
        if self.rate_limiter.is_rate_limited():
            stats = self.rate_limiter.get_stats()
            cooldown = stats.get('cooldown_remaining', 0)
            print(f"  [Rate limited] Skipping facts for: {market_question[:50]}... (cooldown: {cooldown:.0f}s)")
            return MarketFacts(
                condition_id=condition_id,
                market_question=market_question,
                current_status="Rate limited - try again later",
                data_quality="UNKNOWN",
                gathered_at=now.isoformat(),
            )

        # Rate limiting - wait for slot
        await self.rate_limiter.acquire()

        # Build the prompt
        prompt = self._build_prompt(market_question, market_description, end_date, now)

        # Retry with exponential backoff
        last_error = None
        for attempt in range(self.max_retries):
            try:
                # Call Claude with web search
                response = await asyncio.to_thread(
                    self._call_claude_with_search,
                    prompt
                )

                # Parse the response
                facts = self._parse_response(response, condition_id, market_question)
                facts.gathered_at = now.isoformat()
                facts.from_cache = False

                # Report success to rate limiter
                self.rate_limiter.report_success()

                # Cache the result
                if self.enable_cache and self.cache:
                    self.cache.set(
                        "facts",
                        market_question,
                        facts.to_dict(),
                        ttl_seconds=self.cache_ttl_seconds,
                    )

                return facts

            except anthropic.RateLimitError as e:
                last_error = e

                # Extract retry-after header if present
                retry_after = None
                if hasattr(e, 'response') and e.response:
                    retry_after = e.response.headers.get('retry-after')
                    if retry_after:
                        try:
                            retry_after = float(retry_after)
                        except (ValueError, TypeError):
                            retry_after = None

                # Report to rate limiter for smarter backoff
                self.rate_limiter.report_rate_limit_error(retry_after)

                # Exponential backoff: 15s, 30s, 60s, 120s
                wait_time = (2 ** attempt) * 15
                if retry_after and retry_after > wait_time:
                    wait_time = retry_after + 5  # Add buffer

                print(f"Rate limit (attempt {attempt + 1}/{self.max_retries}), waiting {wait_time:.0f}s...")
                await asyncio.sleep(wait_time)
                continue

            except anthropic.APIStatusError as e:
                # Handle server errors (5xx) with retry
                if e.status_code >= 500:
                    wait_time = (2 ** attempt) * 10
                    print(f"API error {e.status_code} (attempt {attempt + 1}/{self.max_retries}), waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    print(f"API error gathering facts for {condition_id}: {e}")
                    return MarketFacts(
                        condition_id=condition_id,
                        market_question=market_question,
                        current_status=f"API Error: {str(e)[:100]}",
                        data_quality="UNKNOWN",
                        gathered_at=now.isoformat(),
                    )

            except Exception as e:
                print(f"Error gathering facts for {condition_id}: {e}")
                return MarketFacts(
                    condition_id=condition_id,
                    market_question=market_question,
                    current_status=f"Error: {str(e)[:100]}",
                    data_quality="UNKNOWN",
                    gathered_at=now.isoformat(),
                )

        # All retries exhausted - return rate limited response
        print(f"Rate limit exhausted for {condition_id}")
        return MarketFacts(
            condition_id=condition_id,
            market_question=market_question,
            current_status="Rate limited - try again later",
            data_quality="UNKNOWN",
            gathered_at=now.isoformat(),
        )

    def _build_prompt(
        self,
        question: str,
        description: str,
        end_date: str,
        now: datetime,
    ) -> str:
        """Build the prompt for Claude to gather facts."""

        date_context = ""
        if end_date:
            date_context = f"\nMarket end date: {end_date}"

        # Shorter prompt to reduce token usage
        return f"""Research this prediction market. Find current facts.

Question: {question}
Date: {now.strftime("%Y-%m-%d")}{date_context}

Search for current data. Return JSON only:
{{"key_facts":[{{"fact":"..","value":"..","source":".."}}],"current_status":"..","progress_indicator":"..","data_quality":"HIGH/MEDIUM/LOW"}}"""

    def _call_claude_with_search(self, prompt: str) -> str:
        """Call Claude API with web search tool."""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=4000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
            }],
            messages=[{
                "role": "user",
                "content": prompt
            }]
        )

        # Extract text from response
        text_content = ""
        for block in response.content:
            if hasattr(block, 'text'):
                text_content += block.text

        return text_content

    def _parse_response(
        self,
        response: str,
        condition_id: str,
        question: str,
    ) -> MarketFacts:
        """Parse Claude's response into MarketFacts."""

        facts = MarketFacts(
            condition_id=condition_id,
            market_question=question,
        )

        try:
            # Try to extract JSON from response
            # Handle potential markdown code blocks
            json_text = response
            if "```json" in response:
                json_text = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                json_text = response.split("```")[1].split("```")[0]

            json_match = re.search(r'\{[\s\S]*\}', json_text)
            if json_match:
                data = json.loads(json_match.group())

                facts.key_facts = data.get("key_facts", [])
                facts.current_status = data.get("current_status", "")
                facts.progress_indicator = data.get("progress_indicator", "")
                facts.data_quality = data.get("data_quality", "UNKNOWN")
            else:
                # Fallback: use the response as current_status
                facts.current_status = response[:500]
                facts.data_quality = "LOW"

        except json.JSONDecodeError:
            facts.current_status = response[:500]
            facts.data_quality = "LOW"

        return facts

    async def gather_batch(
        self,
        markets: list[dict],
        max_concurrent: int = 3,
    ) -> dict[str, MarketFacts]:
        """Gather facts for multiple markets concurrently."""

        semaphore = asyncio.Semaphore(max_concurrent)
        results = {}

        async def gather_one(market: dict) -> tuple[str, MarketFacts]:
            async with semaphore:
                condition_id = market.get("conditionId", market.get("condition_id", ""))
                question = market.get("question", market.get("title", ""))
                description = market.get("description", "")
                end_date = market.get("endDate", market.get("end_date", ""))

                facts = await self.gather_facts(
                    condition_id=condition_id,
                    market_question=question,
                    market_description=description,
                    end_date=end_date,
                )
                return condition_id, facts

        tasks = [gather_one(m) for m in markets]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for result in completed:
            if isinstance(result, Exception):
                print(f"Error in batch gather: {result}")
                continue
            condition_id, facts = result
            results[condition_id] = facts

        return results

    def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        if self.cache:
            return self.cache.get_stats()
        return {"enabled": False}

    def clear_cache(self):
        """Clear the facts cache."""
        if self.cache:
            self.cache.clear_all()


# For testing
if __name__ == "__main__":
    import asyncio

    async def test():
        gatherer = FactsGatherer()

        print("First call (should hit API):")
        facts = await gatherer.gather_facts(
            condition_id="test",
            market_question="Will Bitcoin price be above 100000 USD in January 2026?",
            end_date="2026-01-31T00:00:00Z",
        )
        print(f"  Status: {facts.current_status}")
        print(f"  From cache: {facts.from_cache}")

        print("\nSecond call (should hit cache):")
        facts2 = await gatherer.gather_facts(
            condition_id="test",
            market_question="Will Bitcoin price be above 100000 USD in January 2026?",
        )
        print(f"  Status: {facts2.current_status}")
        print(f"  From cache: {facts2.from_cache}")

        print("\nCache stats:", gatherer.get_cache_stats())

    asyncio.run(test())
