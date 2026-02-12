"""Centralized logging manager for real-time log streaming with DB persistence."""

import sys
import threading
import time
from datetime import datetime
from collections import deque


class LogBuffer:
    """Thread-safe circular buffer for log entries with DB persistence."""

    def __init__(self, channel: str, max_entries: int = 500, persist: bool = True):
        self.channel = channel
        self.max_entries = max_entries
        self.buffer = deque(maxlen=max_entries)
        self.lock = threading.Lock()
        self.subscribers = []
        self.persist = persist

        if persist:
            self._load()

    def _load(self):
        """Load recent entries from daemon_logs table."""
        try:
            from db import execute
            cutoff = time.time() - (24 * 3600)
            rows = execute(
                """SELECT channel, timestamp, time, level, message, source
                   FROM daemon_logs
                   WHERE channel = %s AND timestamp > %s
                   ORDER BY timestamp ASC
                   LIMIT %s""",
                (self.channel, cutoff, self.max_entries),
                fetch=True,
            )
            self.buffer = deque(
                [dict(r) for r in rows],
                maxlen=self.max_entries,
            )
        except Exception:
            self.buffer = deque(maxlen=self.max_entries)

    def add(self, entry: dict):
        """Add a log entry to buffer and DB."""
        with self.lock:
            self.buffer.append(entry)
            # Notify subscribers
            for callback in self.subscribers:
                try:
                    callback(entry)
                except:
                    pass

        # Persist to DB (outside lock to reduce contention)
        if self.persist:
            try:
                from db import execute
                execute(
                    """INSERT INTO daemon_logs (channel, timestamp, time, level, message, source)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (self.channel,
                     entry.get('timestamp', time.time()),
                     entry.get('time', ''),
                     entry.get('level', 'INFO'),
                     entry.get('message', ''),
                     entry.get('source', '')),
                )
            except Exception:
                pass

    def get_recent(self, count: int = 100) -> list:
        with self.lock:
            entries = list(self.buffer)
            return entries[-count:] if count < len(entries) else entries

    def get_since(self, timestamp: float) -> list:
        with self.lock:
            return [e for e in self.buffer if e.get('timestamp', 0) > timestamp]

    def clear(self):
        with self.lock:
            self.buffer.clear()
        if self.persist:
            try:
                from db import execute
                execute("DELETE FROM daemon_logs WHERE channel = %s", (self.channel,))
            except Exception:
                pass

    def subscribe(self, callback):
        self.subscribers.append(callback)

    def unsubscribe(self, callback):
        if callback in self.subscribers:
            self.subscribers.remove(callback)


class StreamCapture:
    """Captures stdout/stderr and redirects to log buffer."""

    def __init__(self, log_buffer: LogBuffer, stream_name: str, original_stream):
        self.log_buffer = log_buffer
        self.stream_name = stream_name
        self.original_stream = original_stream
        self.line_buffer = ""

    def write(self, text):
        if self.original_stream:
            self.original_stream.write(text)
            self.original_stream.flush()

        self.line_buffer += text
        while '\n' in self.line_buffer:
            line, self.line_buffer = self.line_buffer.split('\n', 1)
            if line.strip():
                level = self._detect_level(line)
                self.log_buffer.add({
                    'timestamp': time.time(),
                    'time': datetime.now().strftime('%H:%M:%S'),
                    'level': level,
                    'message': line,
                    'source': self.stream_name,
                })

    def _detect_level(self, line: str) -> str:
        line_lower = line.lower()
        if any(indicator in line_lower for indicator in [
            'error', 'exception', 'traceback', 'failed', 'failure',
            'critical', 'fatal', 'crash'
        ]):
            if not any(ok in line_lower for ok in ['0 error', 'no error', 'without error']):
                return 'ERROR'
        if any(indicator in line_lower for indicator in [
            'warning', 'warn', 'deprecated', 'caution'
        ]):
            return 'WARNING'
        if any(indicator in line_lower for indicator in [
            'debug', 'verbose', 'trace'
        ]):
            return 'DEBUG'
        if '" 2' in line or '" 3' in line:
            return 'INFO'
        if '" 4' in line or '" 5' in line:
            return 'WARNING' if '" 4' in line else 'ERROR'
        return 'INFO'

    def flush(self):
        if self.original_stream:
            self.original_stream.flush()


class LogManager:
    """Manages multiple log channels for different components."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True

        # Create log buffers for different components
        self.buffers = {
            'scanner': LogBuffer('scanner', max_entries=500, persist=True),
            'profit_monitor': LogBuffer('profit_monitor', max_entries=500, persist=True),
            'deep_research': LogBuffer('deep_research', max_entries=500, persist=True),
            'copy_trading': LogBuffer('copy_trading', max_entries=500, persist=True),
            'system': LogBuffer('system', max_entries=500, persist=False),
        }

        # Capture stdout/stderr for system logs
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr

        self._stdout_capture = StreamCapture(self.buffers['system'], 'stdout', self._original_stdout)
        self._stderr_capture = StreamCapture(self.buffers['system'], 'stderr', self._original_stderr)

        sys.stdout = self._stdout_capture
        sys.stderr = self._stderr_capture

        # Cleanup old logs (48h)
        self._cleanup_old_logs()

    def _cleanup_old_logs(self):
        """Delete daemon_logs older than 48 hours."""
        try:
            from db import execute
            cutoff = time.time() - (48 * 3600)
            execute("DELETE FROM daemon_logs WHERE timestamp < %s", (cutoff,))
        except Exception:
            pass

    def log(self, channel: str, message: str, level: str = 'INFO', extra: dict = None):
        if channel not in self.buffers:
            channel = 'system'

        entry = {
            'timestamp': time.time(),
            'time': datetime.now().strftime('%H:%M:%S'),
            'level': level,
            'message': message,
            'source': channel,
        }
        if extra:
            entry.update(extra)

        self.buffers[channel].add(entry)

        self._original_stdout.write(f"[{entry['time']}] [{channel.upper()}] {message}\n")
        self._original_stdout.flush()

    def info(self, channel: str, message: str, **kwargs):
        self.log(channel, message, 'INFO', kwargs if kwargs else None)

    def error(self, channel: str, message: str, **kwargs):
        self.log(channel, message, 'ERROR', kwargs if kwargs else None)

    def warning(self, channel: str, message: str, **kwargs):
        self.log(channel, message, 'WARNING', kwargs if kwargs else None)

    def debug(self, channel: str, message: str, **kwargs):
        self.log(channel, message, 'DEBUG', kwargs if kwargs else None)

    def get_logs(self, channel: str, count: int = 100) -> list:
        if channel not in self.buffers:
            return []
        return self.buffers[channel].get_recent(count)

    def get_logs_since(self, channel: str, timestamp: float) -> list:
        if channel not in self.buffers:
            return []
        return self.buffers[channel].get_since(timestamp)

    def get_all_channels(self) -> list:
        return list(self.buffers.keys())

    def clear_channel(self, channel: str):
        if channel in self.buffers:
            self.buffers[channel].clear()


# Global instance
log_manager = LogManager()


def get_logger(channel: str):
    """Get a logger for a specific channel."""
    class ChannelLogger:
        def __init__(self, channel: str):
            self.channel = channel

        def info(self, message: str, **kwargs):
            log_manager.info(self.channel, message, **kwargs)

        def error(self, message: str, **kwargs):
            log_manager.error(self.channel, message, **kwargs)

        def warning(self, message: str, **kwargs):
            log_manager.warning(self.channel, message, **kwargs)

        def debug(self, message: str, **kwargs):
            log_manager.debug(self.channel, message, **kwargs)

    return ChannelLogger(channel)
