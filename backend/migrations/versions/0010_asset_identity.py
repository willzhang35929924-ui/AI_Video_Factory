"""Delete registry records while keeping review and execution identities intact."""
from alembic import context, op
import sqlalchemy as sa

revision = "0010_asset_identity"
down_revision = "0009_description_generation"
branch_labels = None
depends_on = None

REFERENCES = {
    "cutout_assets": ["asset_id"], "quality_assessments": ["asset_id"],
    "background_assets": ["asset_id"], "composition_assets": ["asset_id", "person_asset_id"],
    "reviews": ["asset_id"], "publishing_tasks": ["video_id"],
}
NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def upgrade():
    offline = context.is_offline_mode()
    inspector = None if offline else sa.inspect(op.get_bind())
    if offline or not inspector.has_table("asset_identities"):
        op.create_table("asset_identities",
            sa.Column("id", sa.String(100), primary_key=True),
            sa.Column("task_id", sa.String(100), sa.ForeignKey("tasks.id"), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
            sa.Column("deletion_snapshot", sa.JSON(), nullable=False, server_default="{}"))
        op.create_index("ix_asset_identities_task_id", "asset_identities", ["task_id"])
    op.execute(sa.text("INSERT INTO asset_identities (id, task_id, deletion_snapshot) "
        "SELECT a.id, a.task_id, '{}' FROM video_assets a WHERE NOT EXISTS "
        "(SELECT 1 FROM asset_identities i WHERE i.id = a.id)"))
    for table, columns in REFERENCES.items():
        foreign_keys = [] if offline else sa.inspect(op.get_bind()).get_foreign_keys(table)
        pending = [column for column in columns if offline or not any(
            key["constrained_columns"] == [column] and key["referred_table"] == "asset_identities" for key in foreign_keys)]
        if not pending:
            continue
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            for column in pending:
                old = None if offline else next(key for key in foreign_keys if key["constrained_columns"] == [column])
                name = f"{table}_{column}_fkey" if offline else old["name"] or f"fk_{table}_{column}_video_assets"
                batch.drop_constraint(name, type_="foreignkey")
                batch.create_foreign_key(f"fk_{table}_{column}_asset_identity", "asset_identities", [column], ["id"])
    foreign_keys = [] if offline else sa.inspect(op.get_bind()).get_foreign_keys("video_assets")
    if offline or not any(key["constrained_columns"] == ["id"] and key["referred_table"] == "asset_identities" for key in foreign_keys):
        with op.batch_alter_table("video_assets", naming_convention=NAMING) as batch:
            batch.create_foreign_key("fk_video_assets_identity", "asset_identities", ["id"], ["id"])


def downgrade():
    raise RuntimeError("Retain deleted-asset identities and history; use a forward migration.")
