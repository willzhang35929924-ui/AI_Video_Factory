"""Keep the original Comfy execution identity separately from cache registration."""
from alembic import context, op
import sqlalchemy as sa

revision = "0005_workflow_execution"
down_revision = "0004_production"
branch_labels = None
depends_on = None


def upgrade():
    if context.is_offline_mode() or "execution" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("workflow_snapshots")}:
        op.add_column("workflow_snapshots", sa.Column("execution", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    raise RuntimeError("Workflow execution history is retained; restore a protected backup instead.")
