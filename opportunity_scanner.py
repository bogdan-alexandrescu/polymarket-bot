"""
Opportunity Scanner - Finds high-probability profit opportunities.

Analyzes markets expiring within 24hrs to find conservative bets
with at least 3% profit potential.

Enhanced with:
- Claude AI analysis for intelligent market assessment
- Historical price data and trend analysis
- Related market detection and correlation risk
- Web research for real-world verification
"""

import asyncio
import aiohttp
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import json
import re

from polymarket_client import PolymarketClient
from scanner_config import ScannerConfig, RISK_WEIGHTS, RISK_WEIGHTS_NO_CLAUDE, RISK_PROFILE_THRESHOLDS
from log_manager import get_logger

# Loggers
logger = get_logger('scanner')
deep_research_logger = get_logger('deep_research')


@dataclass
class MarketOpportunity:
    """Represents a trading opportunity."""
    # Market info (required fields first)
    condition_id: str
    title: str
    event_title: str
    slug: str

    # Position info
    recommended_side: str  # "YES" or "NO"
    token_id: str
    entry_price: float
    expected_resolution: float  # 0.0 or 1.0
    expected_profit_pct: float

    # Risk analysis
    confidence_score: float  # 0-1
    risk_score: float  # 0-1 (lower = less risk)
    liquidity: float
    spread: float
    volume_24h: float

    # Fields with defaults below this line
    description: str = ""
    end_date: datetime = None
    hours_to_expiry: float = 0.0

    # News/sentiment (legacy)
    news_summary: str = ""
    sentiment_score: float = 0.5  # 0=negative, 0.5=neutral, 1=positive for NO bet
    triggering_event_detected: bool = False

    # Claude AI analysis
    claude_probability: float = 0.5      # Claude's probability estimate (0-1)
    claude_confidence: float = 0.5       # Claude's confidence in estimate (0-1)
    claude_recommendation: str = "SKIP"  # BUY_YES, BUY_NO, SKIP
    claude_reasoning: str = ""           # Explanation
    claude_edge: float = 0.0             # Edge vs market price (-1 to +1)
    claude_risk_factors: list = field(default_factory=list)

    # Historical analysis
    price_trend: str = "STABLE"          # UP, DOWN, STABLE
    price_volatility: float = 0.0        # 0-1 volatility score

    # Cross-market correlation
    related_markets: list = field(default_factory=list)
    correlation_risk: float = 0.0        # 0-1 risk from correlated positions

    # Web research
    web_context: str = ""                # Summary of web findings
    event_status: str = "UNKNOWN"        # OCCURRED, NOT_OCCURRED, UNKNOWN

    # Deep research (comprehensive web-based analysis)
    deep_research_summary: str = ""      # Executive summary from deep research
    deep_research_probability: float = 0.5  # Probability from deep research
    deep_research_quality: str = "NONE"  # NONE, LOW, MEDIUM, HIGH
    key_facts: list = field(default_factory=list)  # Important facts found
    recent_news: list = field(default_factory=list)  # Recent news headlines
    expert_opinions: list = field(default_factory=list)  # Expert views
    contrary_evidence: list = field(default_factory=list)  # Counter-arguments
    research_sentiment: str = "NEUTRAL"  # POSITIVE, NEGATIVE, NEUTRAL, MIXED

    # Real-time facts (from FactsGatherer)
    research_facts: list = field(default_factory=list)  # List of {"fact": str, "value": str, "source": str}
    research_status: str = ""            # Current status summary
    research_progress: str = ""          # Progress indicator (e.g., "15/20 tweets")
    facts_quality: str = "UNKNOWN"       # HIGH, MEDIUM, LOW, UNKNOWN
    facts_gathered_at: str = ""          # Timestamp when facts were gathered

    # Triage status (for cost-saving filters before deep research)
    triage_status: str = "PENDING"       # PENDING, PASSED, FILTERED
    triage_reasons: list = field(default_factory=list)  # Reasons if filtered

    # AI analysis status
    ai_analysis_skipped: bool = False    # True if skipped due to max_ai limit
    preliminary_score: float = 0.0       # Score used to prioritize AI analysis (0-1)

    # Sizing
    recommended_amount: float = 0.0
    potential_profit: float = 0.0

    def __str__(self):
        claude_info = ""
        if self.claude_recommendation != "SKIP":
            claude_info = (
                f"\n  Claude: {self.claude_recommendation} "
                f"(prob: {self.claude_probability*100:.0f}%, "
                f"edge: {self.claude_edge*100:+.1f}%)"
            )

        return (
            f"{self.title}\n"
            f"  Side: {self.recommended_side} @ {self.entry_price*100:.1f}%\n"
            f"  Expected Profit: {self.expected_profit_pct*100:.1f}%\n"
            f"  Confidence: {self.confidence_score*100:.0f}%\n"
            f"  Risk Score: {self.risk_score*100:.0f}% (lower=better)\n"
            f"  Expires in: {self.hours_to_expiry:.1f}h\n"
            f"  Liquidity: ${self.liquidity:,.0f}"
            f"{claude_info}"
        )


class OpportunityScanner:
    """Scans markets for high-probability profit opportunities."""

    def __init__(self, client: PolymarketClient, config: ScannerConfig = None):
        self.client = client
        self.config = config or ScannerConfig()
        self.opportunities: list[MarketOpportunity] = []

        # Lazy-load optional modules
        self._market_analyzer = None
        self._data_enricher = None
        self._web_researcher = None

    @property
    def market_analyzer(self):
        """Lazy-load MarketAnalyzer."""
        if self._market_analyzer is None and self.config.enable_claude_analysis:
            try:
                from market_analyzer import MarketAnalyzer
                self._market_analyzer = MarketAnalyzer(model=self.config.claude_model)
            except Exception as e:
                print(f"Warning: Could not load MarketAnalyzer: {e}")
        return self._market_analyzer

    @property
    def data_enricher(self):
        """Lazy-load DataEnricher."""
        if self._data_enricher is None and (
            self.config.enable_historical_analysis or self.config.enable_related_markets
        ):
            try:
                from data_enricher import DataEnricher
                self._data_enricher = DataEnricher(
                    lookback_hours=self.config.news_lookback_hours,
                )
            except Exception as e:
                print(f"Warning: Could not load DataEnricher: {e}")
        return self._data_enricher

    @property
    def web_researcher(self):
        """Lazy-load WebResearcher."""
        if self._web_researcher is None and self.config.enable_web_research:
            try:
                from web_researcher import WebResearcher
                self._web_researcher = WebResearcher()
            except Exception as e:
                print(f"Warning: Could not load WebResearcher: {e}")
        return self._web_researcher

    @property
    def deep_researcher(self):
        """Lazy-load DeepMarketAnalyzer for comprehensive research."""
        if not hasattr(self, '_deep_researcher'):
            self._deep_researcher = None
        if self._deep_researcher is None and self.config.enable_deep_research:
            try:
                from deep_researcher import DeepMarketAnalyzer
                self._deep_researcher = DeepMarketAnalyzer(
                    research_model=self.config.deep_research_model,
                    analysis_model=self.config.deep_research_model,
                )
            except Exception as e:
                print(f"Warning: Could not load DeepMarketAnalyzer: {e}")
        return self._deep_researcher

    @property
    def facts_gatherer(self):
        """Lazy-load FactsGatherer for real-time facts."""
        if not hasattr(self, '_facts_gatherer'):
            self._facts_gatherer = None
        if self._facts_gatherer is None and getattr(self.config, 'enable_facts_gathering', True):
            try:
                from facts_gatherer import FactsGatherer
                self._facts_gatherer = FactsGatherer(
                    model=getattr(self.config, 'facts_model', 'claude-sonnet-4-20250514'),
                    cache_ttl_hours=getattr(self.config, 'cache_ttl_hours', 2.0),
                    enable_cache=getattr(self.config, 'cache_enabled', True),
                    rate_limit_per_minute=getattr(self.config, 'api_rate_limit_per_minute', 50),
                )
            except Exception as e:
                print(f"Warning: Could not load FactsGatherer: {e}")
        return self._facts_gatherer

    async def get_cash_balance(self) -> float:
        """Get current cash balance from proxy wallet."""
        try:
            from onchain import OnchainClient
            onchain = OnchainClient()
            # Use proxy wallet for balance
            wallet = self.client.proxy_wallet or self.client.address
            if wallet:
                return onchain.get_usdc_balance(wallet)
        except:
            pass
        return 0.0

    async def get_portfolio_value(self) -> float:
        """Get total portfolio value (cash + positions)."""
        cash = await self.get_cash_balance()
        positions = await self.client.get_positions()
        positions_value = sum(p.get('currentValue', 0) for p in positions)
        return cash + positions_value

    async def fetch_expiring_markets(self) -> list[dict]:
        """Fetch all markets expiring within the configured timeframe."""
        now = datetime.now(timezone.utc)
        min_end = now + timedelta(hours=self.config.min_hours_to_expiry)
        max_end = now + timedelta(hours=self.config.max_hours_to_expiry)

        async with aiohttp.ClientSession() as session:
            # Fetch events with markets (order by volume to get active markets)
            async with session.get(
                "https://gamma-api.polymarket.com/events",
                params={
                    "closed": "false",
                    "limit": 1000,
                    "order": "volume",
                    "ascending": "false",
                },
            ) as resp:
                events = await resp.json()

        expiring_markets = []
        for event in events:
            for market in event.get("markets", []):
                if market.get("closed"):
                    continue

                end_date_str = market.get("endDate")
                if not end_date_str:
                    continue

                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                except:
                    continue

                # Check if within our time window
                if min_end <= end_date <= max_end:
                    market["_event_title"] = event.get("title", "")
                    market["_event_description"] = event.get("description", "") or market.get("description", "")
                    market["_end_date"] = end_date
                    market["_hours_to_expiry"] = (end_date - now).total_seconds() / 3600
                    expiring_markets.append(market)

        return expiring_markets

    def analyze_liquidity(self, token_id: str) -> dict:
        """Analyze order book liquidity for a token."""
        try:
            book = self.client.get_order_book(token_id)

            # Calculate total liquidity near the top of book (USD value)
            ask_liquidity = sum(float(o.size) * float(o.price) for o in (book.asks or [])[:5])
            bid_liquidity = sum(float(o.size) * float(o.price) for o in (book.bids or [])[:5])

            best_ask = float(book.asks[0].price) if book.asks else 1.0
            best_bid = float(book.bids[0].price) if book.bids else 0.0
            spread = best_ask - best_bid

            # For prediction markets, spread_pct should reflect actual trading cost
            # Use a simpler metric: how tight is the market around the fair value
            # For buying, what matters is: best_ask vs. where we expect to exit (1.0 for winners)
            #
            # If best_bid > 0.10, we have a reasonable two-sided market
            # Otherwise, calculate based on ask depth (price impact)
            if best_bid >= 0.10 and best_ask <= 0.90:
                # Two-sided market - use traditional spread
                midpoint = (best_ask + best_bid) / 2
                spread_pct = spread / midpoint if midpoint > 0 else 1.0
            else:
                # One-sided or extreme market - for buying, spread is minimal
                # if there's liquidity at a reasonable price
                # Use distance from 1.0 (exit price) as proxy
                # e.g., if best_ask is 0.95, spread is 5% (we pay 0.95 to get 1.00)
                spread_pct = max(0, 1.0 - best_ask) if best_ask < 1.0 else 1.0

            return {
                "ask_liquidity": ask_liquidity,
                "bid_liquidity": bid_liquidity,
                "total_liquidity": ask_liquidity + bid_liquidity,
                "best_ask": best_ask,
                "best_bid": best_bid,
                "spread": spread,
                "spread_pct": spread_pct,
            }
        except Exception as e:
            return {
                "ask_liquidity": 0,
                "bid_liquidity": 0,
                "total_liquidity": 0,
                "best_ask": 1.0,
                "best_bid": 0.0,
                "spread": 1.0,
                "spread_pct": 1.0,
            }

    async def analyze_news_sentiment(self, market_title: str, event_title: str) -> dict:
        """Analyze news and sentiment for a market."""
        if not self.config.enable_news_analysis:
            return {"summary": "", "sentiment": 0.5, "event_detected": False}

        # Extract key terms from title
        search_query = f"{event_title} {market_title}"
        # Remove common words and dates
        search_query = re.sub(r'\b(by|before|after|will|the|in|on|at|\d{4})\b', '', search_query, flags=re.I)
        search_query = search_query.strip()[:100]

        try:
            async with aiohttp.ClientSession() as session:
                # Use a news API or web search
                # For now, we'll use DuckDuckGo instant answers as a simple check
                async with session.get(
                    "https://api.duckduckgo.com/",
                    params={"q": search_query, "format": "json", "no_html": "1"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        abstract = data.get("Abstract", "")
                        headline = data.get("Heading", "")

                        # Simple sentiment analysis based on keywords
                        text = f"{headline} {abstract}".lower()

                        # Check for triggering event keywords
                        trigger_keywords = ["confirmed", "happened", "occurred", "struck", "attacked",
                                          "announced", "declared", "broke out", "launched"]
                        event_detected = any(kw in text for kw in trigger_keywords)

                        # Check for negative outcome keywords (good for NO bets)
                        negative_keywords = ["denied", "no evidence", "unlikely", "failed", "rejected",
                                           "postponed", "cancelled", "not expected"]
                        negative_score = sum(1 for kw in negative_keywords if kw in text)

                        sentiment = 0.5 + (negative_score * 0.1)  # Higher = more confident in NO
                        sentiment = min(1.0, sentiment)

                        return {
                            "summary": abstract[:200] if abstract else headline[:200],
                            "sentiment": sentiment,
                            "event_detected": event_detected,
                        }
        except:
            pass

        return {"summary": "", "sentiment": 0.5, "event_detected": False}

    def calculate_expected_profit(
        self,
        entry_price: float,
        expected_resolution: float,
        side: str,
    ) -> float:
        """Calculate expected profit percentage."""
        if side == "YES":
            # Buy YES at entry_price, expect to resolve at expected_resolution
            profit = expected_resolution - entry_price
        else:  # NO
            # Buy NO at entry_price, expect to resolve at expected_resolution
            profit = expected_resolution - entry_price

        return profit / entry_price if entry_price > 0 else 0

    def calculate_risk_score(
        self,
        hours_to_expiry: float,
        liquidity: float,
        price_distance: float,  # Distance from 0 or 100%
        sentiment_score: float,
        volume_24h: float,
        spread_pct: float,
    ) -> float:
        """
        Calculate risk score (0-1, lower = less risk).

        Uses weighted factors based on risk mode.
        """
        weights = RISK_WEIGHTS.get(self.config.risk_mode, RISK_WEIGHTS["conservative"])

        # Normalize factors to 0-1 (where 1 = good/low risk)
        time_score = min(1.0, hours_to_expiry / 24)  # More time = better
        liquidity_score = min(1.0, liquidity / 10000)  # More liquidity = better
        price_score = 1.0 - price_distance  # Closer to 0/100 = better
        sentiment_factor = sentiment_score  # Higher sentiment = better for NO
        volume_score = min(1.0, volume_24h / 100000)  # More volume = better
        spread_score = max(0, 1.0 - spread_pct * 10)  # Lower spread = better

        # Weighted average (higher = lower risk)
        confidence = (
            weights["time_to_expiry"] * time_score +
            weights["liquidity"] * liquidity_score +
            weights["price_distance"] * price_score +
            weights["news_sentiment"] * sentiment_factor +
            weights["volume"] * volume_score +
            weights["spread"] * spread_score
        )

        # Convert to risk score (lower = better)
        risk_score = 1.0 - confidence

        return risk_score

    async def analyze_market(self, market: dict) -> Optional[MarketOpportunity]:
        """Analyze a single market for opportunity."""
        try:
            # Parse basic market info
            condition_id = market.get("conditionId", "")
            title = market.get("question", "")
            event_title = market.get("_event_title", "")
            slug = market.get("slug", "")
            description = market.get("_event_description", "") or market.get("description", "")
            end_date = market.get("_end_date")
            hours_to_expiry = market.get("_hours_to_expiry", 0)

            # Parse prices
            prices_str = market.get("outcomePrices", "[]")
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
            yes_price = float(prices[0]) if len(prices) > 0 else 0.5
            no_price = float(prices[1]) if len(prices) > 1 else 0.5

            # Parse token IDs
            tokens_str = market.get("clobTokenIds", "[]")
            tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
            yes_token = tokens[0] if len(tokens) > 0 else ""
            no_token = tokens[1] if len(tokens) > 1 else ""

            # Get volume
            volume_24h = float(market.get("volume24hr", 0))

            # For conservative strategy: prefer NO bets on events unlikely to happen
            # If YES price is low (<50%), the market thinks event is unlikely
            # We bet NO expecting it to resolve to $1

            if yes_price < 0.50:
                # Market thinks event is unlikely - bet NO
                recommended_side = "NO"
                token_id = no_token
                entry_price = no_price
                expected_resolution = 1.0  # NO resolves to $1 if event doesn't happen
                price_distance = 1.0 - no_price  # Distance from 100%
            else:
                # Market thinks event is likely - could bet YES
                # But for conservative mode, we skip high-uncertainty markets
                if self.config.risk_mode == "conservative" and 0.40 < yes_price < 0.60:
                    return None  # Skip uncertain markets
                recommended_side = "YES"
                token_id = yes_token
                entry_price = yes_price
                expected_resolution = 1.0
                price_distance = 1.0 - yes_price

            # Skip if no valid token
            if not token_id:
                return None

            # Analyze liquidity
            liquidity_info = self.analyze_liquidity(token_id)

            # Skip if insufficient liquidity
            if liquidity_info["total_liquidity"] < self.config.min_liquidity:
                return None

            # Skip if spread too wide
            if liquidity_info["spread_pct"] > self.config.max_spread_pct:
                return None

            # Calculate expected profit
            expected_profit = self.calculate_expected_profit(
                entry_price, expected_resolution, recommended_side
            )

            # Skip if profit below threshold
            if expected_profit < self.config.min_profit_pct:
                return None

            # Analyze news/sentiment
            news_info = await self.analyze_news_sentiment(title, event_title)

            # If triggering event detected, skip (too risky for conservative)
            if news_info["event_detected"] and self.config.risk_mode == "conservative":
                return None

            # Calculate risk score
            risk_score = self.calculate_risk_score(
                hours_to_expiry=hours_to_expiry,
                liquidity=liquidity_info["total_liquidity"],
                price_distance=price_distance,
                sentiment_score=news_info["sentiment"],
                volume_24h=volume_24h,
                spread_pct=liquidity_info["spread_pct"],
            )

            # Calculate confidence (inverse of risk)
            confidence_score = 1.0 - risk_score

            # Skip if confidence too low
            if confidence_score < self.config.min_confidence_score:
                return None

            return MarketOpportunity(
                condition_id=condition_id,
                title=title,
                event_title=event_title,
                slug=slug,
                description=description,
                end_date=end_date,
                hours_to_expiry=hours_to_expiry,
                recommended_side=recommended_side,
                token_id=token_id,
                entry_price=entry_price,
                expected_resolution=expected_resolution,
                expected_profit_pct=expected_profit,
                confidence_score=confidence_score,
                risk_score=risk_score,
                liquidity=liquidity_info["total_liquidity"],
                spread=liquidity_info["spread_pct"],
                volume_24h=volume_24h,
                news_summary=news_info["summary"],
                sentiment_score=news_info["sentiment"],
                triggering_event_detected=news_info["event_detected"],
            )

        except Exception as e:
            print(f"Error analyzing market: {e}")
            return None

    async def scan(self) -> list[MarketOpportunity]:
        """Scan all markets and return ranked opportunities."""
        result = await self.scan_with_stats()
        return result['opportunities']

    async def scan_with_stats(self) -> dict:
        """Scan all markets and return ranked opportunities with stats."""
        logger.info("Starting opportunity scan...")
        print("Scanning for opportunities...")

        stats = {
            'markets_fetched': 0,
            'markets_analyzed': 0,
            'filtered_liquidity': 0,
            'filtered_spread': 0,
            'filtered_profit': 0,
            'filtered_confidence': 0,
            'filtered_uncertain': 0,
            'filtered_event': 0,
            'filtered_claude': 0,
            'opportunities_found': 0,
            'claude_analyzed': 0,
            'enriched': 0,
        }

        # 1. Fetch expiring markets
        logger.info(f"Fetching markets expiring in {self.config.min_hours_to_expiry}-{self.config.max_hours_to_expiry}h")
        print(f"  Fetching markets expiring in {self.config.min_hours_to_expiry}-{self.config.max_hours_to_expiry}h...")
        markets = await self.fetch_expiring_markets()
        stats['markets_fetched'] = len(markets)
        logger.info(f"Found {len(markets)} markets in timeframe")
        print(f"  Found {len(markets)} markets in timeframe")

        # 2. Pre-filter by basic criteria (liquidity, spread)
        logger.info("Pre-filtering markets by liquidity and spread...")
        print("  Pre-filtering markets...")
        candidates = await self._pre_filter_markets(markets, stats)
        logger.info(f"{len(candidates)} candidates passed pre-filter")
        print(f"  {len(candidates)} candidates after pre-filter")

        if not candidates:
            self.opportunities = []
            return {'opportunities': [], 'stats': stats}

        # 3. Enrich with additional data (historical, related markets)
        if self.data_enricher:
            logger.info("Enriching with historical data...")
            print("  Enriching with historical data...")
            candidates = await self.data_enricher.enrich_batch(
                candidates,
                max_concurrent=5,
            )
            stats['enriched'] = len(candidates)
            logger.info(f"Enriched {len(candidates)} candidates")

        # 4. Web research for context
        if self.web_researcher:
            print("  Researching web context...")
            research_results = await self.web_researcher.research_batch(
                candidates,
                max_concurrent=5,
            )
            # Attach research to markets
            for market in candidates:
                cond_id = market.get("conditionId", "")
                if cond_id in research_results:
                    market["_web_research"] = research_results[cond_id]

        # 5. Claude AI analysis (limited by max_ai_analysis to control costs)
        claude_analyses = {}
        max_ai = getattr(self.config, 'max_ai_analysis', 10)
        if self.market_analyzer:
            # Sort candidates by preliminary score (most promising first)
            # This ensures skipped markets have lower confidence potential
            candidates.sort(key=lambda m: self._calculate_preliminary_score(m), reverse=True)

            # Limit to top N candidates for AI analysis
            candidates_for_ai = candidates[:max_ai]
            candidates_skipped = candidates[max_ai:]

            # Mark skipped candidates with lower base confidence
            for market in candidates_skipped:
                market["_skipped_ai_analysis"] = True
                market["_preliminary_score"] = self._calculate_preliminary_score(market)

            logger.info(f"Running Claude AI analysis on {len(candidates_for_ai)}/{len(candidates)} markets")
            print(f"  Running Claude AI analysis on {len(candidates_for_ai)}/{len(candidates)} markets (max_ai={max_ai})...")

            # Prepare markets with needed fields
            for market in candidates_for_ai:
                self._prepare_market_for_claude(market)

            claude_analyses = await self.market_analyzer.analyze_markets_batch(
                candidates_for_ai,
                max_concurrent=self.config.claude_max_concurrent,
            )
            stats['claude_analyzed'] = len(claude_analyses)
            stats['claude_skipped'] = len(candidates) - len(candidates_for_ai)

        # 6. Create opportunities with enhanced scoring
        logger.info("Scoring opportunities...")
        print("  Scoring opportunities...")
        opportunities = []
        for market in candidates:
            condition_id = market.get("conditionId", "")
            analysis = claude_analyses.get(condition_id)
            opp = await self._create_enhanced_opportunity(market, analysis, stats)
            if opp:
                opportunities.append(opp)

        stats['markets_analyzed'] = len(candidates)
        stats['opportunities_found'] = len(opportunities)
        logger.info(f"Found {len(opportunities)} opportunities meeting criteria")
        print(f"  Found {len(opportunities)} opportunities meeting criteria")

        # Initialize triage stats
        stats['triage_low_volume'] = 0
        stats['triage_low_confidence'] = 0
        stats['triage_low_edge'] = 0
        stats['triage_resolved'] = 0
        stats['triage_passed'] = 0

        # 7. Gather real-time facts for opportunities (limited by max_ai_analysis)
        if self.facts_gatherer and opportunities:
            opportunities.sort(key=lambda x: (x.risk_score, -x.expected_profit_pct))

            # Limit facts gathering to top N opportunities
            opps_for_facts = opportunities[:max_ai]
            print(f"  Gathering real-time facts for {len(opps_for_facts)}/{len(opportunities)} opportunities (max_ai={max_ai})...")
            facts_results = await self._gather_facts(opps_for_facts)
            stats['facts_gathered'] = len(facts_results)
            stats['facts_skipped'] = len(opportunities) - len(opps_for_facts)

            # Apply facts to opportunities
            for opp in opportunities:
                if opp.condition_id in facts_results:
                    self._apply_facts(opp, facts_results[opp.condition_id])

        # 8. Apply triage filters (always run - useful for quick scan too)
        if opportunities:
            print("  Applying triage filters...")
            triaged_opportunities = []

            for opp in opportunities:
                triage_result = self._apply_triage_filters(opp, stats)
                if triage_result['passed']:
                    opp.triage_status = "PASSED"
                    opp.triage_reasons = []
                    triaged_opportunities.append(opp)
                else:
                    opp.triage_status = "FILTERED"
                    opp.triage_reasons = triage_result['reasons']

            stats['triage_passed'] = len(triaged_opportunities)
            logger.info(f"{len(triaged_opportunities)}/{len(opportunities)} passed triage filters")
            print(f"  {len(triaged_opportunities)}/{len(opportunities)} passed triage filters")

            # 9. Deep research only for triaged candidates (when enabled)
            if self.config.enable_deep_research:
                if not self.deep_researcher:
                    deep_research_logger.warning("Deep research enabled but researcher not available")
                elif not triaged_opportunities:
                    deep_research_logger.warning(f"No opportunities passed triage filters (0/{len(opportunities)}) - skipping deep research")
                    logger.warning("No opportunities passed triage - skipping deep research")
                else:
                    triaged_opportunities.sort(key=lambda x: (x.risk_score, -x.expected_profit_pct))
                    top_for_research = triaged_opportunities[:self.config.deep_research_top_n]

                    logger.info(f"Running deep research on {len(top_for_research)} triaged opportunities")
                    deep_research_logger.info(f"Running deep research on {len(top_for_research)} triaged opportunities")
                    print(f"  Running deep research on {len(top_for_research)} triaged opportunities...")
                    deep_results = await self._run_deep_research(top_for_research)
                    stats['deep_researched'] = len(deep_results)

                    # Update opportunities with deep research results
                    for opp in opportunities:
                        if opp.condition_id in deep_results:
                            self._apply_deep_research(opp, deep_results[opp.condition_id])

        # 10. Adjust for correlations
        if self.config.enable_related_markets:
            opportunities = self._adjust_for_correlations(opportunities)

        # 8. Sort by risk score (lowest first) then by profit potential
        opportunities.sort(key=lambda x: (x.risk_score, -x.expected_profit_pct))

        # Calculate position sizing
        portfolio_value = await self.get_portfolio_value()
        cash_balance = await self.get_cash_balance()

        if self.config.fixed_amount is not None:
            max_per_position = self.config.fixed_amount
            max_allocation = self.config.fixed_amount * len(opportunities)
        else:
            max_allocation = portfolio_value * self.config.total_allocation_pct
            max_per_position = cash_balance * self.config.max_position_pct

        allocated = 0
        for opp in opportunities:
            if self.config.fixed_amount is not None:
                opp.recommended_amount = self.config.fixed_amount
                opp.potential_profit = self.config.fixed_amount * opp.expected_profit_pct
                allocated += self.config.fixed_amount
            elif allocated >= max_allocation:
                opp.recommended_amount = 0
            else:
                amount = min(max_per_position, max_allocation - allocated)
                opp.recommended_amount = amount
                opp.potential_profit = amount * opp.expected_profit_pct
                allocated += amount

        self.opportunities = opportunities
        return {
            'opportunities': opportunities,
            'stats': stats,
        }

    async def _pre_filter_markets(
        self,
        markets: list[dict],
        stats: dict,
    ) -> list[dict]:
        """Pre-filter markets by basic criteria before expensive analysis."""
        candidates = []

        # Limit markets to analyze (liquidity check is slow - 1 API call per market)
        max_markets_to_analyze = getattr(self.config, 'max_markets_to_analyze', 50)
        if len(markets) > max_markets_to_analyze:
            # Sort by volume and take top N
            markets = sorted(markets, key=lambda m: float(m.get('volume', 0) or 0), reverse=True)[:max_markets_to_analyze]
            print(f"  Limited to top {max_markets_to_analyze} markets by volume for liquidity analysis")

        # Get thresholds for current risk mode
        thresholds = RISK_PROFILE_THRESHOLDS.get(
            self.config.risk_mode,
            RISK_PROFILE_THRESHOLDS["moderate"]
        )

        min_liquidity = thresholds.get("min_liquidity", self.config.min_liquidity)
        max_spread = thresholds.get("max_spread_pct", self.config.max_spread_pct)
        min_profit = thresholds.get("min_profit_pct", self.config.min_profit_pct)
        uncertain_range = thresholds.get("skip_uncertain_range")

        for market in markets:
            # Parse prices
            prices_str = market.get("outcomePrices", "[]")
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
            yes_price = float(prices[0]) if len(prices) > 0 else 0.5
            no_price = float(prices[1]) if len(prices) > 1 else 0.5

            # Store parsed prices
            market["_yes_price"] = yes_price
            market["_no_price"] = no_price

            # Parse token IDs
            tokens_str = market.get("clobTokenIds", "[]")
            tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
            market["_yes_token"] = tokens[0] if len(tokens) > 0 else ""
            market["_no_token"] = tokens[1] if len(tokens) > 1 else ""

            # Volume
            market["_volume_24h"] = float(market.get("volume24hr", 0))

            # Skip uncertain markets based on risk profile
            if uncertain_range:
                low, high = uncertain_range
                if low < yes_price < high:
                    stats['filtered_uncertain'] += 1
                    continue

            # Determine which side to bet - always pick the side closer to 1.0
            # This gives us the best expected value
            if yes_price < 0.50:
                token_id = market["_no_token"]
                market["_recommended_side"] = "NO"
                market["_entry_price"] = no_price
            else:
                token_id = market["_yes_token"]
                market["_recommended_side"] = "YES"
                market["_entry_price"] = yes_price

            if not token_id:
                continue

            market["_token_id"] = token_id

            # Quick liquidity check
            liquidity_info = self.analyze_liquidity(token_id)
            market["_liquidity_info"] = liquidity_info
            market["_liquidity"] = liquidity_info["total_liquidity"]

            if liquidity_info["total_liquidity"] < min_liquidity:
                stats['filtered_liquidity'] += 1
                continue

            if liquidity_info["spread_pct"] > max_spread:
                stats['filtered_spread'] += 1
                continue

            # Calculate expected profit
            expected_profit = self.calculate_expected_profit(
                market["_entry_price"], 1.0, market["_recommended_side"]
            )
            market["_expected_profit"] = expected_profit

            if expected_profit < min_profit:
                stats['filtered_profit'] += 1
                continue

            candidates.append(market)

        return candidates

    def _prepare_market_for_claude(self, market: dict):
        """Prepare market dict with fields needed for Claude analysis."""
        # These fields are expected by MarketAnalyzer
        if "_yes_price" not in market:
            prices_str = market.get("outcomePrices", "[]")
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
            market["_yes_price"] = float(prices[0]) if len(prices) > 0 else 0.5
            market["_no_price"] = float(prices[1]) if len(prices) > 1 else 0.5

        if "_volume_24h" not in market:
            market["_volume_24h"] = float(market.get("volume24hr", 0))

        if "_liquidity" not in market:
            market["_liquidity"] = float(market.get("liquidity", 0))

        if "_hours_to_expiry" not in market:
            end_date_str = market.get("endDate")
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    market["_hours_to_expiry"] = (end_date - now).total_seconds() / 3600
                except:
                    market["_hours_to_expiry"] = 24

    async def _create_enhanced_opportunity(
        self,
        market: dict,
        claude_analysis,
        stats: dict,
    ) -> Optional[MarketOpportunity]:
        """Create an opportunity with enhanced data."""
        try:
            # Get thresholds for current risk mode
            thresholds = RISK_PROFILE_THRESHOLDS.get(
                self.config.risk_mode,
                RISK_PROFILE_THRESHOLDS["moderate"]
            )
            min_confidence = thresholds.get("min_confidence_score", self.config.min_confidence_score)
            filter_claude_skip = thresholds.get("filter_claude_skip", False)
            filter_event_occurred = thresholds.get("filter_event_occurred", True)

            condition_id = market.get("conditionId", "")
            title = market.get("question", "")
            event_title = market.get("_event_title", "")
            slug = market.get("slug", "")
            description = market.get("_event_description", "") or market.get("description", "")
            end_date = market.get("_end_date")
            hours_to_expiry = market.get("_hours_to_expiry", 0)

            recommended_side = market.get("_recommended_side", "NO")
            token_id = market.get("_token_id", "")
            entry_price = market.get("_entry_price", 0.5)
            expected_profit = market.get("_expected_profit", 0)

            liquidity_info = market.get("_liquidity_info", {})
            volume_24h = market.get("_volume_24h", 0)

            # Claude analysis data
            claude_probability = 0.5
            claude_confidence = 0.5
            claude_recommendation = "SKIP"
            claude_reasoning = ""
            claude_edge = 0.0
            claude_risk_factors = []

            if claude_analysis:
                claude_probability = claude_analysis.probability_yes / 100.0
                claude_confidence = claude_analysis.confidence / 100.0
                claude_recommendation = claude_analysis.recommendation
                claude_reasoning = claude_analysis.reasoning
                claude_edge = claude_analysis.edge_estimate / 100.0
                claude_risk_factors = claude_analysis.risk_factors or []

                # Only filter Claude SKIP if risk profile says to
                if (filter_claude_skip and
                    claude_recommendation == "SKIP" and
                    claude_confidence > 0.7):
                    stats['filtered_claude'] += 1
                    return None

            # Historical data
            price_trend = market.get("_price_trend", "STABLE")
            price_volatility = market.get("_price_volatility", 0.0)

            # Related markets
            related_markets = market.get("_related_markets", [])
            correlation_risk = market.get("_correlation_risk", 0.0)

            # Web research
            web_research = market.get("_web_research", {})
            web_context = web_research.get("summary", "")
            event_status = web_research.get("event_status", "UNKNOWN")

            # Only filter occurred events if risk profile says to
            if filter_event_occurred and event_status == "OCCURRED":
                stats['filtered_event'] += 1
                return None

            # Calculate risk score with enhanced factors
            risk_score = self._calculate_enhanced_risk_score(
                hours_to_expiry=hours_to_expiry,
                liquidity=liquidity_info.get("total_liquidity", 0),
                price_distance=1.0 - entry_price,
                sentiment_score=0.5,
                volume_24h=volume_24h,
                spread_pct=liquidity_info.get("spread_pct", 0),
                claude_confidence=claude_confidence,
                claude_edge=abs(claude_edge),
                price_trend=price_trend,
                recommended_side=recommended_side,
                correlation_risk=correlation_risk,
            )

            confidence_score = 1.0 - risk_score

            if confidence_score < min_confidence:
                stats['filtered_confidence'] += 1
                return None

            # Check if this market was skipped for AI analysis
            ai_skipped = market.get("_skipped_ai_analysis", False)
            prelim_score = market.get("_preliminary_score", 0.0)

            return MarketOpportunity(
                condition_id=condition_id,
                title=title,
                event_title=event_title,
                slug=slug,
                description=description,
                end_date=end_date,
                hours_to_expiry=hours_to_expiry,
                recommended_side=recommended_side,
                token_id=token_id,
                entry_price=entry_price,
                expected_resolution=1.0,
                expected_profit_pct=expected_profit,
                confidence_score=confidence_score,
                risk_score=risk_score,
                liquidity=liquidity_info.get("total_liquidity", 0),
                spread=liquidity_info.get("spread_pct", 0),
                volume_24h=volume_24h,
                news_summary=web_context,
                sentiment_score=0.5,
                triggering_event_detected=(event_status == "OCCURRED"),
                # Claude analysis
                claude_probability=claude_probability,
                claude_confidence=claude_confidence,
                claude_recommendation=claude_recommendation,
                claude_reasoning=claude_reasoning,
                claude_edge=claude_edge,
                claude_risk_factors=claude_risk_factors,
                # Historical
                price_trend=price_trend,
                price_volatility=price_volatility,
                # Correlation
                related_markets=related_markets,
                correlation_risk=correlation_risk,
                # Web research
                web_context=web_context,
                event_status=event_status,
                # AI analysis status
                ai_analysis_skipped=ai_skipped,
                preliminary_score=prelim_score,
            )

        except Exception as e:
            print(f"Error creating opportunity: {e}")
            return None

    def _calculate_enhanced_risk_score(
        self,
        hours_to_expiry: float,
        liquidity: float,
        price_distance: float,
        sentiment_score: float,
        volume_24h: float,
        spread_pct: float,
        claude_confidence: float = 0.5,
        claude_edge: float = 0.0,
        price_trend: str = "STABLE",
        recommended_side: str = "NO",
        correlation_risk: float = 0.0,
    ) -> float:
        """Calculate risk score with enhanced factors."""
        # Choose weights based on whether Claude analysis is enabled
        if self.config.enable_claude_analysis:
            weights = RISK_WEIGHTS.get(self.config.risk_mode, RISK_WEIGHTS["conservative"])
        else:
            weights = RISK_WEIGHTS_NO_CLAUDE.get(
                self.config.risk_mode,
                RISK_WEIGHTS_NO_CLAUDE["conservative"]
            )

        # Normalize base factors to 0-1 (where 1 = good/low risk)
        time_score = min(1.0, hours_to_expiry / 24)
        liquidity_score = min(1.0, liquidity / 10000)
        price_score = 1.0 - price_distance
        sentiment_factor = sentiment_score
        volume_score = min(1.0, volume_24h / 100000)
        spread_score = max(0, 1.0 - spread_pct * 10)

        # Start with base weighted average
        confidence = (
            weights.get("time_to_expiry", 0) * time_score +
            weights.get("liquidity", 0) * liquidity_score +
            weights.get("price_distance", 0) * price_score +
            weights.get("news_sentiment", 0) * sentiment_factor +
            weights.get("volume", 0) * volume_score +
            weights.get("spread", 0) * spread_score
        )

        # Add Claude-based factors if available
        if self.config.enable_claude_analysis:
            # Claude confidence (higher = better)
            claude_conf_score = claude_confidence
            confidence += weights.get("claude_confidence", 0) * claude_conf_score

            # Claude edge (higher = better opportunity)
            claude_edge_score = min(1.0, claude_edge / 0.20)  # 20% edge = max score
            confidence += weights.get("claude_edge", 0) * claude_edge_score

            # Price trend alignment (trend matches our bet = good)
            trend_score = 0.5  # Neutral default
            if price_trend == "UP" and recommended_side == "YES":
                trend_score = 1.0
            elif price_trend == "DOWN" and recommended_side == "NO":
                trend_score = 1.0
            elif price_trend == "STABLE":
                trend_score = 0.7
            confidence += weights.get("price_trend", 0) * trend_score

            # Correlation risk (lower = better)
            corr_score = 1.0 - correlation_risk
            confidence += weights.get("correlation_risk", 0) * corr_score

        # Convert to risk score (lower = better)
        risk_score = 1.0 - confidence
        return max(0.0, min(1.0, risk_score))

    def _adjust_for_correlations(
        self,
        opportunities: list[MarketOpportunity],
    ) -> list[MarketOpportunity]:
        """Adjust opportunity scores based on cross-market correlations."""
        if len(opportunities) < 2:
            return opportunities

        # Group by related markets
        # If multiple opportunities share related markets, increase their risk

        for i, opp1 in enumerate(opportunities):
            related_ids = {m.get("conditionId") for m in opp1.related_markets}

            for j, opp2 in enumerate(opportunities):
                if i >= j:
                    continue

                # Check if opp2 is related to opp1
                if opp2.condition_id in related_ids:
                    # Increase risk for the lower-ranked opportunity
                    opp2.correlation_risk = min(1.0, opp2.correlation_risk + 0.1)
                    opp2.risk_score = min(1.0, opp2.risk_score + 0.05)

        return opportunities

    async def _run_deep_research(
        self,
        opportunities: list[MarketOpportunity],
    ) -> dict[str, dict]:
        """Run deep research on opportunities."""
        if not self.deep_researcher:
            deep_research_logger.warning("Deep researcher not available")
            return {}

        deep_research_logger.info(f"Starting deep research on {len(opportunities)} markets...")

        # Convert opportunities to market dicts for the researcher
        markets = []
        for opp in opportunities:
            deep_research_logger.info(f"  - {opp.title[:60]}...")
            markets.append({
                "conditionId": opp.condition_id,
                "question": opp.title,
                "description": "",
                "_event_title": opp.event_title,
                "endDate": opp.end_date.isoformat() if opp.end_date else "",
                "_yes_price": 1.0 - opp.entry_price if opp.recommended_side == "NO" else opp.entry_price,
                "_no_price": opp.entry_price if opp.recommended_side == "NO" else 1.0 - opp.entry_price,
                "_volume_24h": opp.volume_24h,
                "_liquidity": opp.liquidity,
                "_hours_to_expiry": opp.hours_to_expiry,
            })

        results = await self.deep_researcher.analyze_batch(
            markets,
            max_concurrent=self.config.deep_research_max_concurrent,
        )
        deep_research_logger.info(f"Deep research complete: {len(results)} markets analyzed")
        return results

    def _apply_deep_research(
        self,
        opp: MarketOpportunity,
        research_result: dict,
    ):
        """Apply deep research results to an opportunity."""
        research = research_result.get("research", {})
        analysis = research_result.get("analysis", {})

        # Update opportunity with research findings
        opp.deep_research_summary = research.get("executive_summary", "")
        opp.deep_research_probability = research.get("research_probability", 0.5)
        opp.deep_research_quality = research.get("research_quality", "NONE")
        opp.key_facts = research.get("key_facts", [])[:5]
        opp.recent_news = research.get("recent_news", [])[:3]
        opp.expert_opinions = research.get("expert_opinions", [])[:3]
        opp.contrary_evidence = research.get("contrary_evidence", [])[:3]
        opp.research_sentiment = research.get("sentiment", "NEUTRAL")

        # Update event status based on research
        if research.get("event_occurred"):
            opp.event_status = "OCCURRED"
            opp.triggering_event_detected = True

        # Update Claude analysis with research-backed values
        final_prob = research_result.get("final_probability", 0.5)
        final_conf = research_result.get("final_confidence", 0.5)
        recommendation = research_result.get("recommendation", "SKIP")
        edge = research_result.get("edge", 0)
        reasoning = research_result.get("reasoning", "")

        # Only update if we got meaningful research
        if opp.deep_research_quality in ("MEDIUM", "HIGH"):
            opp.claude_probability = final_prob
            opp.claude_confidence = final_conf
            opp.claude_recommendation = recommendation
            opp.claude_edge = edge
            opp.claude_reasoning = reasoning

            # Recalculate risk score with new information
            if recommendation == "SKIP" or opp.event_status == "OCCURRED":
                opp.risk_score = min(1.0, opp.risk_score + 0.2)
            elif recommendation in ("BUY_YES", "BUY_NO") and final_conf > 0.7:
                opp.risk_score = max(0.0, opp.risk_score - 0.1)

            opp.confidence_score = 1.0 - opp.risk_score

    async def _gather_facts(
        self,
        opportunities: list[MarketOpportunity],
    ) -> dict[str, any]:
        """Gather real-time facts for opportunities."""
        if not self.facts_gatherer:
            return {}

        # Convert opportunities to market dicts for the gatherer
        markets = []
        for opp in opportunities:
            markets.append({
                "conditionId": opp.condition_id,
                "question": opp.title,
                "description": "",
                "endDate": opp.end_date.isoformat() if opp.end_date else "",
            })

        return await self.facts_gatherer.gather_batch(
            markets,
            max_concurrent=getattr(self.config, 'facts_max_concurrent', 3),
        )

    def _apply_facts(
        self,
        opp: MarketOpportunity,
        facts,
    ):
        """Apply gathered facts to an opportunity."""
        if facts is None:
            return

        # Apply facts from MarketFacts object
        opp.research_facts = facts.key_facts or []
        opp.research_status = facts.current_status or ""
        opp.research_progress = facts.progress_indicator or ""
        opp.facts_quality = facts.data_quality or "UNKNOWN"
        opp.facts_gathered_at = facts.gathered_at or ""

    def _apply_triage_filters(
        self,
        opp: MarketOpportunity,
        stats: dict,
    ) -> dict:
        """
        Apply cost-saving triage filters before expensive deep research.

        Returns dict with:
            - passed: bool - whether the opportunity passed all filters
            - reasons: list - reasons why it was filtered (if any)
        """
        reasons = []

        # Filter 1: Activity Filter - Skip markets with low 24h volume
        min_volume = getattr(self.config, 'triage_min_volume_24h', 1000)
        if opp.volume_24h < min_volume:
            reasons.append(f"Low volume: ${opp.volume_24h:.0f} < ${min_volume:.0f}")
            stats['triage_low_volume'] += 1

        # Filter 2: Confidence Gate - Skip if Claude's confidence is too low
        min_confidence = getattr(self.config, 'triage_min_confidence', 0.50)
        if opp.claude_confidence < min_confidence:
            reasons.append(f"Low confidence: {opp.claude_confidence*100:.0f}% < {min_confidence*100:.0f}%")
            stats['triage_low_confidence'] += 1

        # Filter 3: Minimum Edge Threshold - Only proceed if meaningful edge vs market
        min_edge = getattr(self.config, 'triage_min_edge_pct', 0.05)
        actual_edge = abs(opp.claude_edge)
        if actual_edge < min_edge:
            reasons.append(f"Low edge: {actual_edge*100:.1f}% < {min_edge*100:.0f}%")
            stats['triage_low_edge'] += 1

        # Filter 4: Facts-Based Gate - Skip if event already resolved
        skip_resolved = getattr(self.config, 'triage_skip_resolved', True)
        if skip_resolved:
            # Check event_status from facts gathering
            if opp.event_status == "OCCURRED":
                reasons.append("Event already occurred")
                stats['triage_resolved'] += 1
            # Also check research_status for resolution indicators
            elif opp.research_status:
                status_lower = opp.research_status.lower()
                resolved_indicators = [
                    "already happened", "has occurred", "confirmed",
                    "announced", "completed", "finished", "resolved",
                    "event took place", "already resolved"
                ]
                for indicator in resolved_indicators:
                    if indicator in status_lower:
                        reasons.append(f"Event appears resolved: {indicator}")
                        stats['triage_resolved'] += 1
                        break

        return {
            'passed': len(reasons) == 0,
            'reasons': reasons,
        }

    async def _analyze_market_with_stats(self, market: dict, stats: dict) -> Optional[MarketOpportunity]:
        """Analyze a single market for opportunity, updating stats."""
        try:
            # Parse basic market info
            condition_id = market.get("conditionId", "")
            title = market.get("question", "")
            event_title = market.get("_event_title", "")
            slug = market.get("slug", "")
            description = market.get("_event_description", "") or market.get("description", "")
            end_date = market.get("_end_date")
            hours_to_expiry = market.get("_hours_to_expiry", 0)

            # Parse prices
            prices_str = market.get("outcomePrices", "[]")
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
            yes_price = float(prices[0]) if len(prices) > 0 else 0.5
            no_price = float(prices[1]) if len(prices) > 1 else 0.5

            # Parse token IDs
            tokens_str = market.get("clobTokenIds", "[]")
            tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
            yes_token = tokens[0] if len(tokens) > 0 else ""
            no_token = tokens[1] if len(tokens) > 1 else ""

            # Get volume
            volume_24h = float(market.get("volume24hr", 0))

            # For conservative strategy: prefer NO bets on events unlikely to happen
            # If YES price is low (<50%), the market thinks event is unlikely
            # We bet NO expecting it to resolve to $1

            if yes_price < 0.50:
                # Market thinks event is unlikely - bet NO
                recommended_side = "NO"
                token_id = no_token
                entry_price = no_price
                expected_resolution = 1.0  # NO resolves to $1 if event doesn't happen
                price_distance = 1.0 - no_price  # Distance from 100%
            else:
                # Market thinks event is likely - could bet YES
                # But for conservative mode, we skip high-uncertainty markets
                if self.config.risk_mode == "conservative" and 0.40 < yes_price < 0.60:
                    stats['filtered_uncertain'] += 1
                    return None  # Skip uncertain markets
                recommended_side = "YES"
                token_id = yes_token
                entry_price = yes_price
                expected_resolution = 1.0
                price_distance = 1.0 - yes_price

            # Skip if no valid token
            if not token_id:
                return None

            # Analyze liquidity
            liquidity_info = self.analyze_liquidity(token_id)

            # Skip if insufficient liquidity
            if liquidity_info["total_liquidity"] < self.config.min_liquidity:
                stats['filtered_liquidity'] += 1
                return None

            # Skip if spread too wide
            if liquidity_info["spread_pct"] > self.config.max_spread_pct:
                stats['filtered_spread'] += 1
                return None

            # Calculate expected profit
            expected_profit = self.calculate_expected_profit(
                entry_price, expected_resolution, recommended_side
            )

            # Skip if profit below threshold
            if expected_profit < self.config.min_profit_pct:
                stats['filtered_profit'] += 1
                return None

            # Analyze news/sentiment
            news_info = await self.analyze_news_sentiment(title, event_title)

            # If triggering event detected, skip (too risky for conservative)
            if news_info["event_detected"] and self.config.risk_mode == "conservative":
                stats['filtered_event'] += 1
                return None

            # Calculate risk score
            risk_score = self.calculate_risk_score(
                hours_to_expiry=hours_to_expiry,
                liquidity=liquidity_info["total_liquidity"],
                price_distance=price_distance,
                sentiment_score=news_info["sentiment"],
                volume_24h=volume_24h,
                spread_pct=liquidity_info["spread_pct"],
            )

            # Calculate confidence (inverse of risk)
            confidence_score = 1.0 - risk_score

            # Skip if confidence too low
            if confidence_score < self.config.min_confidence_score:
                stats['filtered_confidence'] += 1
                return None

            return MarketOpportunity(
                condition_id=condition_id,
                title=title,
                event_title=event_title,
                slug=slug,
                description=description,
                end_date=end_date,
                hours_to_expiry=hours_to_expiry,
                recommended_side=recommended_side,
                token_id=token_id,
                entry_price=entry_price,
                expected_resolution=expected_resolution,
                expected_profit_pct=expected_profit,
                confidence_score=confidence_score,
                risk_score=risk_score,
                liquidity=liquidity_info["total_liquidity"],
                spread=liquidity_info["spread_pct"],
                volume_24h=volume_24h,
                news_summary=news_info["summary"],
                sentiment_score=news_info["sentiment"],
                triggering_event_detected=news_info["event_detected"],
            )

        except Exception as e:
            print(f"Error analyzing market: {e}")
            return None

    def get_recommendations(self, top_n: int = 5) -> list[MarketOpportunity]:
        """Get top N recommendations."""
        return self.opportunities[:top_n]

    def _calculate_preliminary_score(self, market: dict) -> float:
        """
        Calculate a preliminary score for a market before AI analysis.

        This score is used to prioritize which markets get expensive Claude analysis.
        Markets with higher scores are more promising and get analyzed first.

        Factors considered (all normalized to 0-1 and weighted):
        - Price distance from 0 or 1 (closer = more decisive = higher score)
        - Liquidity (higher = more reliable)
        - 24h volume (higher = more active/interesting)
        - Spread (lower = more efficient market)
        - Expected profit (higher = more opportunity)
        - Time to expiry (optimal range preferred)

        Returns:
            float: Score from 0-1, higher = more promising
        """
        try:
            # Extract market data
            yes_price = market.get("_yes_price", 0.5)
            entry_price = market.get("_entry_price", 0.5)
            liquidity = market.get("_liquidity", 0)
            volume_24h = market.get("_volume_24h", 0)
            hours_to_expiry = market.get("_hours_to_expiry", 24)
            liquidity_info = market.get("_liquidity_info", {})
            spread_pct = liquidity_info.get("spread_pct", 0.1)
            expected_profit = market.get("_expected_profit", 0)

            # 1. Price distance score (closer to 0 or 1 = more decisive)
            # e.g., price at 0.95 or 0.05 is more decisive than 0.50
            distance_from_extreme = min(yes_price, 1.0 - yes_price)  # 0 to 0.5
            price_decisiveness = 1.0 - (distance_from_extreme * 2)  # 0 to 1
            # Boost: very high prices (>0.90) or very low (<0.10) get bonus
            if yes_price > 0.90 or yes_price < 0.10:
                price_decisiveness = min(1.0, price_decisiveness + 0.2)

            # 2. Liquidity score (more = better, normalized to $50k)
            liquidity_score = min(1.0, liquidity / 50000)

            # 3. Volume score (more = better, normalized to $100k)
            volume_score = min(1.0, volume_24h / 100000)

            # 4. Spread score (lower = better)
            spread_score = max(0.0, 1.0 - spread_pct * 5)  # 20% spread = 0 score

            # 5. Expected profit score (higher = better, normalized to 50%)
            profit_score = min(1.0, expected_profit / 0.50)

            # 6. Time to expiry score (optimal is 4-24 hours)
            if hours_to_expiry < 1:
                time_score = 0.3  # Too soon, risky
            elif hours_to_expiry < 4:
                time_score = 0.6  # Getting close
            elif hours_to_expiry <= 24:
                time_score = 1.0  # Optimal range
            elif hours_to_expiry <= 48:
                time_score = 0.8  # Still good
            else:
                time_score = 0.5  # Further out

            # Weighted combination
            # Prioritize price decisiveness and liquidity (most predictive of good opportunities)
            score = (
                0.30 * price_decisiveness +  # Most important - decisive markets
                0.25 * liquidity_score +      # Reliable markets
                0.15 * volume_score +          # Active markets
                0.10 * spread_score +          # Efficient markets
                0.10 * profit_score +          # Good opportunity
                0.10 * time_score              # Good timing
            )

            return score

        except Exception as e:
            # On error, return low score so market gets deprioritized
            return 0.1

    async def execute_opportunity(self, opp: MarketOpportunity) -> dict:
        """Execute a trade for an opportunity."""
        if opp.recommended_amount <= 0:
            return {"success": False, "error": "No amount to invest"}

        try:
            size = opp.recommended_amount / opp.entry_price
            result = self.client.place_order(
                token_id=opp.token_id,
                side="buy",
                size=size,
                price=opp.entry_price + self.config.slippage_buffer,
            )
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}
