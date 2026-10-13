"""Correct the early Collector revision's quoted false default without changing real retry decisions."""
from alembic import op
import sqlalchemy as sa

revision = "0003_error_retryable"
down_revision = "0002_collector_real"
branch_labels = None
depends_on = None


def error_table():
    return sa.Table(
        "error_logs", sa.MetaData(),
        sa.Column("task_id", sa.String(100), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("provider", sa.String(100), server_default="mock", nullable=False),
        sa.Column("details", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Index("ix_error_logs_task_id", "task_id"),
    )


def upgrade():
    table = error_table()
    op.execute(table.update().where(table.c.provider == "mock").values(retryable=False))
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("error_logs", copy_from=table, recreate="always") as batch:
            batch.alter_column("retryable", existing_type=sa.Boolean(), server_default=sa.false(), existing_nullable=False)
    else:
        op.alter_column("error_logs", "retryable", existing_type=sa.Boolean(), server_default=sa.false(), existing_nullable=False)


def downgrade():
    raise RuntimeError("The invalid Boolean default is not restored.")
