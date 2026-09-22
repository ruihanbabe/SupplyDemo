"""Transactional storage operations for deterministic procurement services."""
from contextlib import contextmanager

from sqlalchemy import MetaData, Table, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert


class ProcurementRepository:
    def __init__(self, connection):
        self.connection = connection
        self.metadata = MetaData()

    def table(self, name):
        return Table(name, self.metadata, autoload_with=self.connection, extend_existing=True)

    @contextmanager
    def atomic(self):
        """Caller commits the outer transaction; failures roll back this operation."""
        with self.connection.begin_nested():
            yield

    def create_demand(self, values):
        table = self.table("demand")
        self.connection.execute(pg_insert(table).values(**values).on_conflict_do_nothing())
        stored = self.get_demand(values["demand_id"])
        if any(stored[key] != value for key, value in values.items()):
            raise ValueError("Demand ID already exists with different content")
        return stored

    def get_demand(self, demand_id):
        table = self.table("demand")
        row = self.connection.execute(select(table).where(
            table.c.demand_id == demand_id).with_for_update()).mappings().one()
        return dict(row)

    def bom(self, project_id):
        rows = self.connection.execute(text("""SELECT * FROM bom_line
            WHERE project_id=:project_id ORDER BY line_id FOR SHARE"""),
            {"project_id": project_id}).mappings()
        result = []
        for row in rows:
            candidates = self.connection.execute(text("""SELECT c.component_id,
                c.manufacturer, c.mpn, c.identity_status FROM bom_line_candidate b
                JOIN component c USING(component_id)
                WHERE b.line_id=:line_id ORDER BY b.candidate_seq FOR SHARE OF b,c"""),
                {"line_id": row["line_id"]}).mappings()
            result.append({**row, "candidates": [dict(c) for c in candidates]})
        return result

    def save_demand_lines(self, demand_id, rows):
        table = self.table("demand_line")
        existing = self.connection.execute(select(table).where(
            table.c.demand_id == demand_id)).mappings().all()
        values = [{"demand_id": demand_id, **row} for row in rows]
        if existing:
            if sorted([dict(row) for row in existing], key=lambda r: r["line_id"]) != values:
                raise ValueError("Demand expansion already exists with different content")
        elif values:
            self.connection.execute(insert(table), values)

    def demand_lines(self, demand_id):
        table = self.table("demand_line")
        return [dict(row) for row in self.connection.execute(select(table).where(
            table.c.demand_id == demand_id).order_by(table.c.line_id)).mappings()]

    def effective_rule(self, rule_id, tenant_id="default"):
        """The highest version already in force, or None.

        Versions only ever get added (data-model.md §7), so "current" is a read-time
        question: the newest row whose effective_from has passed. Returning the version
        alongside the value is not optional — a conclusion has to stay traceable to the
        rule version it was computed under.
        """
        row = self.connection.execute(text("""
            SELECT version, value FROM business_rule
            WHERE tenant_id = :tenant AND rule_id = :rule AND effective_from <= now()
            ORDER BY version DESC LIMIT 1"""),
            {"tenant": tenant_id, "rule": rule_id}).mappings().first()
        return dict(row) if row else None

    def components(self, component_ids):
        """Identity lookup for display. Empty in, empty out — never a wildcard query."""
        if not component_ids:
            return []
        table = self.table("component")
        return [dict(row) for row in self.connection.execute(
            select(table).where(table.c.component_id.in_(list(component_ids)))).mappings()]

    def list_projects(self):
        table = self.table("project")
        return [dict(row) for row in self.connection.execute(
            select(table).order_by(table.c.project_id)).mappings()]

    def read_demand(self, demand_id):
        """Lock-free read for query endpoints; get_demand locks for write paths."""
        table = self.table("demand")
        return dict(self.connection.execute(select(table).where(
            table.c.demand_id == demand_id)).mappings().one())

    def create_run(self, values):
        table = self.table("run")
        self.connection.execute(pg_insert(table).values(**values).on_conflict_do_nothing())
        stored = self.get_run(values["run_id"])
        if any(stored[key] != value for key, value in values.items()):
            raise ValueError("Run ID already exists with different content")
        return stored

    def demand_plans(self, demand_id):
        table = self.table("plan")
        return [dict(row) for row in self.connection.execute(select(table).where(
            table.c.demand_id == demand_id).order_by(table.c.version)).mappings()]

    def get_run(self, run_id):
        table = self.table("run")
        return dict(self.connection.execute(select(table).where(
            table.c.run_id == run_id)).mappings().one())

    def supply_snapshot(self, tenant_id):
        # One SELECT gives all three sources the same PostgreSQL statement snapshot.
        # Cast numeric JSON fields to text before decoding to avoid JSON floats.
        from datetime import date
        from decimal import Decimal

        rows = self.connection.execute(text("""
            SELECT 'inventory' AS kind, to_jsonb(i) || jsonb_build_object(
                'on_hand_qty',i.on_hand_qty::text,'quarantine_qty',i.quarantine_qty::text) AS data
            FROM inventory i WHERE tenant_id=:tenant
            UNION ALL
            SELECT 'allocations', to_jsonb(a) || jsonb_build_object(
                'allocated_qty',a.allocated_qty::text)
            FROM inventory_allocation a WHERE tenant_id=:tenant
            UNION ALL
            SELECT 'transit', to_jsonb(t) || jsonb_build_object('qty',t.qty::text)
            FROM in_transit t WHERE tenant_id=:tenant
        """), {"tenant": tenant_id})
        result = {"inventory": [], "allocations": [], "transit": []}
        for kind, data in rows:
            for field in ("on_hand_qty", "quarantine_qty", "allocated_qty", "qty"):
                if field in data:
                    data[field] = Decimal(data[field])
            if data.get("eta"):
                data["eta"] = date.fromisoformat(data["eta"])
            result[kind].append(data)
        return result

    def insert_shortage(self, values):
        self.connection.execute(insert(self.table("shortage_snapshot")).values(**values))

    def shortage_snapshots(self, run_id, snapshot_ids):
        if not snapshot_ids or len(set(snapshot_ids)) != len(snapshot_ids):
            raise ValueError("Supply nonempty unique snapshot IDs")
        table = self.table("shortage_snapshot")
        rows = [dict(row) for row in self.connection.execute(select(table).where(
            table.c.run_id == run_id, table.c.snapshot_id.in_(snapshot_ids)).order_by(
            table.c.component_id)).mappings()]
        if len(rows) != len(snapshot_ids):
            raise ValueError("Snapshot missing or belongs to a different run")
        return rows

    def next_plan_version(self, demand_id):
        # Caller must already hold the demand row FOR UPDATE until commit.
        return self.connection.scalar(text("SELECT COALESCE(max(version),0)+1 FROM plan "
                                           "WHERE demand_id=:id"), {"id": demand_id})

    def insert_plan(self, plan, lines):
        self.connection.execute(insert(self.table("plan")).values(**plan))
        if lines:
            self.connection.execute(insert(self.table("plan_line")),
                                    [{"plan_id": plan["plan_id"], **line} for line in lines])

    def read_plan(self, plan_id):
        header = self.table("plan")
        lines = self.table("plan_line")
        plan = dict(self.connection.execute(select(header).where(
            header.c.plan_id == plan_id)).mappings().one())
        plan["lines"] = [dict(row) for row in self.connection.execute(select(lines).where(
            lines.c.plan_id == plan_id).order_by(lines.c.component_id)).mappings()]
        return plan
