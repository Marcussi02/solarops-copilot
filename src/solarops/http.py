"""Small HTTP helper with retries (standard library only)."""

import json
import time
import urllib.error
import urllib.request

USER_AGENT = "solarops-copilot/0.1 (+https://github.com/Marcussi02/solarops-copilot)"


class FetchError(RuntimeError):
    """Raised when a URL cannot be fetched after all retries."""


def get_bytes(
    url: str, retries: int = 3, timeout: int = 20, headers: dict[str, str] | None = None
) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise FetchError(f"GET {url} failed: HTTP {exc.code}") from exc  # not retryable
            last_error = exc
        except Exception as exc:  # network errors, timeouts
            last_error = exc
        if attempt < retries:
            time.sleep(2**attempt)
    raise FetchError(f"GET {url} failed after {retries} attempts: {last_error}")


def get_json(url: str, **kwargs):
    return json.loads(get_bytes(url, **kwargs))
