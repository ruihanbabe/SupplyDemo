# SupplyAgent 统一命令入口。
# 这里是命令的唯一实现来源；Markdown 只说明入口边界与预期结果，不复制实现。
# 约定：目标要么真的做事，要么明确报告「尚未具备」，不得静默成功。

PYTHON      ?= /usr/local/opt/python@3.11/bin/python3.11
VENV        := .venv
VPY         := $(VENV)/bin/python
VPIP        := $(VENV)/bin/pip
COMPOSE     := docker compose
SRC         := src

.DEFAULT_GOAL := help

.PHONY: eval-check eval-run eval-integration
eval-check: ## F38 E0/E1：报告门禁、案例与人工评分绑定（离线）
	@$(VENV)/bin/ruff check src/supplyagent/service/evaluation/*.py tests/test_product_api_evaluation*.py
	@$(VPY) -m pytest -q tests/test_product_api_evaluation.py tests/test_product_api_evaluation_result.py

.PHONY: eval-score
eval-score: ## F38 E1：评分私有快照；需SNAPSHOT/CASE/OUTPUT，可选REVIEW，未人工评审不算通过
	@PYTHONPATH=src $(VPY) -m supplyagent.service.evaluation.results --snapshot "$(SNAPSHOT)" --case "$(CASE)" --output "$(OUTPUT)" $(if $(REVIEW),--review "$(REVIEW)")

eval-run: ## F38 E0：固定离线契约案例，报告在.runtime/eval，不代表语义评分
	@PYTHONPATH=src $(VPY) -m supplyagent.service.evaluation.runner --output .runtime/eval/offline.json

eval-integration: ## F38 E0：固定克隆库/Pi业务案例，无真实Provider调用
	@PYTHONPATH=src $(VPY) -m supplyagent.service.evaluation.runner --mode integration --output .runtime/eval/integration.json

.PHONY: f33-check f33-integration-test
f33-check: ## F33 只读SSE权限/续接、BFF边界、前端lint与类型
	@$(VENV)/bin/ruff check src/supplyagent/service/api/app.py tests/test_product_api_intelligence_stream.py
	@$(VPY) -m pytest -q tests/test_product_api_intelligence_stream.py tests/test_product_api_trace.py
	@npm --prefix frontend run check:boundary
	@npm --prefix frontend run lint
	@frontend/node_modules/.bin/tsc --noEmit --project frontend/tsconfig.json

f33-integration-test: ## F33 真实克隆库的私有SSE/续接/计量，固定双协议模型报文
	@$(VPY) -m pytest -q -m integration tests/test_product_api_responses_integration.py

.PHONY: recovery-check recovery-integration-test
recovery-check: ## R1 永久/临时失败、checkpoint门禁与绑定能力（离线）
	@$(VENV)/bin/ruff check src/supplyagent/service/runtime/recovery.py src/supplyagent/service/runtime/runtime.py src/supplyagent/service/gateway/gateway.py src/supplyagent/sdk/engine/harness.py src/supplyagent/sdk/engine/tool_bridge.py src/supplyagent/service/runtime/worker_main.py tests/test_product_api_recovery.py
	@$(VPY) -m pytest -q tests/test_product_api_recovery.py tests/test_product_api_runtime.py

recovery-integration-test: ## R1 仅克隆库追加0042，验证Worker及双协议Gateway
	@$(VPY) -m pytest -q -m integration tests/test_product_api_recovery_integration.py tests/test_product_api_runtime_integration.py tests/test_product_api_responses_integration.py

.PHONY: f41-check f41-integration-test
f41-check: ## F41 输入/门禁与现有Worker离线回归
	@$(VENV)/bin/ruff check src/supplyagent/service/actions/models.py src/supplyagent/service/api/app.py src/supplyagent/service/runtime/runtime.py tests/test_product_api_f41_integration.py
	@$(VPY) -m pytest -q tests/test_product_api_runtime.py tests/test_product_api.py

f41-integration-test: ## F41 仅克隆应用0023/0043并验证人工审批与SQL执行
	@$(VPY) -m pytest -q -m integration tests/test_product_api_f41_integration.py

.PHONY: f37-setup
f37-setup: ## F37 可选固定加密依赖；不创建密钥/迁移/连接渠道
	@$(VPIP) install --only-binary=:all: -r requirements.channels.lock.txt

.PHONY: f37-check f37-integration-test
f37-check: ## F37 加密/独立持久Adapter与现有API边界（离线）
	@$(VENV)/bin/ruff check src/supplyagent/service/actions/channels.py src/supplyagent/service/actions/models.py src/supplyagent/service/api/app.py src/supplyagent/service/runtime/runtime.py tests/test_product_api_channels*.py
	@$(VPY) -m pytest -q tests/test_product_api_channels.py

f37-integration-test: ## F37 克隆SQL/独立加密SQLite，审批门禁/未知对账/签名回执
	@$(VPY) -m pytest -q --tb=short -m integration tests/test_product_api_channels_integration.py
.PHONY: help setup status check compile lint test \
        services-up services-status services-smoke services-down freeze migrate test-integration \
        web-install web-check api-check api-integration-test api-live-auth-test api-run tenant-migrate tenant-test supply-migrate supply-test supply-api-check supply-api-integration-test trace-migrate trace-test trace-integration-test f42-check f42-integration-test crm-migrate crm-test gateway-migrate gateway-test gateway-check gateway-integration-test gateway-run runtime-migrate runtime-test runtime-check runtime-integration-test runtime-worker f36-check f36-migrate f36-integration-test knowledge-migrate knowledge-test

help: ## 列出所有可用目标
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------- 环境 ----------

setup: ## 建虚拟环境、装依赖、在缺少 .env 时复制模板
	@test -d $(VENV) || { echo ">> 创建虚拟环境 ($(PYTHON))"; $(PYTHON) -m venv $(VENV); }
	@$(VPY) -m pip install --quiet --upgrade pip setuptools wheel
	@if [ -f requirements.lock.txt ]; then \
	  echo ">> 按锁文件安装"; $(VPIP) install --quiet -r requirements.lock.txt; \
	elif [ -f requirements.txt ]; then \
	  echo ">> 按 requirements.txt 安装"; $(VPIP) install --quiet -r requirements.txt; \
	else \
	  echo ">> 无依赖文件，跳过安装（见 requirement.txt 台账）"; \
	fi
	@test -f .env || { cp .env.example .env; echo ">> 已由模板生成 .env，请填入真实值"; }
	@echo ">> setup 完成"

status: ## 检查解释器、虚拟环境与项目入口（不碰服务）
	@echo "解释器      : $(PYTHON) -> $$($(PYTHON) --version 2>&1)"
	@test -d $(VENV) && echo "虚拟环境    : $(VENV) -> $$($(VPY) --version 2>&1)" || echo "虚拟环境    : 未创建（先跑 make setup）"
	@test -f .env && echo ".env        : 存在" || echo ".env        : 缺失（先跑 make setup）"
	@echo "源码目录    : $$(find $(SRC) -name '*.py' 2>/dev/null | wc -l | tr -d ' ') 个 .py 文件"

freeze: ## 把当前虚拟环境固化为锁文件
	@$(VPIP) freeze > requirements.lock.txt && echo ">> 已写入 requirements.lock.txt"

# ---------- 离线与本地服务验证 ----------

compile: ## ① 静态契约：语法与类型编译检查
	@n=$$(find $(SRC) -name '*.py' 2>/dev/null | wc -l | tr -d ' '); \
	if [ "$$n" = "0" ]; then echo ">> 尚无 Python 源码，compile 跳过（不算通过）"; exit 0; fi; \
	$(VPY) -m compileall -q $(SRC) alembic tests && echo ">> compile 通过"

lint: ## ① 静态契约：ruff 检查
	@if [ ! -x $(VENV)/bin/ruff ]; then echo ">> ruff 未装配，lint 跳过（须在 evidence.static 注明）"; exit 0; fi; \
	n=$$(find $(SRC) -name '*.py' 2>/dev/null | wc -l | tr -d ' '); \
	if [ "$$n" = "0" ]; then echo ">> 尚无 Python 源码，lint 跳过"; exit 0; fi; \
	$(VENV)/bin/ruff check $(SRC) alembic tests

test: ## ② 离线测试：pytest，固定数据与 mock，不连服务也不访问供应商
	@if [ ! -d tests ]; then echo ">> 尚无 tests/ 目录，test 跳过（不算通过）"; exit 0; fi; \
	$(VPY) -m pytest -q -m "not integration and not supabase_live and not model_live" tests

check: compile lint test ## ①② 两层的离线门禁（不证明真实服务与业务正确性）
	@echo ">> check 完成：仅覆盖离线语法/lint/测试"

test-integration: ## ③ 集成故障注入：连本地真实 PostgreSQL / Redis，不调用外部服务
	@if [ ! -d tests ]; then echo ">> 尚无 tests/ 目录，test-integration 跳过（不算通过）"; exit 0; fi; \
	$(COMPOSE) ps --status running --quiet postgres >/dev/null 2>&1 || { echo ">> 服务未启动，先跑 make services-up"; exit 1; }; \
	$(VPY) -m pytest -q -m "integration and not supabase_live and not model_live" tests

migrate: ## 仅维护遗留 Alembic 历史；当前产品 schema 使用各 Feature 的 *-migrate 目标
	@test -d alembic || { echo ">> 遗留 Alembic 历史不存在，无法维护"; exit 1; }
	@$(VENV)/bin/alembic upgrade head

# ---------- 本地服务：集成验证前置 ----------

services-up: ## 起 PostgreSQL + Redis（compose.yaml 为拓扑权威）
	@$(COMPOSE) up -d

services-status: ## 查看服务状态与健康检查
	@$(COMPOSE) ps

services-smoke: ## 连真实 Redis / PostgreSQL 做连通性冒烟
	@$(COMPOSE) exec -T postgres pg_isready -U $${SUPPLYAGENT_PG_USER:-postgres} && \
	 $(COMPOSE) exec -T redis redis-cli ping && \
	 echo ">> services smoke 通过（仅连通性，不证明 schema 或业务正确性）"

services-down: ## 停服务（保留数据卷）
	@$(COMPOSE) down

# ---------- F31 新产品：Next.js + Supabase 兼容 PostgreSQL ----------

web-install: ## 安装 frontend/ 的锁定 Node 依赖
	@npm --prefix frontend ci

web-check: ## 校验 F31 前端边界、lint 与 production build
	@npm --prefix frontend run check

api-check: ## 校验 F31 Python API 的语法、lint 与离线 HTTP 契约
	@$(VPY) -m compileall -q src/product_api src/supplyagent tests/test_product_api.py
	@$(VENV)/bin/ruff check src/product_api src/supplyagent tests/test_product_api.py
	@$(VPY) -m pytest -q tests/test_product_api.py

api-integration-test: tenant-migrate ## F31 Python API 的本地 PostgreSQL 租户事务验收（需服务已启动）
	@$(VPY) -m pytest -q -m integration tests/test_product_api_integration.py

api-live-auth-test: tenant-migrate ## F31：显式授权后以测试用户验证真实 Supabase Auth、Python API 与本地 RLS
	@$(VPY) -m pytest -q -m supabase_live tests/test_product_api_supabase_live.py

api-run: ## 在 127.0.0.1:8000 启动 F31 Python API（读取 .env；真实 Auth 配置缺失时安全拒绝业务请求）
	@PYTHONPATH=src $(VENV)/bin/uvicorn supplyagent.service.api.app:create_default_app --factory --env-file .env --host 127.0.0.1 --port 8000

tenant-migrate: ## 应用 F31 组织/RLS SQL（需先 services-up；不触碰旧 Alembic 表）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0001_f31_tenant.sql

tenant-test: ## F31 真实 PostgreSQL RLS 验收（需先 tenant-migrate；事务结束后回滚 fixture）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/tests/f31_rls.sql

supply-migrate: ## 应用 F39 供应链事实与时间线 schema（需先 tenant-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0002_f39_supply.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0003_f39_supply_timeline.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0015_f39_supply_fact_corrections.sql

supply-test: ## F39 真实 PostgreSQL 供应链 RLS 验收（需先 supply-migrate；事务结束后回滚 fixture）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/tests/f39_supply.sql

supply-api-check: ## F39 Python 领域/API 离线契约与确定性计算验收
	@$(VPY) -m compileall -q src/product_api tests/test_product_api_supply.py
	@$(VENV)/bin/ruff check src/product_api tests/test_product_api_supply.py
	@$(VPY) -m pytest -q tests/test_product_api.py tests/test_product_api_supply.py

supply-api-integration-test: supply-migrate ## F39 Python API 本地 PostgreSQL 端到端验收（需服务已启动）
	@$(VPY) -m pytest -q -m integration tests/test_product_api_supply_integration.py

trace-migrate: ## 应用 F40 组织级 Trace schema（需先 supply-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0004_f40_trace.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0016_f40_trace_hardening.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0017_f40_artifact_read_and_hash.sql

trace-test: ## F40 真实 PostgreSQL Trace 隔离、哈希链与只追加验收（需先 trace-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/tests/f40_trace.sql

trace-integration-test: trace-migrate ## F40 真实 PostgreSQL Case 隔离与 Artifact 并发版本验收
	@$(VPY) -m pytest -q -m integration tests/test_product_api_trace_integration.py

f42-check: ## F42 Python Harness / Node JSONL 协议离线验收
	@$(VPY) -m compileall -q src/product_api tests/test_product_api_harness.py
	@$(VENV)/bin/ruff check src/supplyagent/sdk/engine/harness.py tests/test_product_api_harness.py tests/test_product_api_harness_integration.py
	@$(VPY) -m pytest -q tests/test_product_api_harness.py

f42-integration-test: trace-migrate ## F42 真实 Node sidecar + PostgreSQL Trace/RLS 验收（需服务已启动）
	@$(VPY) -m pytest -q -m integration tests/test_product_api_harness_integration.py

crm-migrate: ## 应用 F32 CRM schema（需先 trace-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0005_f32_crm.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0006_f32_crm_commands.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0007_f32_crm_interactions.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0008_f32_crm_review.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0011_f32_crm_scope.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0012_f32_crm_audit.sql

crm-test: ## F32 真实 PostgreSQL CRM RLS 与 Proposal 验收（需先 crm-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/tests/f32_crm.sql

crm-facts-integration-test: crm-migrate ## F36 P3 经 F32 读取商机事实的 PostgreSQL/RLS 验收
	@PYTHONPATH=src $(VPY) -m pytest -q -m integration tests/test_product_api_crm_integration.py

gateway-migrate: ## 应用 F34 Gateway schema（需先 trace-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0009_f34_gateway.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0018_f34_budget_reservations.sql

gateway-test: ## F34 Gateway 组织隔离验收（需先 gateway-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/tests/f34_gateway.sql

gateway-check: ## F34 HTTP 能力、Pi 接入和 Provider 适配器的离线验证（HTTP 回环需本机端口权限）
	@$(VPY) -m compileall -q src/product_api tests/test_product_api_gateway.py
	@$(VENV)/bin/ruff check src/supplyagent/service/gateway/gateway.py src/supplyagent/sdk/engine/harness.py tests/test_product_api_gateway.py tests/test_product_api_gateway_integration.py
	@$(VPY) -m pytest -q tests/test_product_api_gateway.py

gateway-integration-test: gateway-migrate ## F34 PostgreSQL/RLS 预算预留与计量验收
	@$(VPY) -m pytest -q -m integration tests/test_product_api_gateway_integration.py

gateway-run: ## 启动仅绑定本机的 F34 HTTP Gateway（需配置签名密钥与受限数据库）
	@PYTHONPATH=src $(VENV)/bin/uvicorn supplyagent.service.gateway.gateway:create_default_model_gateway_app --factory --env-file .env --host 127.0.0.1 --port 8001

.PHONY: gateway-readiness-integration-test
gateway-readiness-integration-test: ## 克隆库验证 Gateway 就绪、权限/结构拒绝与计量；不迁移业务库
	@$(VPY) -m pytest -q -m integration tests/test_product_api_gateway_integration.py

runtime-migrate: ## 应用 F35 Durable Runtime schema（需先 gateway-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0010_f35_runtime.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0019_f35_worker_lifecycle.sql

runtime-test: ## F35 SQL 队列、租约与幂等验收（需先 runtime-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/tests/f35_runtime.sql

runtime-check: ## F35 Worker 端口与失败映射离线检查
	@$(VENV)/bin/ruff check src/supplyagent/service/runtime/runtime.py src/supplyagent/service/runtime/worker_main.py tests/test_product_api_runtime.py
	@PYTHONPATH=src $(VPY) -m pytest -q tests/test_product_api_runtime.py

runtime-integration-test: ## 克隆本地测试库验证 F35 SQL/RLS/Pi/Trace 后删除隔离克隆
	@PYTHONPATH=src $(VPY) -m pytest -q -m integration tests/test_product_api_runtime_integration.py

runtime-worker: ## 启动单组织 F35 Worker；需显式配置 Worker 身份和数据库
	@PYTHONPATH=src $(VPY) -m supplyagent.service.runtime.worker_main

.PHONY: runtime-worker-once
runtime-worker-once: ## cron 可选入口：只领取一次已有任务，打印安全报告并返回退出码
	@PYTHONPATH=src $(VPY) -m supplyagent.service.runtime.worker_main --once

f36-check: ## F36 Specialist、知识、CRM 事实读取与质量门禁
	@$(VENV)/bin/ruff check src/supplyagent/service/domains/crm.py src/supplyagent/service/workflows/specialists.py src/supplyagent/service/workflows/specialist_runner.py src/supplyagent/service/workflows/mission_runtime.py src/supplyagent/service/domains/knowledge.py src/supplyagent/service/domains/knowledge_import.py src/supplyagent/service/domains/memory.py tests/test_product_api_crm.py tests/test_product_api_specialists.py tests/test_product_api_specialist_runner.py tests/test_product_api_mission_runtime.py tests/test_product_api_knowledge.py tests/test_product_api_knowledge_import.py tests/test_product_api_memory.py
	@PYTHONPATH=src $(VPY) -m pytest -q tests/test_product_api_crm.py tests/test_product_api_specialists.py tests/test_product_api_specialist_runner.py tests/test_product_api_mission_runtime.py tests/test_product_api_knowledge.py tests/test_product_api_knowledge_import.py tests/test_product_api_memory.py

f36-migrate: runtime-migrate crm-migrate knowledge-migrate ## 应用 F36 Durable Mission schema
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0021_f36_mission.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0022_f36_memory.sql

f36-integration-test: f36-migrate ## F36 多 Specialist Mission、F40 Artifact 与 RLS 验收
	@PYTHONPATH=src $(VPY) -m pytest -q -m integration tests/test_product_api_mission_integration.py

knowledge-migrate: tenant-migrate ## 应用 F36 P1 组织知识、ACL、版本与 Citation schema
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0020_f36_knowledge.sql

knowledge-test: ## F36 P1 PostgreSQL/RLS/ACL/Citation 验收（需先 knowledge-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/tests/f36_knowledge.sql

.PHONY: f45-migrate f45-integration-test

f45-migrate: ## 追加 F45 私有问答与执行快照（需先完成 F36 迁移，不应用 F41 草稿）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0024_f45_intelligence.sql

f45-integration-test: ## F45 在克隆库验证 HTTP/RLS、Pi 与执行快照；不改源库
	@$(VPY) -m pytest -q --tb=short -m integration tests/test_product_api_intelligence_integration.py

.PHONY: f46-check f46-integration-test f46-migrate

f46-check: ## F46 确定性业务节点与固定故事输入
	@$(VENV)/bin/ruff check src/supplyagent/service/domains/business_nodes.py tests/test_product_api_business_nodes*.py
	@$(VPY) -m pytest -q -m "not integration" tests/test_product_api_business_nodes.py

f46-integration-test: ## F46 克隆库：故事种子、HTTP/Worker/Pi、全零件与跟进时序
	@$(VPY) -m pytest -q --tb=short -m integration tests/test_product_api_business_nodes_integration.py

f46-migrate: ## F46 追加 0025（先确认清单并应用 0024；不自动触碰 F41）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0025_f46_business_nodes.sql

.PHONY: f47-check f47-integration-test f47-migrate

f47-check: ## F47 BOM 精确用量与报价时效离线验证
	@$(VENV)/bin/ruff check src/supplyagent/service/domains/commercial.py tests/test_product_api_commercial*.py
	@$(VPY) -m pytest -q -m "not integration" tests/test_product_api_commercial.py

f47-integration-test: ## F47 克隆库：不可变版本、报价 Specialist、改绑与证据撤回
	@$(VPY) -m pytest -q --tb=short -m integration tests/test_product_api_commercial_integration.py

f47-migrate: ## F47 追加 0026（先确认追加清单并应用 0025；不应用 F41）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0026_f47_bom_quotes.sql

.PHONY: f49-check f49-integration-test f49-migrate

f49-check: ## F49 原生 Responses、真实 Pi 往返和失败/计量验证（无外部调用）
	@$(VENV)/bin/ruff check src/supplyagent/service/gateway/gateway.py src/supplyagent/service/gateway/model_tools.py tests/test_product_api_gateway.py
	@$(VPY) -m pytest -q --tb=short -m "not integration" tests/test_product_api_gateway.py tests/test_product_api_intelligence.py

f49-integration-test: ## F49 克隆库：Responses Gateway、五角色、私有交换及 RLS
	@$(VPY) -m pytest -q --tb=short -m integration tests/test_product_api_gateway_integration.py tests/test_product_api_responses_integration.py

f49-migrate: ## F49 追加 0027（先确认清单；商机链路需先应用 0024–0026）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0027_f49_responses.sql


.PHONY: f50-check f50-integration-test f50-live-smoke f50-migrate

f50-check: ## F50 GLM 固定免费模型与原生 Pi 兼容（不联网）
	@$(VENV)/bin/ruff check src/supplyagent/service/gateway/glm.py tests/test_product_api_glm*.py
	@$(VPY) -m pytest -q --tb=short -m "not model_live" tests/test_product_api_glm.py

f50-integration-test: ## F50 克隆库：GLM 五角色和既有 Responses 回归
	@$(VPY) -m pytest -q --tb=short -m integration tests/test_product_api_responses_integration.py

f50-live-smoke: ## F50 已获授权后运行：最多 3 次官方免费 GLM 请求，仅合成数据
	@SUPPLYAGENT_GLM_LIVE=1 $(VPY) -m pytest -q -s --tb=short -m model_live tests/test_product_api_glm_live.py

f50-migrate: ## F50 追加 0028（先确认清单并应用 0027；不改业务数据）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0028_f50_glm.sql

.PHONY: cleanup-product-migrate
cleanup-product-migrate: ## 仅应用已批准的 0029 旧入口清理；先备份并验证克隆，不应用0023–0028
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0029_retire_obsolete_entrypoints.sql

.PHONY: cleanup-integration-test
cleanup-integration-test: ## 临时数据库验证空表保护、事务回滚、退役幂等与产品/硬件保留
	@$(VPY) -m pytest -q -m integration tests/test_cleanup_migrations_integration.py

.PHONY: f51-check f51-integration-test

f51-check: ## F51 P1–P4 Registry/Runtime/Scheduler/真实 Pi v1/v2（离线）
	@$(VENV)/bin/ruff check src/supplyagent/sdk/tools/registry.py src/supplyagent/service/gateway/model_tools.py src/supplyagent/sdk/engine/harness.py src/supplyagent/sdk/tools/runtime.py src/supplyagent/sdk/tools/scheduler.py src/supplyagent/sdk/engine/tool_bridge.py tests/test_product_api_tool_registry.py tests/test_product_api_tool_runtime.py tests/test_product_api_tool_scheduler.py tests/test_product_api_tool_bridge.py
	@$(VPY) -m pytest -q --tb=short tests/test_product_api_tool_registry.py tests/test_product_api_tool_runtime.py tests/test_product_api_tool_scheduler.py tests/test_product_api_tool_bridge.py tests/test_product_api_harness.py tests/test_product_api_specialist_runner.py

f51-integration-test: ## F51 P1–P4 F36/F45/双模型协议、台账/共享容量克隆验证，不迁移源库
	@$(VPY) -m pytest -q --tb=short -m integration tests/test_product_api_runtime_integration.py tests/test_product_api_business_nodes_integration.py tests/test_product_api_commercial_integration.py tests/test_product_api_mission_integration.py tests/test_product_api_intelligence_integration.py tests/test_product_api_responses_integration.py tests/test_product_api_tool_runtime_integration.py tests/test_product_api_tool_scheduler_integration.py

.PHONY: f51-migrate
f51-migrate: ## 须先确认清单：只追加0030调用台账、0031共享容量；不自动应用其他Feature
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0030_f51_tool_execution.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0031_f51_tool_resources.sql

.PHONY: f52-check f52-integration-test
f52-check: ## F52 当前单元的 Context 公共Interface与消息配对离线验证
	@node --check pi_sidecar/context_protocol.mjs
	@node --check pi_sidecar/f42_protocol_sidecar.mjs
	@$(VENV)/bin/ruff check src/product_api/context*.py tests/test_product_api_context*.py
	@$(VPY) -m pytest -q --tb=short tests/test_product_api_context_store.py tests/test_product_api_context_runtime.py tests/test_product_api_context_compaction.py

f52-integration-test: ## F52 自动删除克隆库中的会话/状态与当前Context协议验证，不迁移源库
	@$(VPY) -m pytest -q --tb=short -m integration tests/test_product_api_context_store_integration.py tests/test_product_api_context_session_integration.py tests/test_product_api_context_compaction_integration.py

.PHONY: f48-setup f48-check f48-backend-test
f48-setup: ## F48 可选依赖：只安装固定Mem0及锁定后端，不调用模型或迁移
	@$(VPIP) install -r requirements.memory.lock.txt

f48-check: ## F48 当前单元离线准入验证；不代表后端/长期记忆闭环完成
	@$(VENV)/bin/ruff check src/supplyagent/service/memory/mem0_adapter.py src/product_api/memory_*.py tests/test_product_api_memory*.py tests/test_product_api_mem0_backend_integration.py
	@$(VPY) -m pytest -q -m "not integration" tests/test_product_api_mem0_backend_integration.py tests/test_product_api_memory_runtime.py

f48-backend-test: ## F48 实际锁定Mem0/Qdrant/SQLite与本地embedding的隔离/擦除探针
	@$(VPY) -m pytest -q --tb=short -m integration tests/test_product_api_mem0_backend_integration.py

.PHONY: f48-integration-test
f48-integration-test: ## F48 当前单元：克隆SQL + 实际Mem0与本地报文，不迁移业务库
	@$(VPY) -m pytest -q --tb=short -m integration tests/test_product_api_memory*_integration.py tests/test_product_api_mem0_backend_integration.py

.PHONY: memory-worker
memory-worker: ## F48 运行一个后台步骤；默认disabled，显式合成配置后才接本机embedding
	@$(VPY) -m supplyagent.service.memory.worker_main --once

.PHONY: architecture-check
architecture-check: ## F53 A：模块归属、旧入口同一性及过渡依赖不扩张
	@$(VENV)/bin/ruff check src/supplyagent src/product_api tests/test_architecture.py
	@$(VPY) -m pytest -q tests/test_architecture.py
