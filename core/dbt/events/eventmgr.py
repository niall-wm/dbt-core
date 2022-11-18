import betterproto
from colorama import Style
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json
import logging
from logging.handlers import RotatingFileHandler
import threading
from typing import Any, Callable, List, Optional, TextIO
from uuid import uuid4

from dbt.events.base_types import BaseEvent, EventLevel


# A Filter is a function which takes a BaseEvent and returns True if the event
# should be logged, False otherwise.
Filter = Callable[[BaseEvent], bool]


# Default filter which logs every event
def NoFilter(_: BaseEvent) -> bool:
    return True


# A Scrubber removes secrets from an input string, returning a sanitized string.
Scrubber = Callable[[str], str]


# Provide a pass-through scrubber implementation, also used as a default
def NoScrubber(s: str) -> str:
    return s


class LineFormat(Enum):
    PlainText = 1
    DebugText = 2
    Json = 3


# Map from dbt event levels to python log levels
_log_level_map = {
    EventLevel.DEBUG: 10,
    EventLevel.TEST: 10,
    EventLevel.INFO: 20,
    EventLevel.WARN: 30,
    EventLevel.ERROR: 40,
}


@dataclass
class LoggerConfig:
    name: str
    filter: Filter = NoFilter
    scrubber: Scrubber = NoScrubber
    line_format: LineFormat = LineFormat.PlainText
    level: EventLevel = EventLevel.WARN
    use_colors: bool = False
    output_stream: Optional[TextIO] = None
    output_file_name: Optional[str] = None
    logger: Optional[Any] = None


class _Logger:
    def __init__(self):
        self.name: str
        self.filter: Filter
        self.scrubber: Scrubber
        self.level: EventLevel
        self.event_manager: EventManager
        self._python_logger: logging.Logger = None
        self._stream: TextIO

    def create_line(self, e: BaseEvent) -> str:
        ...

    def write_line(self, e: BaseEvent):
        line = self.create_line(e)
        python_level = _log_level_map[e.log_level()]
        if self._python_logger is not None:
            self._python_logger.log(python_level, line)
        elif self._stream is not None and _log_level_map[self.level] <= python_level:
            self._stream.write(line + "\n")

    def flush(self):
        if self._python_logger is not None:
            for handler in self._python_logger.handlers:
                handler.flush()
        elif self._stream is not None:
            self._stream.flush()


class _TextLogger(_Logger):
    def __init__(self):
        super().__init__()
        self.use_colors = True
        self.use_debug_format = False

    def create_line(self, e: BaseEvent) -> str:
        return self.create_debug_line(e) if self.use_debug_format else self.create_info_line(e)

    def create_info_line(self, e: BaseEvent) -> str:
        ts: str = datetime.utcnow().strftime("%H:%M:%S")
        scrubbed_msg: str = self.scrubber(e.message())  # type: ignore
        return f"{self._get_color_tag()}{ts}  {scrubbed_msg}"

    def create_debug_line(self, e: BaseEvent) -> str:
        log_line: str = ""
        # Create a separator if this is the beginning of an invocation
        # TODO: This is an ugly hack, get rid of it if we can
        if type(e).__name__ == "MainReportVersion":
            separator = 30 * "="
            log_line = f"\n\n{separator} {datetime.utcnow()} | {self.event_manager.invocation_id} {separator}\n"
        ts: str = datetime.utcnow().strftime("%H:%M:%S.%f")
        scrubbed_msg: str = self.scrubber(e.message())  # type: ignore
        log_line += f"{self._get_color_tag()}{ts} [{e.log_level():<5}]{self._get_thread_name()} {scrubbed_msg}"
        return log_line

    def _get_color_tag(self) -> str:
        return "" if not self.use_colors else Style.RESET_ALL

    def _get_thread_name(self) -> str:
        thread_name = ""
        if threading.current_thread().name:
            thread_name = threading.current_thread().name
            thread_name = thread_name[:10]
            thread_name = thread_name.ljust(10, " ")
            thread_name = f" [{thread_name}]:"
        return thread_name


class _JsonLogger(_Logger):
    def create_line(self, e: BaseEvent) -> str:
        event_dict = self.event_to_dict(e)
        raw_log_line = json.dumps(event_dict, sort_keys=True)
        line = self.scrubber(raw_log_line)  # type: ignore
        return line

    def event_to_dict(self, event: BaseEvent) -> dict:
        event_dict = dict()
        try:
            # We could use to_json here, but it wouldn't sort the keys.
            # The 'to_json' method just does json.dumps on the dict anyway.
            event_dict = event.to_dict(casing=betterproto.Casing.SNAKE, include_default_values=True)  # type: ignore
        except AttributeError as exc:
            event_type = type(event).__name__
            raise Exception(f"type {event_type} is not serializable. {str(exc)}")
        return event_dict


# Factory function which creates a logger from a config, hiding the gross details.
def _create_logger(config: LoggerConfig):
    logger: _Logger
    if config.line_format == LineFormat.Json:
        logger = _JsonLogger()
    else:
        logger = _TextLogger()
        logger.use_colors = config.use_colors
        logger.use_debug_format = config.line_format == LineFormat.DebugText

    logger.name = config.name
    logger.filter = config.filter
    logger.scrubber = config.scrubber
    logger.level = config.level

    if config.logger:
        logger._python_logger = config.logger
    elif config.output_stream:
        logger._stream = config.output_stream
    else:
        log = logging.getLogger(logger.name)
        log.setLevel(_log_level_map[config.level])
        handler = RotatingFileHandler(
            filename=str(config.output_file_name),
            encoding="utf8",
            maxBytes=10 * 1024 * 1024,  # 10 mb
            backupCount=5,
        )

        handler.setFormatter(logging.Formatter(fmt="%(message)s"))
        log.handlers.clear()
        log.addHandler(handler)

        logger._python_logger = log

    return logger


class EventManager:
    def __init__(self):
        self.loggers: List[_Logger] = []
        self.callbacks: List[Callable[[BaseEvent], None]] = []
        self.invocation_id: str = str(uuid4())

    def fire_event(self, e: BaseEvent) -> None:
        for logger in self.loggers:
            if logger.filter(e):  # type: ignore
                logger.write_line(e)

        for callback in self.callbacks:
            callback(e)

    def add_logger(self, config: LoggerConfig):
        logger = _create_logger(config)
        logger.event_manager = self
        self.loggers.append(logger)

    def flush(self):
        for logger in self.loggers:
            logger.flush()
