"""Small async CSV service with one intentional defect for the demo to repair."""

import csv
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4


ORDER_ROWS = (
    ("order_id", "customer", "total"),
    ("ORD-1001", "林晓", "129.00"),
    ("ORD-1002", "周宁", "88.50"),
    ("ORD-1003", "陈晨", "240.00"),
)


class ExportServer(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, address, fail_writes):
        self._fail_writes = fail_writes
        self._statuses = {}
        self._lock = Lock()
        self._resources = ExitStack()
        with self._resources as resources:
            self._directory = Path(resources.enter_context(TemporaryDirectory(prefix="order-exports-")))
            self._executor = resources.enter_context(ThreadPoolExecutor(max_workers=2))
            super().__init__(address, ExportHandler)
            self._resources = resources.pop_all()

    def submit_export(self):
        job_id = uuid4().hex
        self._set_status(job_id, "processing")
        self._executor.submit(self._export, job_id)
        return job_id

    def get_status(self, job_id):
        with self._lock:
            return self._statuses.get(job_id)

    def download(self, job_id):
        return self._artifact(job_id).read_bytes()

    def _artifact(self, job_id):
        return self._directory / f"{job_id}.csv"

    def _set_status(self, job_id, status):
        with self._lock:
            self._statuses[job_id] = status

    def _export(self, job_id):
        try:
            self._write_csv(self._artifact(job_id))
        except OSError:
            return  # Demo defect: failed writes leave the job processing.
        self._set_status(job_id, "succeeded")

    def _write_csv(self, path):
        if self._fail_writes:
            raise OSError("simulated CSV write failure")
        with path.open("w", encoding="utf-8", newline="") as output:
            csv.writer(output).writerows(ORDER_ROWS)

    def server_close(self):
        try:
            super().server_close()
        finally:
            self._resources.close()


class ExportHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if urlsplit(self.path).path != "/exports":
            self._json(404, {"error": "not_found"})
            return

        job_id = self.server.submit_export()
        self._json(202, {"job_id": job_id, "status": "processing"})

    def do_GET(self):
        parts = urlsplit(self.path).path.split("/")
        status_request = len(parts) == 3
        download_request = len(parts) == 4 and parts[-1] == "download"
        if parts[1:2] != ["exports"] or not (status_request or download_request):
            self._json(404, {"error": "not_found"})
            return

        job_id = parts[2]
        status = self.server.get_status(job_id)
        if status is None:
            self._json(404, {"error": "export_not_found"})
        elif status_request:
            job = {"job_id": job_id, "status": status}
            if status == "failed":
                job["error"] = "export_write_failed"
            self._json(200, job)
        elif status == "succeeded":
            self._download(job_id)
        else:
            self._json(409, {"error": "export_not_ready", "status": status})

    def _download(self, job_id):
        try:
            content = self.server.download(job_id)
        except OSError:
            self._json(500, {"error": "export_unavailable"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="orders.csv"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status, payload):
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        pass


def make_server(host="127.0.0.1", port=0, *, fail_writes=False):
    """Create a local server. Call serve_forever and shut it down when finished."""
    return ExportServer((host, port), fail_writes)
