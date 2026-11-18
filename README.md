# 多 Agent 长任务原型

把订单 CSV 导出改为异步，涉及后台写入、状态查询和客户端轮询。这个原型演示 Leader 如何拆分改造，Worker 如何分别交付代码，以及 Verifier 怎样用一次真实的 HTTP 验收发现问题并触发局部返修。

**Python 3.9+，只用标准库。** Agent 角色由可重复执行的脚本模拟。原型跑真实的导出服务、后台任务、CLI 客户端和端到端检查，服务只监听本机。

## 跑一遍完整流程

```bash
python3 -m agent_team run
```

1. Leader 定义接口和两条验收路径。前端和后端 Worker 并行构建第一版候选代码。
2. Verifier 先提交正常导出并校验 CSV，再注入一次文件写入错误。第一版的正常下载通过，异常分支一直返回 `processing`，轮询在测试截止时间超时。
3. Team Engine 重新调度后端 Worker。后端把写入异常变为 `failed`，客户端使用同一份代码，在收到失败终态后退出轮询。
4. Verifier 在第二版重跑两条路径。两条都通过，流程才进入 `done`。

终端打印运行目录和事件。每个候选版本保存在 `.agent-runs/run-…/artifacts/v1` 或 `v2`。同目录的 `verification-v*.json` 记录检查结果和候选文件的 SHA-256，可以用下面的命令查看实际返修差异。

```bash
RUN=.agent-runs/run-替换为上一步打印的目录名
diff -u "$RUN/artifacts/v1/export_app.py" "$RUN/artifacts/v2/export_app.py"
python3 -m agent_team status "$RUN"
```

## 演示中断恢复和停止条件

```bash
python3 -m agent_team run --pause-on-rejection
# 把 <运行目录> 替换为上面打印的路径
python3 -m agent_team status "<运行目录>"
python3 -m agent_team resume "<运行目录>"
```

第一次验收失败后，状态和事件已保存到该次运行目录中的 SQLite。恢复时从已排队的后端返修开始，复用第一版的前端文件。也可以运行 `python3 -m agent_team run --max-repairs 0`，观察任务怎样停在 `blocked` 并留下诊断信息，进程退出码为 1。

## 手动体验通过验收的服务

先按第一节生成一个 `done` 的运行，再开两个终端。以下假设 `RUN` 已指向该运行目录，每个终端需要单独设置。

```bash
# 终端 1
python3 -m agent_team serve "$RUN" --port 8000
```

```bash
# 终端 2
python3 "$RUN/artifacts/v2/client.py" http://127.0.0.1:8000 --output orders.csv
```

| 请求 | 成功响应 |
| --- | --- |
| `POST /exports` | HTTP 202，返回 `job_id` 和初始状态 |
| `GET /exports/{job_id}` | HTTP 200，状态为 `processing`、`succeeded` 或 `failed` |
| `GET /exports/{job_id}/download` | 成功的任务返回 CSV，尚未成功的任务返回 HTTP 409 |

业务订单是测试数据，后台导出任务只保存在服务进程里。可跨进程恢复的是 Agent 编排状态。

## 代码入口

- `agent_team/workflow.py` 保存任务状态、构建版本并决定验收失败后的流向。
- `agent_team/verifier.py` 启动候选版本，通过真实 HTTP 检查成功与失败用例。
- `agent_team/fixtures/` 提供后端初版和轮询客户端，由 Worker 复制到各次运行的产物目录。

设计参考 [MiniMax Agent Team 官方文章](https://www.minimax.cn/blog/minimax-agent-team-long-running-1779893521)。
