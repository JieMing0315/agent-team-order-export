from __future__ import annotations

import argparse
import json
import runpy
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from .workflow import create_run, events_after, execute, hashes, open_run, snapshot, state


def print_event(event: sqlite3.Row) -> None:
    print(f"{event['seq']:02}  {event['role']:16}  {event['phase']:9}  v{event['revision']}  {event['message']}", flush=True)


def repair_budget(value: str) -> int:
    if value not in ("0", "1"):
        raise argparse.ArgumentTypeError("此演示的预设修复预算可设为 0 或 1")
    return int(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="订单 CSV 异步导出 · 多 Agent 协作演示")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="创建运行并执行角色分工")
    run.add_argument("--runs-dir", type=Path, default=Path(".agent-runs"))
    run.add_argument("--max-repairs", type=repair_budget, default=1)
    run.add_argument("--pause-on-rejection", action="store_true", help="记录首次退回后退出，便于演示恢复")

    commands.add_parser("resume", help="从持久化状态继续").add_argument("run_dir", type=Path)
    commands.add_parser("status", help="查看当前状态和完整事件").add_argument("run_dir", type=Path)
    serve = commands.add_parser("serve", help="启动已经验收通过的导出服务")
    serve.add_argument("run_dir", type=Path)
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    try:
        if args.command == "run":
            path = create_run(args.runs_dir, args.max_repairs)
            print(f"运行目录 {path}", flush=True)
        else:
            path = args.run_dir.resolve()

        with closing(open_run(path)) as connection:
            if args.command in ("run", "status"):
                for event in events_after(connection, 0):
                    print_event(event)
            if args.command == "status":
                current = state(connection)
                print(f"当前状态 {current['phase']} · v{current['revision']}")
                return 0
            if args.command == "serve":
                return serve_snapshot(connection, path, args.port)

            outcome = execute(connection, path, print_event, pause_on_rejection=getattr(args, "pause_on_rejection", False))
            if outcome == "paused":
                print(f"已保存断点。恢复命令 python3 -m agent_team resume \"{path}\"")
            return 1 if outcome == "blocked" else 0
    except KeyboardInterrupt:
        print("\n运行已中断。可以使用 resume 从上一次提交的状态继续。", file=sys.stderr)
        return 130
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"执行失败 · {error}", file=sys.stderr)
        return 1


def serve_snapshot(connection: sqlite3.Connection, path: Path, port: int) -> int:
    current = state(connection)
    if current["phase"] != "done":
        raise ValueError("只能启动验收通过的版本")
    revision = current["revision"]
    candidate = snapshot(path, revision)
    report = json.loads((path / f"verification-v{revision}.json").read_text(encoding="utf-8"))
    if hashes(candidate) != report["source_sha256"]:
        raise ValueError("候选文件与已验收版本不一致")
    make_server = runpy.run_path(str(candidate / "export_app.py"))["make_server"]
    with make_server(port=port) as server:
        host, bound_port = server.server_address
        print(f"导出服务已启动 http://{host}:{bound_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n导出服务已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
