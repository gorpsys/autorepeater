"""Schema-neutral catalog discovery and exact-name selection."""
from dataclasses import dataclass
from pathlib import Path

from autorepeater.strategy_contract import UnsupportedSourceError


@dataclass
class Candidate:
    """One parsed document or its diagnostic, without assuming a config schema."""
    path: Path
    name: str | None
    config: object
    error: ValueError | None


def discover_candidates(paths, read_document, validate_document):
    """Read each path once; retain readable names even when validation fails."""
    candidates = []
    for path in paths:
        name = None
        config = None
        error = None
        try:
            document = read_document(path)
            if isinstance(document, dict) and isinstance(document.get('name'), str):
                name = document['name']
            config = validate_document(document)
        except (OSError, ValueError) as cause:
            label = f'{path} ({name})' if name is not None else str(path)
            error = ValueError(f'{label}: {cause}')
        candidates.append(Candidate(path, name, config, error))
    return candidates


def _duplicate_message(name, candidates):
    paths = ', '.join(str(candidate.path) for candidate in candidates)
    return f'duplicate strategy name: {name} ({paths})'


def select_candidate(candidates, name, warn):
    """Warn about foreign problems in order, then return one exact valid match."""
    catalog = {}
    for candidate in candidates:
        if candidate.name is not None:
            catalog.setdefault(candidate.name, []).append(candidate)
        if candidate.error is not None and candidate.name != name:
            warn(str(candidate.error))
    for candidate_name, matches in catalog.items():
        if candidate_name != name and len(matches) > 1:
            warn(_duplicate_message(candidate_name, matches))
    matches = catalog.get(name, []) if isinstance(name, str) else []
    if not matches:
        problems = ', '.join(str(item.path) for item in candidates if item.error is not None)
        detail = f'; problematic files: {problems}' if problems else ''
        raise UnsupportedSourceError(f'unsupported src: {name}{detail}')
    if len(matches) != 1:
        raise ValueError(_duplicate_message(name, matches))
    if matches[0].error is not None:
        raise matches[0].error
    return matches[0].config
