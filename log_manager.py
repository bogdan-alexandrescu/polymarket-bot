"""Centralized logging manager for real-time log streaming with persistence."""

import os
import sys
import threading
import time
from datetime import datetime
from collections import deque
from typing import Optional
import json
from pathlib import Path


# Storage directory for logs
LOG_STORAGE_DIR = Path(__file__).parent / 'data' / 'logs'


class LogBuffer:
    """Thread-safe circular buffer for log entries with persistence."""

    def __init__(self, channel: str, max_entries: int = 500, persist: bool = True):
        self.channel = channel
        self.max_entries = max_entries
        self.buffer = deque(maxlen=max_entries)
        self.lock = threading.Lock()
        self.subscribers = []
        self.persist = persist
        self._dirty = False
        self._last_save = 0
        self._save_interval = 5  # Save at most every 5 seconds

        # Load existing logs
        if persist:
            self._load()

    def _get_log_file(self) -> Path:
        """Get the log file path for this channel."""
        LOG_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        return LOG_STORAGE_DIR / f"{self.channel}.json"

    def _load(self):
        """Load logs from disk."""
        try:
            log_file = self._get_log_file()
            if log_file.exists():
                with open(log_file, 'r') as f:
                    data = json.load(f)
                    # Filter out old entries (older than 24 hours)
                    cutoff = time.time() - (24 * 3600)
                    entries = [e for e in data if e.get('timestamp', 0) > cutoff]
                    # Only keep max_entries
                    entries = entries[-self.max_entries:]
                    self.buffer = deque(entries, maxlen=self.max_entries)
        except Exception as e:
            # If loading fails, start fresh
            self.buffer = deque(maxlen=self.max_entries)

    def _save(self):
        """Save logs to disk."""
        if not self.persist:
            return
        try:
            log_file = self._get_log_file()
            with open(log_file, 'w') as f:
                json.dump(list(self.buffer), f)
            self._dirty = False
            self._last_save = time.time()
        except Exception as e:
            pass  # Silently fail on save errors

    def _maybe_save(self):
        """Save if dirty and enough time has passed."""
        if self._dirty and (time.time() - self._last_save) > self._save_interval:
            self._save()

    def add(self, entry: dict):
        """Add a log entry."""
        with self.lock:
            self.buffer.append(entry)
            self._dirty = True
            # Notify subscribers
            for callback in self.subscribers:
                try:
                    callback(entry)
                except:
                    pass
            # Periodic save
            self._maybe_save()

    def get_recent(self, count: int = 100) -> list:
        """Get recent log entries."""
        with self.lock:
            entries = list(self.buffer)
            return entries[-count:] if count < len(entries) else entries

    def get_since(self, timestamp: float) -> list:
        """Get entries since a timestamp."""
        with self.lock:
            return [e for e in self.buffer if e.get('timestamp', 0) > timestamp]

    def clear(self):
        """Clear the buffer."""
        with self.lock:
            self.buffer.clear()
            self._dirty = True
            self._save()

    def flush(self):
        """Force save to disk."""
        with self.lock:
            if self._dirty:
                self._save()

    def subscribe(self, callback):
        """Subscribe to new log entries."""
        self.subscribers.append(callback)

    def unsubscribe(self, callback):
        """Unsubscribe from log entries."""
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
        # Write to original stream
        if self.original_stream:
            self.original_stream.write(text)
            self.original_stream.flush()

        # Buffer lines
        self.line_buffer += text
        while '\n' in self.line_buffer:
            line, self.line_buffer = self.line_buffer.split('\n', 1)
            if line.strip():
                # Detect log level from content, not just stream name
                level = self._detect_level(line)
                self.log_buffer.add({
                    'timestamp': time.time(),
                    'time': datetime.now().strftime('%H:%M:%S'),
                    'level': level,
                    'message': line,
                    'source': self.stream_name,
                })

    def _detect_level(self, line: str) -> str:
        """Detect log level from message content."""
        line_lower = line.lower()

        # Check for explicit error indicators
        if any(indicator in line_lower for indicator in [
            'error', 'exception', 'traceback', 'failed', 'failure',
            'critical', 'fatal', 'crash'
        ]):
            # But not "0 errors" or "no error" type messages
            if not any(ok in line_lower for ok in ['0 error', 'no error', 'without error']):
                return 'ERROR'

        # Check for warnings
        if any(indicator in line_lower for indicator in [
            'warning', 'warn', 'deprecated', 'caution'
        ]):
            return 'WARNING'

        # Check for debug
        if any(indicator in line_lower for indicator in [
            'debug', 'verbose', 'trace'
        ]):
            return 'DEBUG'

        # Flask request logs (from stderr but not errors)
        # Format: "127.0.0.1 - - [date] "METHOD /path HTTP/1.1" STATUS -"
        if '" 2' in line or '" 3' in line:  # 2xx or 3xx status codes
            return 'INFO'
        if '" 4' in line or '" 5' in line:  # 4xx or 5xx status codes
            return 'WARNING' if '" 4' in line else 'ERROR'

        # Default based on stream
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

        # Create log buffers for different components (with persistence)
        self.buffers = {
            'scanner': LogBuffer('scanner', max_entries=500, persist=True),
            'profit_monitor': LogBuffer('profit_monitor', max_entries=500, persist=True),
            'deep_research': LogBuffer('deep_research', max_entries=500, persist=True),
            'copy_trading': LogBuffer('copy_trading', max_entries=500, persist=True),
            'system': LogBuffer('system', max_entries=500, persist=False),  # System logs don't persist
        }

        # Capture stdout/stderr for system logs
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr

        # Create captured streams
        self._stdout_capture = StreamCapture(self.buffers['system'], 'stdout', self._original_stdout)
        self._stderr_capture = StreamCapture(self.buffers['system'], 'stderr', self._original_stderr)

        # Redirect streams
        sys.stdout = self._stdout_capture
        sys.stderr = self._stderr_capture

        # Start background save thread
        self._start_save_thread()

    def _start_save_thread(self):
        """Start a background thread to periodically save logs."""
        def save_loop():
            while True:
                time.sleep(10)  # Save every 10 seconds
                self.flush_all()

        thread = threading.Thread(target=save_loop, daemon=True)
        thread.start()

    def log(self, channel: str, message: str, level: str = 'INFO', extra: dict = None):
        """Log a message to a specific channel."""
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

        # Also print to console
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
        """Get recent logs from a channel."""
        if channel not in self.buffers:
            return []
        return self.buffers[channel].get_recent(count)

    def get_logs_since(self, channel: str, timestamp: float) -> list:
        """Get logs since a timestamp."""
        if channel not in self.buffers:
            return []
        return self.buffers[channel].get_since(timestamp)

    def get_all_channels(self) -> list:
        """Get list of all log channels."""
        return list(self.buffers.keys())

    def clear_channel(self, channel: str):
        """Clear logs for a channel."""
        if channel in self.buffers:
            self.buffers[channel].clear()

    def flush_all(self):
        """Flush all buffers to disk."""
        for buffer in self.buffers.values():
            buffer.flush()


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
