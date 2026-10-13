"""Persist paid description requests and cloud readback without credentials."""
from alembic import context, op
import sqlalchemy as sa

revision = "0009_description_generation"
down_revision = "0008_publishing_context"
branch_labels = None
depends_on = None


def upgrade():
    if not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table("description_generations"):
        return
    op.create_table("description_generations",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("product_id", sa.String(100), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("country", sa.String(100), nullable=False),
        sa.Column("keyword", sa.String(300), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("descriptions", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(100)), sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_index("ix_description_generations_product_id", "description_generations", ["product_id"])


def downgrade():
    raise RuntimeError("Retain paid request and description history; use a forward migration.")
