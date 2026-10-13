"""Persistent production queue, prompt versions, quality and workflow run lineage."""
from alembic import op
import sqlalchemy as sa

revision = "0004_production"
down_revision = "0003_error_retryable"
branch_labels = None
depends_on = None


def upgrade():
    for name, type_ in (("processing_run_id", sa.String(100)), ("prompt_id", sa.String(100)), ("sha256", sa.String(64))):
        op.add_column("workflow_snapshots", sa.Column(name, type_))
    for column in (sa.Column("ordinal", sa.Integer()), sa.Column("status", sa.String(30), server_default="MOCK", nullable=False),
                   sa.Column("processing_run_id", sa.String(100)), sa.Column("fingerprint", sa.String(64))):
        op.add_column("prompts", column)
    op.create_table("production_batches", sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("status", sa.String(40), nullable=False), sa.Column("task_ids", sa.JSON(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False), sa.Column("active_task_id", sa.String(100)),
        sa.Column("summary", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("quality_assessments", sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("task_id", sa.String(100), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("asset_id", sa.String(100), sa.ForeignKey("video_assets.id"), nullable=False),
        sa.Column("processing_run_id", sa.String(100), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("quality_score", sa.Float()), sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("issue_segments", sa.JSON(), nullable=False), sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_quality_assessments_task_id", "quality_assessments", ["task_id"])


def downgrade():
    raise RuntimeError("Production history is retained. Restore a protected backup instead of deleting it.")
