"""F11 数据库层权限：Agent 侧只读角色与 Procurement 执行器角色（spec §5.5"数据库层权限"）。

- supplyagent_agent_ro：对全部业务表只有 SELECT（Agent 侧连接用它，代码有 bug 也写不进去）。
- supplyagent_procurement：同样只读，另可 INSERT env_write（采购单写入环境；env_write 本身只追加，触发器禁改删）。
两者都是 NOLOGIN 角色（集群级，已存在则跳过）；应用以登录角色连接并继承它们归 F29 部署。当前 schema 授予 USAGE。
依赖：0001–0005 的表。被 tests/test_permissions.py 集成测试验证。
"""
from alembic import op  # alembic 迁移操作

revision = "0006"  # 本迁移编号
down_revision = "0005"  # 上一个迁移
branch_labels = None  # 无分支
depends_on = None  # 无额外依赖

ROLES = ("supplyagent_agent_ro", "supplyagent_procurement")  # 两个角色
TABLES = ("hw_part", "env_snapshot", "env_write", "session_event", "artifact", "artifact_dep",  # 全部业务表
          "tool_record", "metering")  # 留档与计量


# 功能：建角色（若无）并授权。依赖：前序迁移的表。完成：schema USAGE、全表只读、procurement 可追加 env_write。
def upgrade():
    for role in ROLES:  # 每个角色
        op.execute(f"""DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} NOLOGIN;
            END IF;
        END $$""")  # 集群级角色，可能已由别的库建过
        op.execute(f"DO $$ BEGIN EXECUTE format('GRANT USAGE ON SCHEMA %I TO {role}', current_schema()); END $$")  # 当前 schema
        op.execute(f"GRANT SELECT ON {', '.join(TABLES)} TO {role}")  # 只读
    op.execute("GRANT INSERT ON env_write TO supplyagent_procurement")  # 采购写入（只追加）


# 功能：撤销授权（保留集群级角色：别的库可能在用）。依赖：无。完成：表权限与 schema USAGE 全部收回。
def downgrade():
    for role in ROLES:  # 每个角色
        op.execute(f"REVOKE ALL ON {', '.join(TABLES)} FROM {role}")  # 收回表权限
        op.execute(f"DO $$ BEGIN EXECUTE format('REVOKE USAGE ON SCHEMA %I FROM {role}', current_schema()); END $$")  # 收回 USAGE
