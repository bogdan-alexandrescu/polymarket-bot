"""SMS alerting via Twilio."""

from twilio.rest import Client
import config


class SMSAlerter:
    def __init__(
        self,
        account_sid: str = None,
        auth_token: str = None,
        from_number: str = None,
        to_number: str = None,
    ):
        self.account_sid = account_sid or config.TWILIO_ACCOUNT_SID
        self.auth_token = auth_token or config.TWILIO_AUTH_TOKEN
        self.from_number = from_number or config.TWILIO_PHONE_NUMBER
        self.to_number = to_number or config.ALERT_PHONE_NUMBER
        self.client = None
        self._init_client()

    def _init_client(self):
        if self.account_sid and self.auth_token:
            self.client = Client(self.account_sid, self.auth_token)

    def send_alert(self, message: str, to_number: str = None) -> bool:
        """Send SMS alert. Returns True on success."""
        if not self.client:
            print(f"[SMS DISABLED] {message}")
            return False

        target = to_number or self.to_number
        if not target:
            print(f"[NO RECIPIENT] {message}")
            return False

        try:
            msg = self.client.messages.create(
                body=message[:1600],  # SMS limit
                from_=self.from_number,
                to=target,
            )
            print(f"[SMS SENT] SID: {msg.sid}")
            return True
        except Exception as e:
            print(f"[SMS ERROR] {e}")
            return False

    def send_price_alert(
        self,
        market_name: str,
        outcome: str,
        old_price: float,
        new_price: float,
        threshold: float,
    ):
        direction = "UP" if new_price > old_price else "DOWN"
        change = abs(new_price - old_price) * 100
        message = (
            f"🔔 POLYMARKET ALERT\n"
            f"Market: {market_name[:50]}\n"
            f"Outcome: {outcome}\n"
            f"{direction} {change:.1f}% (threshold: {threshold*100:.1f}%)\n"
            f"Price: {old_price*100:.1f}% → {new_price*100:.1f}%"
        )
        return self.send_alert(message)

    def send_order_alert(
        self,
        action: str,
        market_name: str,
        outcome: str,
        size: float,
        price: float,
        order_id: str = None,
    ):
        message = (
            f"📊 ORDER {action.upper()}\n"
            f"Market: {market_name[:50]}\n"
            f"Outcome: {outcome}\n"
            f"Size: ${size:.2f} @ {price*100:.1f}%"
        )
        if order_id:
            message += f"\nOrder ID: {order_id[:16]}..."
        return self.send_alert(message)
