from __future__ import annotations

import tomllib
from typing import Any


class ValidationContractError(ValueError):
    pass


def parse_validation_contract(value: str) -> dict[str, Any]:
    try:
        document = tomllib.loads(value)
    except tomllib.TOMLDecodeError as exc:
        raise ValidationContractError("is missing or invalid") from exc
    validation = document.get("validation")
    if (
        set(document) != {"schema_version", "validation"}
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or not isinstance(validation, dict)
        or set(validation) != {"command", "timeout_seconds"}
        or not isinstance(validation.get("command"), list)
        or not validation["command"]
        or not all(isinstance(item, str) and item for item in validation["command"])
        or type(validation.get("timeout_seconds")) is not int
        or validation["timeout_seconds"] <= 0
    ):
        raise ValidationContractError("contract is invalid")
    return validation
