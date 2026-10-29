# Model Gateway 与 Worker 运行说明

更新：2026-10-23。归属 F34/F35；功能范围与证据仍见 Contract。业务职责见 [业务逻辑 BL01/BL11](../../business-logic.md)，能力/计量与报告格式见 [数据契约 DC07/DC13](../../data-contract.md)。

## 启动与依赖

```text
Web → Next BFF → Python API → SQL Job → Worker/Harness → Pi
                                             ↑          ↓
                                          SQL/Trace   私有 Gateway → GLM
```

- 数据库与已确认迁移就绪后启动 `make gateway-run`，其监听 `127.0.0.1:8001`；Worker 同机通过私有 HTTP 连接。没有浏览器直连 Gateway 或多端桥接。
- Gateway 从 `SUPPLYAGENT_PG_*` 连接；Worker/API 使用 `SUPPLYAGENT_PRODUCT_DATABASE_URL`。部署必须指向同一目标库/迁移集，Worker 与 Gateway 共享现有签名密钥。进程不自动启动依赖或执行迁移。
- API/Web 可按自身依赖启动并显示历史记录。API 能入队不等于 Worker/Gateway/Provider 均已可执行；具体组织策略/凭据在受授权请求中校验。
- Gateway `/health` 为存活检查；`/ready` 为受限只读数据库/列/函数及交换写权限检查。ready 最多等待 5 秒，SQL 单语句 3 秒；不虚构 actor/组织，不调用模型、不写账本、不泄漏内部异常。

## Worker 与可选 cron

- 默认 `make runtime-worker` 常驻；可选 `make runtime-worker-once` 只领取一次已有任务，不创建巡检 Mission。默认不同时安装两种运行模式。
- `SUPPLYAGENT_WORKER_OWNER` 为 1–87 字符的字母/数字/点/下划线/连字符标签；自动追加 UUID，实例不复用 owner。
- 当前默认 lease 30 秒、heartbeat 10 秒、max attempts 3、retry 30 秒、空闲轮询 2 秒；现有 WORKER_* 配置可覆盖，不在此另设策略。
- CLI 输出 DC13 的一行 JSON 报告。Make 对失败 recipe 通常自身返回 2，调度需读 outcome，不能据此断言一定是配置错误；cron 不追加业务重试。
- 部署者固定仓库工作目录、解释器、配置来源与日志位置；仓库命令不修改 crontab/创建系统服务。机器停止/休眠不保证调度。

## 停止、恢复与限制

SIGTERM/SIGINT 停止领取新任务，通过现有 Adapter 取消 turn 并清理子进程；未完成不写 completed，正常停止取消沿既有 SQL cancelled 语义，不承诺自动续跑。

硬退出后由 SQL lease/reap 与新 generation 接手，不恢复 Pi 内存状态。测试强杀独立进程组，不证明部署平台能清理任意孤儿进程。2026-10-28 R1的新Worker模型能力绑定Job/owner/generation，Gateway在新请求准备/预算预留/交换接受时重验；已发出流不承诺撤回，旧/独立无Job能力仍保留原短期边界。0042须先在目标库获准应用，未就绪时新绑定请求安全拒绝。

## 验证入口

| 命令 | 作用 |
|---|---|
| `make gateway-check` | 私有模型协议、能力、健康/就绪脱敏 |
| `make gateway-readiness-integration-test` | 克隆库验证就绪、权限/结构缺失与计量，不迁移业务库 |
| `make runtime-check` | 报告/退出码、实例与停止单元测试 |
| `make runtime-integration-test` | 克隆库真实 Pi Replay、并发领取、旧代次拒绝、进程停止/强杀回收 |
| `make f50-check` / `make f50-integration-test` | 固定 GLM 兼容报文及 Responses 回归，不调用真实模型 |

历史 gateway-integration-test 等目标会先迁移源库，未批准迁移时不要替代上述克隆入口。最新执行结果只在 HANDOFF/Contract 维护，不在此重复一套测试数量。
