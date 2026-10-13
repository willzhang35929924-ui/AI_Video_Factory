"""Persist production policy and process supervision without replacing history."""
from alembic import context, op
import sqlalchemy as sa

revision = "0006_production_supervision"
down_revision = "0005_workflow_execution"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        "processing_runs": [
            sa.Column("last_heartbeat", sa.DateTime(timezone=True)),
            sa.Column("termination_signal", sa.Integer()),
            sa.Column("timed_out", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("resource_info", sa.JSON(), nullable=False, server_default="{}"),
        ],
        "factory_settings": [sa.Column("production_policy", sa.JSON(), nullable=False, server_default="{}")],
    }
    for table, additions in columns.items():
        existing = set() if context.is_offline_mode() else {row["name"] for row in sa.inspect(op.get_bind()).get_columns(table)}
        for column in additions:
            if column.name not in existing:
                op.add_column(table, column)


def downgrade():
    raise RuntimeError("Retain production history; use a forward migration for rollback.")
