"""Exercise a candidate export service and polling client over HTTP."""

import csv
import importlib.util
import io
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    details: str
    code: str = ""


EXPECTED_ROWS = [
    ["order_id", "customer", "total"],
    ["ORD-1001", "林晓", "129.00"],
    ["ORD-1002", "周宁", "88.50"],
    ["ORD-1003", "陈晨", "240.00"],
]


@contextmanager
def _candidate_module(path):
    name = f"_export_candidate_{path.stem}_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None:
        raise ImportError(f"无法加载候选文件 {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        # Read the exact source being verified without creating a bytecode cache.
        exec(compile(path.read_bytes(), str(path), "exec"), vars(module))
        yield module
    finally:
        del sys.modules[name]


@contextmanager
def _running_server(app, *, fail_writes):
    with app.make_server(fail_writes=fail_writes) as server:
        host, port = server.server_address
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            yield f"http://{host}:{port}"
        finally:
            server.shutdown()
            thread.join()


def _require(condition, details):
    if not condition:
        raise AssertionError(details)


def _submit(base_url):
    request = Request(f"{base_url}/exports", method="POST")
    with urlopen(request, timeout=1.0) as response:
        _require(response.status == 202, f"提交应返回 HTTP 202，实际为 {response.status}")
        job = json.load(response)
    job_id = job.get("job_id")
    _require(isinstance(job_id, str) and job_id, "提交响应缺少任务 ID")
    _require(job.get("status") == "processing", f"提交后应进入 processing，实际为 {job.get('status')!r}")
    return job_id


def _download_check(app, client):
    with _running_server(app, fail_writes=False) as base_url:
        job_id = _submit(base_url)
        result = client.wait_for_export(base_url, job_id)
        _require(result.get("status") == "succeeded", f"导出应成功，实际为 {result.get('status')!r}")
        with urlopen(f"{base_url}/exports/{job_id}/download", timeout=1.0) as response:
            _require(response.status == 200, f"下载应返回 HTTP 200，实际为 {response.status}")
            _require(response.headers.get_content_type() == "text/csv", "下载响应类型应为 text/csv")
            rows = list(csv.reader(io.StringIO(response.read().decode("utf-8"), newline="")))
        _require(rows == EXPECTED_ROWS, f"订单 CSV 内容不符，实际为 {rows!r}")
    return Check("download", True, "正常订单导出进入 succeeded，并下载了包含 3 条订单的 CSV。")


def _write_failure_check(app, client):
    with _running_server(app, fail_writes=True) as base_url:
        job_id = _submit(base_url)
        try:
            result = client.wait_for_export(base_url, job_id)
        except TimeoutError:
            with urlopen(f"{base_url}/exports/{job_id}", timeout=1.0) as response:
                status = json.load(response).get("status")
            if status == "processing":
                return Check(
                    "write_failure", False,
                    "模拟 CSV 写入失败后，状态仍为 processing，客户端轮询超时。",
                    "poll_timeout",
                )
            raise
        _require(result.get("status") == "failed", f"写入失败应进入 failed，实际为 {result.get('status')!r}")
        _require(result.get("error") == "export_write_failed", "失败响应应携带稳定的错误码 export_write_failed")
        try:
            with urlopen(f"{base_url}/exports/{job_id}/download", timeout=1.0) as response:
                download_status = response.status
        except HTTPError as error:
            with error:
                download_status = error.code
        _require(download_status == 409, f"失败任务的下载应返回 HTTP 409，实际为 {download_status}")
    return Check("write_failure", True, "模拟 CSV 写入失败后进入 failed，客户端结束轮询，下载返回 HTTP 409。")


def _run_check(name, check, app, client):
    try:
        return check(app, client)
    except AssertionError as error:
        return Check(name, False, str(error), "assertion_failed")
    except Exception as error:
        return Check(name, False, f"{type(error).__name__}: {error}", "execution_error")


def verify(snapshot: Path) -> list[Check]:
    """Return the normal download and injected write-failure checks."""
    checks = [("download", _download_check), ("write_failure", _write_failure_check)]
    try:
        with _candidate_module(snapshot / "export_app.py") as app, _candidate_module(snapshot / "client.py") as client:
            return [_run_check(name, check, app, client) for name, check in checks]
    except Exception as error:
        details = f"候选版本加载失败，{type(error).__name__}: {error}"
        return [Check(name, False, details, "load_error") for name, _ in checks]
