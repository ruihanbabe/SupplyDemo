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
        services-up services-status services-smoke services-down freeze migrate test-integration

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
	$(VPY) -m pytest -q -m "not integration" tests

check: compile lint test ## ①② 两层的离线门禁（不证明真实服务与业务正确性）
	@echo ">> check 完成：仅覆盖离线语法/lint/测试"

test-integration: ## ③ 集成故障注入：连本地真实 PostgreSQL / Redis，需先 services-up
	@if [ ! -d tests ]; then echo ">> 尚无 tests/ 目录，test-integration 跳过（不算通过）"; exit 0; fi; \
	$(COMPOSE) ps --status running --quiet postgres >/dev/null 2>&1 || { echo ">> 服务未启动，先跑 make services-up"; exit 1; }; \
	$(VPY) -m pytest -q -m integration tests

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
