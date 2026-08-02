from __future__ import annotations

import tomllib
from pathlib import PurePosixPath
from typing import Any


VALIDATION_STATUS_EXIT_CODES = {
    "passed": 0,
    "rejected": 1,
    "inconclusive": 2,
}


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
        or set(validation) != {"command", "timeout_seconds", "trusted_files"}
        or not isinstance(validation.get("command"), list)
        or not validation["command"]
        or not all(isinstance(item, str) and item for item in validation["command"])
        or not isinstance(validation.get("trusted_files"), list)
        or not validation["trusted_files"]
        or not all(_is_trusted_file(item) for item in validation["trusted_files"])
        or len(set(validation["trusted_files"])) != len(validation["trusted_files"])
        or type(validation.get("timeout_seconds")) is not int
        or validation["timeout_seconds"] <= 0
    ):
        raise ValidationContractError("contract is invalid")
    return validation


def _is_trusted_file(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )
