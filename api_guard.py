"""
API Guard - Prevents repeated failed API calls when credits are exhausted.

Tracks billing/credit errors and prevents further API calls until manually reset
or auto-reset after a cooldown period.
"""

import time
import threading
from typing import Optional


class APIGuard:
    """Guards against repeated failed API calls due to billing/credit issues."""

    _instance = None
    _lock = threading.Lock()

    # Auto-reset after 5 minutes (user might have added credits)
    AUTO_RESET_SECONDS = 300

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._credit_error = False
        self._credit_error_time = 0
        self._credit_error_message = ""
        self._lock = threading.Lock()

    def is_blocked(self) -> bool:
        """Check if API calls should be blocked due to credit errors."""
        with self._lock:
            if not self._credit_error:
                return False

            # Auto-reset after cooldown period
            if time.time() - self._credit_error_time > self.AUTO_RESET_SECONDS:
                self._credit_error = False
                self._credit_error_time = 0
                self._credit_error_message = ""
                return False

            return True

    def get_error_message(self) -> str:
        """Get the credit error message if blocked."""
        with self._lock:
            if self._credit_error:
                remaining = int(self.AUTO_RESET_SECONDS - (time.time() - self._credit_error_time))
                if remaining > 0:
                    return f"{self._credit_error_message} (auto-reset in {remaining}s)"
                return self._credit_error_message
            return ""

    def report_credit_error(self, message: str):
        """Report a credit/billing error to block further calls."""
        with self._lock:
            self._credit_error = True
            self._credit_error_time = time.time()
            self._credit_error_message = message

    def reset(self):
        """Manually reset the guard (e.g., after adding credits)."""
        with self._lock:
            self._credit_error = False
            self._credit_error_time = 0
            self._credit_error_message = ""

    def check_and_raise(self):
        """Check if blocked and raise an exception if so."""
        if self.is_blocked():
            raise CreditExhaustedError(self._credit_error_message)


class CreditExhaustedError(Exception):
    """Raised when API credits are exhausted."""
    pass


def is_credit_error(error) -> bool:
    """
    Check if an error is specifically a credit/billing exhaustion error.

    Only returns True for very specific credit exhaustion messages,
    not general billing or payment errors.
    """
    error_str = str(error).lower()

    # Very specific phrases that indicate credit exhaustion
    credit_exhaustion_phrases = [
        'credit balance is too low',
        'upgrade or purchase credits',
        'insufficient credits',
        'out of credits',
        'no credits remaining',
    ]

    return any(phrase in error_str for phrase in credit_exhaustion_phrases)


# Global instance
api_guard = APIGuard()
