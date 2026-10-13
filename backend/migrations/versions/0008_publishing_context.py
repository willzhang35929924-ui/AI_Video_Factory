"""Retain publishing source-document and account context without credentials."""
from alembic import context, op
import sqlalchemy as sa

revision = "0008_publishing_context"
down_revision = "0007_real_publishing"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("products", "publishing_accounts"):
        if context.is_offline_mode() or "publishing_context" not in {row["name"] for row in sa.inspect(op.get_bind()).get_columns(table)}:
            op.add_column(table, sa.Column("publishing_context", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    raise RuntimeError("Retain account and source-document context; use a forward migration.")
