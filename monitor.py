"""Market monitoring and automated actions."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Literal
from enum import Enum

from polymarket_client import PolymarketClient
from sms_alerts import SMSAlerter


class TriggerDirection(Enum):
    UP = "up"
    DOWN = "down"
    BOTH = "both"


@dataclass
class PriceAlert:
    """Configuration for a price alert."""
    token_id: str
    market_name: str
    outcome: str  # "YES" or "NO"
    threshold: float  # Percentage change to trigger (e.g., 0.05 = 5%)
    direction: TriggerDirection = TriggerDirection.BOTH
    last_price: float = 0.0
    triggered: bool = False
    cooldown: int = 300  # Seconds between repeat alerts
    last_triggered: float = 0.0


@dataclass
class AutoTrade:
    """Configuration for automated trading."""
    token_id: str
    market_name: str
    outcome: str
    trigger_price: float  # Price at which to trigger
    direction: Literal["above", "below"]  # Trigger when price goes above/below
    action: Literal["buy", "sell"]
    amount: float  # Dollar amount or token amount
    limit_price: Optional[float] = None  # If None, use market order
    one_shot: bool = True  # Execute only once
    executed: bool = False


@dataclass
class MonitoredMarket:
    """A market being monitored."""
    condition_id: str
    name: str
    yes_token_id: str
    no_token_id: str
    alerts: list[PriceAlert] = field(default_factory=list)
    auto_trades: list[AutoTrade] = field(default_factory=list)
    last_yes_price: float = 0.0
    last_no_price: float = 0.0


class MarketMonitor:
    """Monitor markets and execute automated actions."""

    def __init__(
        self,
        client: PolymarketClient,
        alerter: SMSAlerter = None,
        poll_interval: float = 5.0,
    ):
        self.client = client
        self.alerter = alerter or SMSAlerter()
        self.poll_interval = poll_interval
        self.markets: dict[str, MonitoredMarket] = {}
        self.running = False
        self._callbacks: list[Callable] = []

    def add_market(
        self,
        condition_id: str,
        name: str,
        yes_token_id: str,
        no_token_id: str,
    ) -> MonitoredMarket:
        """Add a market to monitor."""
        market = MonitoredMarket(
            condition_id=condition_id,
            name=name,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        )
        self.markets[condition_id] = market
        return market

    def add_price_alert(
        self,
        condition_id: str,
        outcome: Literal["YES", "NO"],
        threshold: float,
        direction: TriggerDirection = TriggerDirection.BOTH,
        cooldown: int = 300,
    ) -> PriceAlert:
        """Add a price alert to a monitored market."""
        market = self.markets.get(condition_id)
        if not market:
            raise ValueError(f"Market {condition_id} not being monitored")

        token_id = market.yes_token_id if outcome == "YES" else market.no_token_id
        alert = PriceAlert(
            token_id=token_id,
            market_name=market.name,
            outcome=outcome,
            threshold=threshold,
            direction=direction,
            cooldown=cooldown,
        )
        market.alerts.append(alert)
        return alert

    def add_auto_trade(
        self,
        condition_id: str,
        outcome: Literal["YES", "NO"],
        trigger_price: float,
        direction: Literal["above", "below"],
        action: Literal["buy", "sell"],
        amount: float,
        limit_price: float = None,
        one_shot: bool = True,
    ) -> AutoTrade:
        """Add an automated trade trigger."""
        market = self.markets.get(condition_id)
        if not market:
            raise ValueError(f"Market {condition_id} not being monitored")

        token_id = market.yes_token_id if outcome == "YES" else market.no_token_id
        trade = AutoTrade(
            token_id=token_id,
            market_name=market.name,
            outcome=outcome,
            trigger_price=trigger_price,
            direction=direction,
            action=action,
            amount=amount,
            limit_price=limit_price,
            one_shot=one_shot,
        )
        market.auto_trades.append(trade)
        return trade

    def on_price_change(self, callback: Callable):
        """Register callback for price changes: callback(market, outcome, old, new)."""
        self._callbacks.append(callback)

    def _check_alert(self, alert: PriceAlert, current_price: float) -> bool:
        """Check if alert should trigger."""
        if alert.last_price == 0:
            alert.last_price = current_price
            return False

        now = time.time()
        if now - alert.last_triggered < alert.cooldown:
            return False

        change = current_price - alert.last_price
        pct_change = abs(change / alert.last_price) if alert.last_price > 0 else 0

        should_trigger = False
        if pct_change >= alert.threshold:
            if alert.direction == TriggerDirection.BOTH:
                should_trigger = True
            elif alert.direction == TriggerDirection.UP and change > 0:
                should_trigger = True
            elif alert.direction == TriggerDirection.DOWN and change < 0:
                should_trigger = True

        if should_trigger:
            self.alerter.send_price_alert(
                market_name=alert.market_name,
                outcome=alert.outcome,
                old_price=alert.last_price,
                new_price=current_price,
                threshold=alert.threshold,
            )
            alert.last_triggered = now
            alert.last_price = current_price
            return True

        return False

    def _check_auto_trade(self, trade: AutoTrade, current_price: float) -> bool:
        """Check if auto trade should execute."""
        if trade.executed and trade.one_shot:
            return False

        should_execute = False
        if trade.direction == "above" and current_price >= trade.trigger_price:
            should_execute = True
        elif trade.direction == "below" and current_price <= trade.trigger_price:
            should_execute = True

        if should_execute:
            try:
                if trade.action == "buy":
                    if trade.limit_price:
                        result = self.client.place_order(
                            trade.token_id, "buy",
                            trade.amount / trade.limit_price,
                            trade.limit_price,
                        )
                    else:
                        result = self.client.place_market_order(
                            trade.token_id, "buy", trade.amount
                        )
                else:  # sell
                    if trade.limit_price:
                        result = self.client.place_order(
                            trade.token_id, "sell",
                            trade.amount,
                            trade.limit_price,
                        )
                    else:
                        result = self.client.place_market_order(
                            trade.token_id, "sell", trade.amount
                        )

                order_id = result.get("orderID", "")
                self.alerter.send_order_alert(
                    action=f"EXECUTED ({trade.action})",
                    market_name=trade.market_name,
                    outcome=trade.outcome,
                    size=trade.amount,
                    price=trade.limit_price or current_price,
                    order_id=order_id,
                )
                trade.executed = True
                return True

            except Exception as e:
                self.alerter.send_alert(
                    f"⚠️ AUTO-TRADE FAILED\n"
                    f"Market: {trade.market_name}\n"
                    f"Error: {str(e)[:100]}"
                )
                return False

        return False

    async def _poll_market(self, market: MonitoredMarket):
        """Poll a single market for updates."""
        try:
            yes_price = self.client.get_midpoint_price(market.yes_token_id)
            no_price = self.client.get_midpoint_price(market.no_token_id)

            # Notify callbacks
            if market.last_yes_price != yes_price:
                for cb in self._callbacks:
                    try:
                        cb(market, "YES", market.last_yes_price, yes_price)
                    except Exception:
                        pass

            if market.last_no_price != no_price:
                for cb in self._callbacks:
                    try:
                        cb(market, "NO", market.last_no_price, no_price)
                    except Exception:
                        pass

            # Check alerts
            for alert in market.alerts:
                price = yes_price if alert.outcome == "YES" else no_price
                self._check_alert(alert, price)

            # Check auto trades
            for trade in market.auto_trades:
                price = yes_price if trade.outcome == "YES" else no_price
                self._check_auto_trade(trade, price)

            market.last_yes_price = yes_price
            market.last_no_price = no_price

        except Exception as e:
            print(f"[MONITOR ERROR] {market.name}: {e}")

    async def run(self):
        """Start the monitoring loop."""
        self.running = True
        print(f"[MONITOR] Starting - watching {len(self.markets)} markets")

        while self.running:
            tasks = [self._poll_market(m) for m in self.markets.values()]
            await asyncio.gather(*tasks)
            await asyncio.sleep(self.poll_interval)

    def stop(self):
        """Stop the monitoring loop."""
        self.running = False
        print("[MONITOR] Stopped")

    def status(self) -> dict:
        """Get current monitoring status."""
        return {
            "running": self.running,
            "markets": len(self.markets),
            "alerts": sum(len(m.alerts) for m in self.markets.values()),
            "auto_trades": sum(len(m.auto_trades) for m in self.markets.values()),
            "prices": {
                m.name: {"yes": m.last_yes_price, "no": m.last_no_price}
                for m in self.markets.values()
            },
        }
