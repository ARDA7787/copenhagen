"""Registry, tenant isolation, immutable specs and append-only audit grants."""

from alembic import op

from copenhagen.db.models import AuditEvent, Capability, Recipe, Tenant

revision = "0001"
down_revision = None


def upgrade() -> None:
    connection = op.get_bind()
    for table in (Tenant.__table__, Capability.__table__, Recipe.__table__, AuditEvent.__table__):
        table.create(connection)
    if connection.dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION cph_reject_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'audit_events is append-only'; END $$""")
        op.execute("""CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE OR TRUNCATE
        ON audit_events
                      FOR EACH STATEMENT EXECUTE FUNCTION cph_reject_audit_mutation()""")
        op.execute("""CREATE FUNCTION cph_immutable_spec() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN IF NEW.data IS DISTINCT FROM OLD.data OR NEW.name <> OLD.name
        OR NEW.version <> OLD.version
        THEN RAISE EXCEPTION 'published versions are immutable'; END IF; RETURN NEW; END $$""")
        for table in ("capabilities", "recipes"):
            op.execute(
                f"CREATE TRIGGER immutable_spec BEFORE UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION cph_immutable_spec()"
            )
            op.execute(f"GRANT SELECT, INSERT, UPDATE(status) ON {table} TO copenhagen_app")
        op.execute("GRANT SELECT, INSERT ON audit_events TO copenhagen_app")
        op.execute("GRANT SELECT, INSERT, UPDATE ON tenants TO copenhagen_app")


def downgrade() -> None:
    raise RuntimeError("destructive downgrade refused; restore a reviewed backup")
