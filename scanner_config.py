"""Configuration for the profit opportunity scanner."""

from dataclasses import dataclass, field
from typing import Optional
import os
from dotenv import load_dotenv

load_dotenv()


@dataclass
class ScannerConfig:
    """Configuration for the opportunity scanner."""

    # Investment parameters
    max_position_pct: float = 0.10  # 10% of cash balance per position
    total_allocation_pct: float = 0.75  # 75% of portfolio for these trades
    min_profit_pct: float = 0.02  # Minimum 2% profit target (lowered from 3%)
    fixed_amount: Optional[float] = None  # Fixed $ amount per position (overrides max_position_pct)

    # Time parameters
    max_hours_to_expiry: int = 48  # Only markets expiring within this time (increased from 24)
    min_hours_to_expiry: int = 0.5  # Avoid markets expiring too soon (lowered to 30min)

    # Risk parameters
    # Modes: "conservative", "moderate", "aggressive", "speculative"
    risk_mode: str = "moderate"  # Changed default to moderate for more opportunities
    min_liquidity: float = 500  # Lowered from 1000 - minimum $ liquidity
    max_spread_pct: float = 0.10  # Increased from 5% to 10% to allow more markets
    min_confidence_score: float = 0.40  # Lowered from 0.70 to show more opportunities

    # Execution parameters
    auto_execute: bool = False  # Require approval by default
    slippage_buffer: float = 0.01  # 1% slippage buffer for calculations

    # News/Sentiment parameters
    enable_news_analysis: bool = True
    news_lookback_hours: int = 24  # How far back to search for news

    # Market filters
    excluded_categories: list = field(default_factory=list)  # Categories to skip
    included_keywords: list = field(default_factory=list)  # Keywords to prioritize

    # Performance limits
    max_markets_to_analyze: int = 50  # Limit markets for liquidity analysis (each is an API call)

    # Claude AI analysis
    enable_claude_analysis: bool = True
    claude_model: str = "claude-haiku-4-20250514"
    claude_max_concurrent: int = 2  # Max parallel API calls (reduced to avoid rate limits)
    max_ai_analysis: int = 10  # Limit how many markets get Claude/AI analysis (cost control)

    # Deep research (uses web search for comprehensive analysis)
    enable_deep_research: bool = False  # Enable for thorough research (slower, more API cost)
    deep_research_model: str = "claude-sonnet-4-20250514"  # Model for deep research
    deep_research_max_concurrent: int = 1  # Sequential to avoid rate limits
    deep_research_top_n: int = 10  # Only deep research top N candidates

    # Data enrichment
    enable_historical_analysis: bool = True
    enable_related_markets: bool = True
    enable_web_research: bool = True  # Basic web research (DuckDuckGo)

    # Real-time facts gathering (uses Claude with web search for market-specific facts)
    enable_facts_gathering: bool = True  # Gather real-time facts for each opportunity
    facts_model: str = "claude-sonnet-4-20250514"  # Model for facts gathering (needs Sonnet for web search)
    facts_max_concurrent: int = 1  # Max parallel facts gathering calls (sequential to avoid rate limits)

    # Triage filters (cost-saving gates before expensive operations)
    triage_min_edge_pct: float = 0.05  # 5% minimum edge to proceed to deep research
    triage_min_confidence: float = 0.50  # 50% minimum Claude confidence
    triage_min_volume_24h: float = 1000  # $1000 minimum 24h volume (activity filter)
    triage_skip_resolved: bool = True  # Skip markets where facts indicate event resolved

    # API caching (reduces costs and avoids rate limits)
    cache_ttl_hours: float = 2.0  # Cache expiration time (default 2 hours)
    cache_enabled: bool = True  # Enable/disable caching
    api_rate_limit_per_minute: int = 8  # Max API requests per minute (conservative for web search)
    api_min_delay_seconds: float = 3.0  # Minimum delay between API requests

    # Correlation limits
    max_correlated_exposure: float = 0.30  # Max 30% portfolio in correlated markets

    @classmethod
    def from_env(cls) -> "ScannerConfig":
        """Load configuration from environment variables."""
        return cls(
            max_position_pct=float(os.getenv("SCANNER_MAX_POSITION_PCT", "0.10")),
            total_allocation_pct=float(os.getenv("SCANNER_TOTAL_ALLOCATION_PCT", "0.75")),
            min_profit_pct=float(os.getenv("SCANNER_MIN_PROFIT_PCT", "0.03")),
            max_hours_to_expiry=int(os.getenv("SCANNER_MAX_HOURS_TO_EXPIRY", "24")),
            risk_mode=os.getenv("SCANNER_RISK_MODE", "conservative"),
            auto_execute=os.getenv("SCANNER_AUTO_EXECUTE", "false").lower() == "true",
            enable_news_analysis=os.getenv("SCANNER_ENABLE_NEWS", "true").lower() == "true",
            enable_claude_analysis=os.getenv("SCANNER_ENABLE_CLAUDE", "true").lower() == "true",
            enable_historical_analysis=os.getenv("SCANNER_ENABLE_HISTORICAL", "true").lower() == "true",
            enable_related_markets=os.getenv("SCANNER_ENABLE_RELATED", "true").lower() == "true",
            enable_web_research=os.getenv("SCANNER_ENABLE_WEB_RESEARCH", "true").lower() == "true",
            enable_deep_research=os.getenv("SCANNER_ENABLE_DEEP_RESEARCH", "false").lower() == "true",
            deep_research_model=os.getenv("SCANNER_DEEP_RESEARCH_MODEL", "claude-sonnet-4-20250514"),
            deep_research_top_n=int(os.getenv("SCANNER_DEEP_RESEARCH_TOP_N", "10")),
        )


# Risk profile thresholds - different modes have different filtering criteria
RISK_PROFILE_THRESHOLDS = {
    "conservative": {
        "min_liquidity": 1000,
        "max_spread_pct": 0.05,
        "min_profit_pct": 0.03,
        "min_confidence_score": 0.60,
        "skip_uncertain_range": (0.40, 0.60),  # Skip markets in this YES price range
        "filter_claude_skip": True,  # Filter out markets where Claude says SKIP
        "filter_event_occurred": True,  # Filter out if event detected
    },
    "moderate": {
        "min_liquidity": 500,
        "max_spread_pct": 0.10,
        "min_profit_pct": 0.02,
        "min_confidence_score": 0.40,
        "skip_uncertain_range": (0.45, 0.55),  # Narrower uncertain range
        "filter_claude_skip": False,  # Show Claude SKIP markets (user decides)
        "filter_event_occurred": True,
    },
    "aggressive": {
        "min_liquidity": 200,
        "max_spread_pct": 0.15,
        "min_profit_pct": 0.01,
        "min_confidence_score": 0.30,
        "skip_uncertain_range": None,  # Don't skip any price range
        "filter_claude_skip": False,
        "filter_event_occurred": False,
    },
    "speculative": {
        "min_liquidity": 100,
        "max_spread_pct": 0.25,
        "min_profit_pct": 0.005,  # 0.5% profit is ok
        "min_confidence_score": 0.20,
        "skip_uncertain_range": None,  # All markets shown
        "filter_claude_skip": False,
        "filter_event_occurred": False,
    },
}

# Risk weights for different factors (used in scoring)
# When Claude analysis is enabled, weights shift to rely more on AI assessment
RISK_WEIGHTS = {
    "conservative": {
        "time_to_expiry": 0.15,       # More time = lower risk
        "liquidity": 0.10,            # More liquidity = lower risk
        "price_distance": 0.10,       # Closer to 0 or 100 = lower risk
        "news_sentiment": 0.05,       # Confirming news = lower risk
        "volume": 0.05,               # Higher volume = lower risk
        "spread": 0.05,               # Lower spread = lower risk
        # Claude-based factors
        "claude_confidence": 0.25,    # How confident Claude is in estimate
        "claude_edge": 0.15,          # Edge vs market price
        "price_trend": 0.05,          # Historical trend alignment
        "correlation_risk": 0.05,     # Cross-market exposure
    },
    "moderate": {
        "time_to_expiry": 0.10,
        "liquidity": 0.10,
        "price_distance": 0.15,
        "news_sentiment": 0.05,
        "volume": 0.05,
        "spread": 0.05,
        "claude_confidence": 0.20,
        "claude_edge": 0.15,
        "price_trend": 0.10,
        "correlation_risk": 0.05,
    },
    "aggressive": {
        "time_to_expiry": 0.05,
        "liquidity": 0.05,
        "price_distance": 0.15,
        "news_sentiment": 0.05,
        "volume": 0.05,
        "spread": 0.05,
        "claude_confidence": 0.25,
        "claude_edge": 0.20,
        "price_trend": 0.10,
        "correlation_risk": 0.05,
    },
    "speculative": {
        "time_to_expiry": 0.05,
        "liquidity": 0.05,
        "price_distance": 0.20,
        "news_sentiment": 0.05,
        "volume": 0.05,
        "spread": 0.05,
        "claude_confidence": 0.20,
        "claude_edge": 0.25,
        "price_trend": 0.05,
        "correlation_risk": 0.05,
    },
}

# Fallback weights when Claude analysis is disabled
RISK_WEIGHTS_NO_CLAUDE = {
    "conservative": {
        "time_to_expiry": 0.25,
        "liquidity": 0.20,
        "price_distance": 0.25,
        "news_sentiment": 0.15,
        "volume": 0.10,
        "spread": 0.05,
    },
    "moderate": {
        "time_to_expiry": 0.15,
        "liquidity": 0.15,
        "price_distance": 0.30,
        "news_sentiment": 0.20,
        "volume": 0.10,
        "spread": 0.10,
    },
    "aggressive": {
        "time_to_expiry": 0.10,
        "liquidity": 0.10,
        "price_distance": 0.35,
        "news_sentiment": 0.25,
        "volume": 0.10,
        "spread": 0.10,
    },
    "speculative": {
        "time_to_expiry": 0.10,
        "liquidity": 0.05,
        "price_distance": 0.40,
        "news_sentiment": 0.20,
        "volume": 0.10,
        "spread": 0.15,
    },
}
