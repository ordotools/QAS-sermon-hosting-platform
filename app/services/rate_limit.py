import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

_buckets: dict[str, list[float]] = defaultdict(list)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def rate_limit(request: Request, key: str, max_requests: int, window_seconds: int) -> None:
    ip = _client_ip(request)
    bucket_key = f"{key}:{ip}"
    now = time.time()
    window_start = now - window_seconds
    timestamps = [t for t in _buckets[bucket_key] if t > window_start]
    if len(timestamps) >= max_requests:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
        )
    timestamps.append(now)
    _buckets[bucket_key] = timestamps
