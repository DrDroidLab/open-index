"""Storage + search backends for the brain."""

from open_index.storage.base import SearchBackend, SearchResults
from open_index.storage.sqlite_backend import SQLiteBackend

__all__ = ["SearchBackend", "SearchResults", "SQLiteBackend", "get_backend"]


def get_backend(config):
    """Construct the search backend named in a BrainConfig."""
    name = config.search.backend
    if name == "sqlite":
        return SQLiteBackend(config.db_path(), config)
    if name == "opensearch":
        from open_index.storage.opensearch_backend import OpenSearchBackend

        return OpenSearchBackend(config)
    raise ValueError(f"unknown search backend: {name!r}")
