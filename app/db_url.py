from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

_SSL_QUERY_KEYS = frozenset({"sslmode", "sslrootcert", "sslcert", "sslkey"})


def _strip_ssl_query_params(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    params = parse_qs(parsed.query, keep_blank_values=True)
    filtered = {key: value for key, value in params.items() if key.lower() not in _SSL_QUERY_KEYS}
    query = urlencode(filtered, doseq=True) if filtered else ""
    return urlunparse(parsed._replace(query=query))


def normalize_async_database_url(url: str) -> str:
    """Accept Coolify-style postgres:// URLs for SQLAlchemy async engines."""
    if not url:
        return url
    if url.startswith("postgres://"):
        url = f"postgresql+asyncpg://{url[len('postgres://'):]}"
    elif url.startswith("postgresql://"):
        url = f"postgresql+asyncpg://{url[len('postgresql://'):]}"
    return _strip_ssl_query_params(url)


def normalize_sync_database_url(url: str) -> str:
    """Accept Coolify-style postgres:// URLs for Alembic migrations."""
    url = normalize_async_database_url(url)
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://").replace(
        "sqlite+aiosqlite://", "sqlite://"
    )
