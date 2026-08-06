"""Storage + search backends for the brain."""

from droid_brain.storage.base import SearchBackend, SearchResults
from droid_brain.storage.sqlite_backend import SQLiteBackend

__all__ = ["SearchBackend", "SearchResults", "SQLiteBackend", "get_backend"]


def get_backend(config):
    """Construct the search backend named in a BrainConfig."""
    name = config.search.backend
    if name == "sqlite":
        return SQLiteBackend(config.db_path(), config)
    if name == "opensearch":
        from droid_brain.storage.opensearch_backend import OpenSearchBackend

        return OpenSearchBackend(config)
    raise ValueError(f"unknown search backend: {name!r}")
