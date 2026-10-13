"""One canonical identity for every native platform video; attempts remain intact."""
from alembic import context, op
import sqlalchemy as sa

revision = "0011_collection_identity"
down_revision = "0010_asset_identity"
branch_labels = None
depends_on = None


def upgrade():
    if not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table("collection_identities"):
        return
    op.create_table("collection_identities",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("platform", sa.String(30), nullable=False),
        sa.Column("native_video_id", sa.String(100), nullable=False),
        sa.Column("canonical_task_id", sa.String(100), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("confidence", sa.String(30), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("platform", "native_video_id", name="uq_collection_platform_video"))
    op.create_index("ix_collection_identities_canonical_task_id", "collection_identities", ["canonical_task_id"])


def downgrade():
    raise RuntimeError("Keep source identity and encounter history; restore the verified pre-migration backup offline.")
