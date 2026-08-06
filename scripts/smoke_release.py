"""Smoke-test the deployed Immensity API and console without credentials."""

from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url: str, *, follow_redirects: bool = True, timeout: float = 15) -> tuple[int, bytes]:
    opener = build_opener() if follow_redirects else build_opener(NoRedirect)
    try:
        response = opener.open(Request(url, headers={"User-Agent": "immensity-release-smoke"}), timeout=timeout)
        return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except URLError as error:
        raise RuntimeError(f"could not reach {url}: {error.reason}") from error


def check_api(api_url: str, timeout: float) -> None:
    for path, expected_status in (("/health", "ok"), ("/ready", "ready")):
        status, body = request(f"{api_url}{path}", timeout=timeout)
        payload = json.loads(body)
        if status != 200 or payload.get("status") != expected_status:
            raise RuntimeError(f"{path} failed: HTTP {status} {payload}")
        print(f"PASS API {path}")


def check_web(web_url: str, timeout: float) -> None:
    paths = (
        "/api/health",
        "/sign-in",
        "/dashboard/warrooms/discovery",
        "/dashboard/warrooms/problem-breakdown",
        "/dashboard/warrooms/mvp-build",
    )
    for path in paths:
        status, _ = request(f"{web_url}{path}", follow_redirects=False, timeout=timeout)
        if not 200 <= status < 400:
            raise RuntimeError(f"{path} failed: HTTP {status}")
        print(f"PASS WEB {path} ({status})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="Public backend URL")
    parser.add_argument("--web-url", required=True, help="Public console URL")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()

    check_api(args.api_url.rstrip("/"), args.timeout)
    check_web(args.web_url.rstrip("/"), args.timeout)
    print("Release smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
