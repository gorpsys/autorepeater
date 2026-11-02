"""Schema-neutral JSON reading that rejects ambiguous object keys."""
import json
from pathlib import Path


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key!r}')
        result[key] = value
    return result


def read_config_document(path: str | Path) -> object:
    """Read one UTF-8 document, rejecting duplicate keys at every depth."""
    with open(path, encoding='utf-8') as config_file:
        return json.load(config_file, object_pairs_hook=_unique_object)
