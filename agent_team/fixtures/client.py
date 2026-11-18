"""Submit an order export, poll its status, and download the finished CSV."""

import argparse
import json
import math
from pathlib import Path
import sys
import time
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


TERMINAL_STATUSES = {"succeeded", "failed"}


def _exports_url(base_url):
    return base_url.rstrip("/") + "/exports"


def _job_url(base_url, job_id):
    return _exports_url(base_url) + "/" + quote(job_id, safe="")


def _read_response(url, *, method="GET", expected_status=200, timeout):
    with urlopen(Request(url, method=method), timeout=timeout) as response:
        if response.status != expected_status:
            raise ValueError(f"Expected HTTP {expected_status}, got {response.status}")
        return response.read()


def _decode_job(payload):
    job = json.loads(payload)
    if not isinstance(job, dict) or not isinstance(job.get("job_id"), str) or not job["job_id"]:
        raise ValueError("Export response is missing a valid job ID")
    status = job.get("status")
    if not isinstance(status, str) or status not in TERMINAL_STATUSES | {"processing"}:
        raise ValueError(f"Unexpected export status: {status!r}")
    return job


def submit_export(base_url: str, *, timeout: float = 2.0) -> dict:
    payload = _read_response(
        _exports_url(base_url), method="POST", expected_status=202, timeout=timeout
    )
    return _decode_job(payload)


def wait_for_export(
    base_url: str, job_id: str, *, timeout: float = 1.0, poll_interval: float = 0.02
) -> dict:
    """Return the terminal status document, or raise when polling reaches its deadline."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("poll_interval must be a positive finite number")

    deadline = time.monotonic() + timeout
    status_url = _job_url(base_url, job_id)
    last_status = "unknown"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            job = _decode_job(_read_response(status_url, timeout=remaining))
        except URLError as error:
            if not isinstance(error.reason, TimeoutError):
                raise
            break
        except TimeoutError:
            break
        if job["job_id"] != job_id:
            raise ValueError("Status response refers to a different export job")
        last_status = job["status"]
        if last_status in TERMINAL_STATUSES:
            return job

        wait = min(poll_interval, deadline - time.monotonic())
        if wait > 0:
            time.sleep(wait)

    raise TimeoutError(f"Export {job_id} timed out (last status: {last_status})")


def download_export(base_url: str, job_id: str, *, timeout: float = 2.0) -> bytes:
    return _read_response(_job_url(base_url, job_id) + "/download", timeout=timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="Order export server URL")
    parser.add_argument("--output", type=Path, default=Path("orders.csv"), help="CSV destination")
    parser.add_argument("--timeout", type=float, default=5.0, help="Polling deadline in seconds")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")

    try:
        job_id = submit_export(args.base_url)["job_id"]
        print(f"Submitted export {job_id}", flush=True)
        job = wait_for_export(args.base_url, job_id, timeout=args.timeout)
        if job["status"] == "failed":
            print(f"Export {job_id} failed: {job.get('error') or 'CSV generation failed'}", file=sys.stderr)
            return 1

        args.output.write_bytes(download_export(args.base_url, job_id))
    except (OSError, ValueError) as error:
        print(f"Could not export orders: {error}", file=sys.stderr)
        return 1

    print(f"Downloaded export {job_id} to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
