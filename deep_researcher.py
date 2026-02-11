"""
Deep Researcher - Comprehensive research using Claude with web search.

Performs multi-angle research on prediction market topics to gather
real-world information, news, expert opinions, and event verification.
"""

import asyncio
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
import anthropic
from dotenv import load_dotenv

load_dotenv()

# Import cache and rate limiter
from api_cache import get_cache, get_rate_limiter
from log_manager import get_logger

# Deep research logger
logger = get_logger('deep_research')


@dataclass
class DeepResearchResult:
    """Comprehensive research findings for a market."""
    # Core findings
    event_occurred: bool = False
    event_occurred_confidence: float = 0.0
    probability_estimate: float = 0.5

    # Research quality
    research_quality: str = "LOW"  # LOW, MEDIUM, HIGH
    sources_found: int = 0

    # Key information
    key_facts: list[str] = field(default_factory=list)
    recent_news: list[str] = field(default_factory=list)
    expert_opinions: list[str] = field(default_factory=list)
    contrary_evidence: list[str] = field(default_factory=list)

    # Timeline analysis
    relevant_dates: list[str] = field(default_factory=list)
    deadline_analysis: str = ""

    # Sentiment
    overall_sentiment: str = "NEUTRAL"  # POSITIVE, NEGATIVE, NEUTRAL, MIXED
    sentiment_score: float = 0.5  # 0-1

    # Risk assessment
    resolution_risk: str = ""  # Issues with how market might resolve
    information_gaps: list[str] = field(default_factory=list)

    # Summary
    executive_summary: str = ""
    recommendation_rationale: str = ""

    # Raw data
    search_queries_used: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)


class DeepResearcher:
    """
    Performs deep research on prediction market topics using Claude with web search.

    Research angles:
    1. Direct event verification - Has the event occurred?
    2. News analysis - Recent news about the topic
    3. Expert/official sources - What do authorities say?
    4. Timeline analysis - Key dates and deadlines
    5. Contrary evidence - What argues against the prediction?
    6. Historical patterns - Similar past events
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        enable_extended_thinking: bool = False,
        rate_limit_per_minute: int = 6,  # Very conservative for deep research with web search
        max_retries: int = 4,
    ):
        self.client = anthropic.Anthropic()
        self.model = model
        self.enable_extended_thinking = enable_extended_thinking
        self.max_retries = max_retries

        # Use shared rate limiter and cache
        self.rate_limiter = get_rate_limiter(requests_per_minute=rate_limit_per_minute)
        self.cache = get_cache(ttl_hours=2.0)

    async def research_market(
        self,
        condition_id: str,
        title: str,
        description: str = "",
        event_title: str = "",
        end_date: str = "",
        current_yes_price: float = 0.5,
    ) -> DeepResearchResult:
        """
        Perform comprehensive research on a market topic.

        Uses Claude with web search to gather information from multiple angles.
        """
        # Check persistent cache
        cache_key = f"{condition_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H')}"
        cached = self.cache.get("deep_research", cache_key)
        if cached:
            logger.info(f"[Cache hit] {title[:50]}...")
            print(f"  [Cache hit] Deep research for: {title[:50]}...")
            return DeepResearchResult(**cached)

        # Check if rate limited
        if self.rate_limiter.is_rate_limited():
            stats = self.rate_limiter.get_stats()
            logger.warning(f"[Rate limited] Skipping: {title[:50]}... (cooldown: {stats.get('cooldown_remaining', 0):.0f}s)")
            print(f"  [Rate limited] Skipping deep research for: {title[:50]}... (cooldown: {stats.get('cooldown_remaining', 0):.0f}s)")
            return DeepResearchResult()

        # Build the research prompt
        prompt = self._build_research_prompt(
            title=title,
            description=description,
            event_title=event_title,
            end_date=end_date,
            current_yes_price=current_yes_price,
        )

        # Retry with exponential backoff
        logger.info(f"Researching: {title[:60]}...")
        for attempt in range(self.max_retries):
            try:
                # Wait for rate limit slot
                await self.rate_limiter.acquire()

                # Use Claude with web search
                logger.info(f"Calling Claude with web search (attempt {attempt + 1}/{self.max_retries})")
                response = await asyncio.to_thread(
                    lambda: self._call_claude_with_search(prompt)
                )

                # Parse the research results
                result = self._parse_research_response(response)

                # Report success and cache the result
                self.rate_limiter.report_success()
                logger.info(f"Research complete: quality={result.research_quality}, prob={result.probability_estimate*100:.0f}%")

                # Convert to dict for caching (excluding complex fields)
                result_dict = {
                    'event_occurred': result.event_occurred,
                    'event_occurred_confidence': result.event_occurred_confidence,
                    'probability_estimate': result.probability_estimate,
                    'research_quality': result.research_quality,
                    'sources_found': result.sources_found,
                    'key_facts': result.key_facts,
                    'recent_news': result.recent_news,
                    'expert_opinions': result.expert_opinions,
                    'contrary_evidence': result.contrary_evidence,
                    'relevant_dates': result.relevant_dates,
                    'deadline_analysis': result.deadline_analysis,
                    'overall_sentiment': result.overall_sentiment,
                    'sentiment_score': result.sentiment_score,
                    'resolution_risk': result.resolution_risk,
                    'information_gaps': result.information_gaps,
                    'executive_summary': result.executive_summary,
                    'recommendation_rationale': result.recommendation_rationale,
                    'search_queries_used': result.search_queries_used,
                    'sources': result.sources,
                }
                self.cache.set("deep_research", cache_key, result_dict)
                return result

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

                wait_time = (2 ** attempt) * 20  # 20s, 40s, 80s, 160s
                logger.warning(f"Rate limit hit (attempt {attempt + 1}/{self.max_retries}), waiting {wait_time}s")
                print(f"Rate limit (attempt {attempt + 1}/{self.max_retries}) for deep research {title[:30]}..., waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue

            except anthropic.APIStatusError as e:
                if e.status_code >= 500:
                    wait_time = (2 ** attempt) * 10
                    print(f"API error {e.status_code} (attempt {attempt + 1}/{self.max_retries}), waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue
                print(f"Deep research API error for {title[:50]}: {e}")
                return DeepResearchResult()

            except Exception as e:
                logger.error(f"Deep research error: {e}")
                print(f"Deep research error for {title[:50]}: {e}")
                return DeepResearchResult()

        logger.error(f"Rate limit exhausted for: {title[:50]}")
        print(f"Rate limit exhausted for deep research {title[:50]}")
        return DeepResearchResult()

    def _build_research_prompt(
        self,
        title: str,
        description: str,
        event_title: str,
        end_date: str,
        current_yes_price: float,
    ) -> str:
        """Build comprehensive research prompt."""

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        market_probability = f"{current_yes_price * 100:.0f}%"

        return f"""You are an expert research analyst investigating a prediction market. Your goal is to gather comprehensive, factual information to determine the likely outcome.

## PREDICTION MARKET TO RESEARCH

**Question**: {title}
**Event Context**: {event_title}
**Description**: {description or "No additional description"}
**Resolution Date**: {end_date}
**Current Market Price**: {market_probability} YES
**Today's Date**: {today}

## RESEARCH INSTRUCTIONS

Conduct thorough web research to answer these questions:

### 1. EVENT VERIFICATION (Most Important)
- Has this event already occurred or been confirmed?
- Search for recent news (last 7 days) about this specific topic
- Look for official announcements, press releases, or authoritative sources

### 2. CURRENT STATUS
- What is the latest development on this topic?
- Are there any scheduled events, deadlines, or announcements coming up?
- What do official sources say?

### 3. EXPERT ANALYSIS
- What do experts, analysts, or officials say about this?
- Are there any forecasts or predictions from credible sources?
- What's the consensus view?

### 4. CONTRARY EVIDENCE
- What evidence suggests the opposite outcome?
- Are there obstacles or challenges to this happening?
- What could prevent the predicted outcome?

### 5. TIMELINE ANALYSIS
- When is this supposed to happen/resolve?
- Is there enough time for this to occur?
- What are the key dates to watch?

### 6. SENTIMENT ANALYSIS
- What's the overall media/public sentiment?
- Is coverage positive, negative, or mixed?

## OUTPUT FORMAT

After completing your research, provide your findings as a JSON object:

```json
{{
    "event_occurred": <true/false - has the event definitively occurred?>,
    "event_occurred_confidence": <0-1 confidence in the above>,
    "probability_estimate": <0-1 your estimated probability of YES>,

    "research_quality": "<LOW|MEDIUM|HIGH - based on sources found>",
    "sources_found": <number of relevant sources>,

    "key_facts": ["<fact 1>", "<fact 2>", ...],
    "recent_news": ["<news headline 1>", "<news headline 2>", ...],
    "expert_opinions": ["<expert view 1>", ...],
    "contrary_evidence": ["<contrary point 1>", ...],

    "relevant_dates": ["<date: event>", ...],
    "deadline_analysis": "<analysis of timing/deadline>",

    "overall_sentiment": "<POSITIVE|NEGATIVE|NEUTRAL|MIXED>",
    "sentiment_score": <0-1, where 1 = very positive for YES>,

    "resolution_risk": "<any concerns about how this might resolve>",
    "information_gaps": ["<what we don't know>", ...],

    "executive_summary": "<2-3 sentence summary of findings>",
    "recommendation_rationale": "<why you estimate this probability>",

    "search_queries_used": ["<query 1>", "<query 2>", ...]
}}
```

IMPORTANT:
- Be factual and cite specific sources when possible
- Clearly distinguish between confirmed facts and speculation
- If you cannot find relevant information, say so
- Focus on recent information (last 30 days preferred)
- Consider the resolution date when assessing probability"""

    def _call_claude_with_search(self, prompt: str) -> str:
        """Call Claude API with web search enabled."""

        messages = [{"role": "user", "content": prompt}]

        # Build request with web search tool
        request_params = {
            "model": self.model,
            "max_tokens": 4000,  # Reduced from 16000 to save costs
            "tools": [{"type": "web_search_20250305"}],
            "messages": messages,
        }

        # Add extended thinking if enabled (for complex analysis)
        if self.enable_extended_thinking:
            request_params["thinking"] = {
                "type": "enabled",
                "budget_tokens": 10000
            }

        response = self.client.messages.create(**request_params)

        # Extract text content from response
        text_content = ""
        for block in response.content:
            if hasattr(block, 'text'):
                text_content += block.text

        return text_content

    def _parse_research_response(self, response: str) -> DeepResearchResult:
        """Parse Claude's research response into structured result."""

        try:
            # Try to extract JSON from response
            json_match = None

            # Look for JSON block
            if "```json" in response:
                start = response.find("```json") + 7
                end = response.find("```", start)
                if end > start:
                    json_match = response[start:end].strip()
            elif "```" in response:
                start = response.find("```") + 3
                end = response.find("```", start)
                if end > start:
                    json_match = response[start:end].strip()
            elif "{" in response:
                # Try to find JSON object
                start = response.find("{")
                end = response.rfind("}") + 1
                if end > start:
                    json_match = response[start:end]

            if json_match:
                data = json.loads(json_match)

                return DeepResearchResult(
                    event_occurred=data.get("event_occurred", False),
                    event_occurred_confidence=float(data.get("event_occurred_confidence", 0)),
                    probability_estimate=float(data.get("probability_estimate", 0.5)),
                    research_quality=data.get("research_quality", "LOW"),
                    sources_found=int(data.get("sources_found", 0)),
                    key_facts=data.get("key_facts", []),
                    recent_news=data.get("recent_news", []),
                    expert_opinions=data.get("expert_opinions", []),
                    contrary_evidence=data.get("contrary_evidence", []),
                    relevant_dates=data.get("relevant_dates", []),
                    deadline_analysis=data.get("deadline_analysis", ""),
                    overall_sentiment=data.get("overall_sentiment", "NEUTRAL"),
                    sentiment_score=float(data.get("sentiment_score", 0.5)),
                    resolution_risk=data.get("resolution_risk", ""),
                    information_gaps=data.get("information_gaps", []),
                    executive_summary=data.get("executive_summary", ""),
                    recommendation_rationale=data.get("recommendation_rationale", ""),
                    search_queries_used=data.get("search_queries_used", []),
                )

        except json.JSONDecodeError as e:
            print(f"Failed to parse research JSON: {e}")
        except Exception as e:
            print(f"Error parsing research response: {e}")

        # Return default result if parsing fails
        return DeepResearchResult(
            executive_summary="Research parsing failed. Raw response available.",
        )

    async def research_batch(
        self,
        markets: list[dict],
        max_concurrent: int = 3,  # Lower concurrency for deep research
    ) -> dict[str, DeepResearchResult]:
        """Research multiple markets with concurrency limit."""

        semaphore = asyncio.Semaphore(max_concurrent)
        results = {}

        async def research_with_limit(market: dict):
            condition_id = market.get("conditionId", "")
            async with semaphore:
                try:
                    result = await self.research_market(
                        condition_id=condition_id,
                        title=market.get("question", ""),
                        description=market.get("description", ""),
                        event_title=market.get("_event_title", ""),
                        end_date=market.get("endDate", ""),
                        current_yes_price=market.get("_yes_price", 0.5),
                    )
                    results[condition_id] = result
                except Exception as e:
                    print(f"Research failed for {condition_id}: {e}")
                    results[condition_id] = DeepResearchResult()

        tasks = [research_with_limit(m) for m in markets]
        await asyncio.gather(*tasks)

        return results

    def clear_cache(self):
        """Clear the research cache."""
        self.cache = {}


class DeepMarketAnalyzer:
    """
    Enhanced market analyzer that combines deep research with Claude analysis.

    Pipeline:
    1. Deep research to gather facts
    2. Claude analysis informed by research
    3. Combined probability estimate
    """

    def __init__(
        self,
        research_model: str = "claude-sonnet-4-20250514",
        analysis_model: str = "claude-sonnet-4-20250514",
    ):
        self.researcher = DeepResearcher(model=research_model)
        self.client = anthropic.Anthropic()
        self.analysis_model = analysis_model
        self.cache = {}

    async def analyze_with_research(
        self,
        condition_id: str,
        title: str,
        description: str = "",
        event_title: str = "",
        end_date: str = "",
        yes_price: float = 0.5,
        no_price: float = 0.5,
        volume_24h: float = 0,
        liquidity: float = 0,
        hours_to_expiry: float = 24,
    ) -> dict:
        """
        Perform deep research and analysis on a market.

        Returns comprehensive analysis with research backing.
        """
        # Check cache
        cache_key = f"{condition_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H')}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        # Step 1: Deep research
        logger.info(f"Starting deep analysis: {title[:60]}...")
        print(f"  Researching: {title[:50]}...")
        research = await self.researcher.research_market(
            condition_id=condition_id,
            title=title,
            description=description,
            event_title=event_title,
            end_date=end_date,
            current_yes_price=yes_price,
        )

        # Step 2: Analysis with research context
        logger.info(f"Analyzing with context (research quality: {research.research_quality})")
        analysis = await self._analyze_with_context(
            title=title,
            description=description,
            event_title=event_title,
            end_date=end_date,
            yes_price=yes_price,
            no_price=no_price,
            hours_to_expiry=hours_to_expiry,
            research=research,
        )

        # Combine into final result
        result = {
            "condition_id": condition_id,
            "title": title,

            # Research findings
            "research": {
                "event_occurred": research.event_occurred,
                "event_occurred_confidence": research.event_occurred_confidence,
                "research_probability": research.probability_estimate,
                "research_quality": research.research_quality,
                "sources_found": research.sources_found,
                "key_facts": research.key_facts,
                "recent_news": research.recent_news,
                "expert_opinions": research.expert_opinions,
                "contrary_evidence": research.contrary_evidence,
                "executive_summary": research.executive_summary,
                "sentiment": research.overall_sentiment,
                "sentiment_score": research.sentiment_score,
            },

            # Analysis
            "analysis": analysis,

            # Final recommendation
            "final_probability": analysis.get("probability_yes", 50) / 100,
            "final_confidence": analysis.get("confidence", 50) / 100,
            "recommendation": analysis.get("recommendation", "SKIP"),
            "edge": analysis.get("edge_estimate", 0) / 100,
            "reasoning": analysis.get("reasoning", ""),
        }

        # Cache result
        self.cache[cache_key] = result
        return result

    async def _analyze_with_context(
        self,
        title: str,
        description: str,
        event_title: str,
        end_date: str,
        yes_price: float,
        no_price: float,
        hours_to_expiry: float,
        research: DeepResearchResult,
    ) -> dict:
        """Run Claude analysis with research context."""

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Build research context
        research_context = f"""
## RESEARCH FINDINGS

**Research Quality**: {research.research_quality} ({research.sources_found} sources)
**Event Status**: {"OCCURRED" if research.event_occurred else "NOT YET OCCURRED"} (confidence: {research.event_occurred_confidence*100:.0f}%)
**Research Probability Estimate**: {research.probability_estimate*100:.0f}%
**Overall Sentiment**: {research.overall_sentiment}

### Executive Summary
{research.executive_summary}

### Key Facts
{chr(10).join(f"- {fact}" for fact in research.key_facts[:5]) if research.key_facts else "- No key facts found"}

### Recent News
{chr(10).join(f"- {news}" for news in research.recent_news[:3]) if research.recent_news else "- No recent news found"}

### Expert Opinions
{chr(10).join(f"- {opinion}" for opinion in research.expert_opinions[:3]) if research.expert_opinions else "- No expert opinions found"}

### Contrary Evidence
{chr(10).join(f"- {contrary}" for contrary in research.contrary_evidence[:3]) if research.contrary_evidence else "- No contrary evidence found"}

### Timeline
{research.deadline_analysis if research.deadline_analysis else "No specific timeline analysis"}

### Information Gaps
{chr(10).join(f"- {gap}" for gap in research.information_gaps[:3]) if research.information_gaps else "- No major gaps identified"}
"""

        prompt = f"""You are an expert prediction market analyst. Based on the research findings below, provide your final analysis and recommendation.

## MARKET INFORMATION
- **Question**: {title}
- **Event**: {event_title}
- **Description**: {description or "No description"}
- **End Date**: {end_date}
- **Hours Until Resolution**: {hours_to_expiry:.1f}
- **Current YES Price**: {yes_price*100:.1f}%
- **Current NO Price**: {no_price*100:.1f}%
- **Current Date**: {today}

{research_context}

## YOUR TASK

Based on ALL the research above, provide your final analysis:

1. What is the TRUE probability this resolves YES?
2. Is the market price accurate, or is there an edge?
3. What's your recommendation?

Consider:
- If the event has already occurred, probability should be near 100% or 0%
- Weight recent, credible sources more heavily
- Account for information gaps in your confidence level
- Consider contrary evidence

Respond with JSON only:
{{
    "probability_yes": <0-100>,
    "confidence": <0-100>,
    "recommendation": "<BUY_YES|BUY_NO|SKIP>",
    "reasoning": "<2-3 sentences explaining your conclusion>",
    "edge_estimate": <-100 to +100>,
    "key_insight": "<the most important finding from research>"
}}"""

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.client.messages.create(
                    model=self.analysis_model,
                    max_tokens=1000,
                    messages=[{"role": "user", "content": prompt}],
                )
            )

            content = response.content[0].text.strip()

            # Parse JSON
            if "```json" in content:
                start = content.find("```json") + 7
                end = content.find("```", start)
                content = content[start:end].strip()
            elif "```" in content:
                start = content.find("```") + 3
                end = content.find("```", start)
                content = content[start:end].strip()

            return json.loads(content)

        except Exception as e:
            print(f"Analysis with context failed: {e}")
            # Return research-based fallback
            return {
                "probability_yes": research.probability_estimate * 100,
                "confidence": 50,
                "recommendation": "SKIP",
                "reasoning": research.executive_summary or "Analysis failed",
                "edge_estimate": 0,
                "key_insight": research.key_facts[0] if research.key_facts else "",
            }

    async def analyze_batch(
        self,
        markets: list[dict],
        max_concurrent: int = 2,  # Low concurrency due to API costs
    ) -> dict[str, dict]:
        """Analyze multiple markets with deep research."""

        semaphore = asyncio.Semaphore(max_concurrent)
        results = {}

        async def analyze_with_limit(market: dict):
            condition_id = market.get("conditionId", "")
            async with semaphore:
                try:
                    result = await self.analyze_with_research(
                        condition_id=condition_id,
                        title=market.get("question", ""),
                        description=market.get("description", ""),
                        event_title=market.get("_event_title", ""),
                        end_date=market.get("endDate", ""),
                        yes_price=market.get("_yes_price", 0.5),
                        no_price=market.get("_no_price", 0.5),
                        volume_24h=market.get("_volume_24h", 0),
                        liquidity=market.get("_liquidity", 0),
                        hours_to_expiry=market.get("_hours_to_expiry", 24),
                    )
                    results[condition_id] = result
                except Exception as e:
                    print(f"Deep analysis failed for {condition_id}: {e}")

        tasks = [analyze_with_limit(m) for m in markets]
        await asyncio.gather(*tasks)

        return results

    def clear_cache(self):
        """Clear all caches."""
        self.cache = {}
        self.researcher.clear_cache()
