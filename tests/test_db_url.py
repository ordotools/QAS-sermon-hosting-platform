from app.db_url import normalize_async_database_url, normalize_sync_database_url


def test_coolify_postgres_url_for_async():
    url = "postgres://user:pass@ksgggwgwkswgwkoccckoocw:5432/postgres"
    assert (
        normalize_async_database_url(url)
        == "postgresql+asyncpg://user:pass@ksgggwgwkswgwkoccckoocw:5432/postgres"
    )


def test_coolify_postgres_url_strips_ssl_params():
    url = (
        "postgres://user:pass@host:5432/db"
        "?sslmode=verify-full&sslrootcert=/etc/ssl/certs/coolify-ca.crt"
    )
    assert (
        normalize_async_database_url(url) == "postgresql+asyncpg://user:pass@host:5432/db"
    )


def test_coolify_postgres_url_for_alembic():
    url = "postgres://user:pass@host:5432/db"
    assert normalize_sync_database_url(url) == "postgresql+psycopg://user:pass@host:5432/db"
