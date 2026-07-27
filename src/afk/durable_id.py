import re
from typing import Any


DURABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def is_durable_id(value: Any) -> bool:
    return isinstance(value, str) and DURABLE_ID_PATTERN.fullmatch(value) is not None
