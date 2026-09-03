import logging
import sys

_logging_initialized = False


def setup_logging():
    global _logging_initialized
    if _logging_initialized:
        return

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        '{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}'
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    _logging_initialized = True
