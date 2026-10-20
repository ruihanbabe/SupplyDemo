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

# ---------- 验证：对应 DECISIONS.md D14 的 ①② 层 ----------

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
	$(VPY) -m pytest -q -m "not integration and not supabase_live" tests

check: compile lint test ## ①② 两层的离线门禁（不证明真实服务与业务正确性）
	@echo ">> check 完成：仅覆盖离线语法/lint/测试"

test-integration: ## ③ 集成故障注入：连本地真实 PostgreSQL / Redis，不调用外部服务
	@if [ ! -d tests ]; then echo ">> 尚无 tests/ 目录，test-integration 跳过（不算通过）"; exit 0; fi; \
	$(COMPOSE) ps --status running --quiet postgres >/dev/null 2>&1 || { echo ">> 服务未启动，先跑 make services-up"; exit 1; }; \
	$(VPY) -m pytest -q -m "integration and not supabase_live" tests

migrate: ## 把 alembic 迁移（基线只有 hw_part）升级到本地数据库
	@test -d alembic || { echo ">> alembic/ 未初始化，随 F01 一并建立"; exit 1; }
	@$(VENV)/bin/alembic upgrade head

# ---------- 服务：对应 D14 的 ③ 层前置 ----------

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

# ---------- 硬件数据 ----------

.PHONY: fetch-hardware
fetch-hardware: ## 按 config/knowledge/sources.yaml 钉死的提交拉取 KiCad 原理图到只读原始层，并裁到只剩电子元件（公开 GitHub，无费用）
	@PYTHONPATH=src $(VPY) -m knowledge.fetch

.PHONY: fetch-docs
fetch-docs: ## 按 sources.yaml 的 docs 列表拉取板级文档原文，只读存档并追加进 MANIFEST；不动原理图（公开 GitHub，无费用）
	@PYTHONPATH=src $(VPY) -m knowledge.fetch --docs

.PHONY: prune-hardware
prune-hardware: ## 从本地原始层原理图删除非电子元件（连接器、开关、电池、机械件），重算 MANIFEST 哈希；可重跑
	@PYTHONPATH=src $(VPY) -m knowledge.prune

.PHONY: build-parts
build-parts: ## 从原始层 KiCad 文件生成料号表 hw_part（先做 MANIFEST 校验）：每项目每料号每厂商一行（纯确定性，零 token）
	@PYTHONPATH=src $(VPY) -m persistence.build_hardware_parts

# ---------- 模拟环境（F03） ----------

.PHONY: sim-run
sim-run: ## 不接 Agent 按种子跑满模拟环境，打印逐日事件与结果指标（纯内存，零 token）；用法 make sim-run SEED=42
	@test -n "$(SEED)" || { echo ">> 需要种子：make sim-run SEED=42"; exit 1; }
	@PYTHONPATH=src $(VPY) -m simenv.cli --seed $(SEED)

# ---------- F31 新产品：Next.js + Supabase 兼容 PostgreSQL ----------

web-install: ## 安装 frontend/ 的锁定 Node 依赖
	@npm --prefix frontend ci

web-check: ## 校验 F31 前端边界、lint 与 production build
	@npm --prefix frontend run check

api-check: ## 校验 F31 Python API 的语法、lint 与离线 HTTP 契约
	@$(VPY) -m compileall -q src/product_api tests/test_product_api.py
	@$(VENV)/bin/ruff check src/product_api tests/test_product_api.py
	@$(VPY) -m pytest -q tests/test_product_api.py

api-integration-test: tenant-migrate ## F31 Python API 的本地 PostgreSQL 租户事务验收（需服务已启动）
	@$(VPY) -m pytest -q -m integration tests/test_product_api_integration.py

api-live-auth-test: tenant-migrate ## F31：显式授权后以测试用户验证真实 Supabase Auth、Python API 与本地 RLS
	@$(VPY) -m pytest -q -m supabase_live tests/test_product_api_supabase_live.py

api-run: ## 在 127.0.0.1:8000 启动 F31 Python API（读取 .env；真实 Auth 配置缺失时安全拒绝业务请求）
	@PYTHONPATH=src $(VENV)/bin/uvicorn product_api.app:create_default_app --factory --env-file .env --host 127.0.0.1 --port 8000

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
	@$(VENV)/bin/ruff check src/product_api/harness.py tests/test_product_api_harness.py tests/test_product_api_harness_integration.py
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
	@$(VENV)/bin/ruff check src/product_api/gateway.py src/product_api/harness.py tests/test_product_api_gateway.py tests/test_product_api_gateway_integration.py
	@$(VPY) -m pytest -q tests/test_product_api_gateway.py

gateway-integration-test: gateway-migrate ## F34 PostgreSQL/RLS 预算预留与计量验收
	@$(VPY) -m pytest -q -m integration tests/test_product_api_gateway_integration.py

gateway-run: ## 启动仅绑定本机的 F34 HTTP Gateway（需配置签名密钥与受限数据库）
	@PYTHONPATH=src $(VENV)/bin/uvicorn product_api.gateway:create_default_model_gateway_app --factory --env-file .env --host 127.0.0.1 --port 8001

runtime-migrate: ## 应用 F35 Durable Runtime schema（需先 gateway-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0010_f35_runtime.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0014_f35_runtime_commands.sql
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0019_f35_worker_lifecycle.sql

runtime-test: ## F35 SQL 队列、租约与幂等验收（需先 runtime-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/tests/f35_runtime.sql

runtime-check: ## F35 Worker 端口与失败映射离线检查
	@$(VENV)/bin/ruff check src/product_api/runtime.py src/product_api/worker_main.py tests/test_product_api_runtime.py
	@PYTHONPATH=src $(VPY) -m pytest -q tests/test_product_api_runtime.py

runtime-integration-test: ## 克隆本地测试库验证 F35 SQL/RLS/Pi/Trace 后删除隔离克隆
	@PYTHONPATH=src $(VPY) -m pytest -q -m integration tests/test_product_api_runtime_integration.py

runtime-worker: ## 启动单组织 F35 Worker；需显式配置 Worker 身份和数据库
	@PYTHONPATH=src $(VPY) -m product_api.worker_main

f36-check: ## F36 Specialist、知识、CRM 事实读取与质量门禁
	@$(VENV)/bin/ruff check src/product_api/crm.py src/product_api/specialists.py src/product_api/specialist_runner.py src/product_api/mission_runtime.py src/product_api/knowledge.py src/product_api/knowledge_import.py tests/test_product_api_crm.py tests/test_product_api_specialists.py tests/test_product_api_specialist_runner.py tests/test_product_api_mission_runtime.py tests/test_product_api_knowledge.py tests/test_product_api_knowledge_import.py
	@PYTHONPATH=src $(VPY) -m pytest -q tests/test_product_api_crm.py tests/test_product_api_specialists.py tests/test_product_api_specialist_runner.py tests/test_product_api_mission_runtime.py tests/test_product_api_knowledge.py tests/test_product_api_knowledge_import.py

f36-migrate: runtime-migrate crm-migrate knowledge-migrate ## 应用 F36 Durable Mission schema
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0021_f36_mission.sql

f36-integration-test: f36-migrate ## F36 多 Specialist Mission、F40 Artifact 与 RLS 验收
	@PYTHONPATH=src $(VPY) -m pytest -q -m integration tests/test_product_api_mission_integration.py

knowledge-migrate: tenant-migrate ## 应用 F36 P1 组织知识、ACL、版本与 Citation schema
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/migrations/0020_f36_knowledge.sql

knowledge-test: ## F36 P1 PostgreSQL/RLS/ACL/Citation 验收（需先 knowledge-migrate）
	@$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $${SUPPLYAGENT_PG_USER:-postgres} -d $${SUPPLYAGENT_PG_DB:-supplyagent} < supabase/tests/f36_knowledge.sql
