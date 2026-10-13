"""Add isolated real-publishing checkpoints without changing retained Factory fields."""
from alembic import context, op
import sqlalchemy as sa

revision = "0007_real_publishing"
down_revision = "0006_production_supervision"
branch_labels = None
depends_on = None


def upgrade():
    offline = context.is_offline_mode()
    inspector = None if offline else sa.inspect(op.get_bind())
    def create_table(name, *columns):
        if offline or not inspector.has_table(name):
            op.create_table(name, *columns)
    create_table("publishing_batches",
        sa.Column("id", sa.String(100), primary_key=True), sa.Column("status", sa.String(40), nullable=False),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("settings_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("active_task_id", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("concurrency = 1", name="ck_publishing_batch_serial"))
    additions = {
        "products": [sa.Column("platform_product_id", sa.String(200)),
            sa.Column("descriptions", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("description_selection", sa.JSON(), nullable=False, server_default="{}")],
        "publishing_accounts": [sa.Column("hubstudio_env_id", sa.String(100)), sa.Column("cdp_endpoint", sa.Text()),
            sa.Column("expected_username", sa.String(200)),
            sa.Column("identity_verification", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("vpn_verification", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default="120")],
        "publishing_tasks": [sa.Column("batch_id", sa.String(100)), sa.Column("selected_description", sa.JSON()),
            sa.Column("input_sha256", sa.String(64)), sa.Column("current_step", sa.String(40), nullable=False, server_default="PREFLIGHT"),
            sa.Column("checkpoint", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("submission_state", sa.String(40), nullable=False, server_default="NOT_SUBMITTED"),
            sa.Column("platform_video_id", sa.String(200)), sa.Column("platform_url", sa.Text()),
            sa.Column("moderation_status", sa.String(40), nullable=False, server_default="NOT_CHECKED"),
            sa.Column("product_link_status", sa.String(40), nullable=False, server_default="NOT_CHECKED"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())],
    }
    for table, columns in additions.items():
        existing = set() if offline else {row["name"] for row in inspector.get_columns(table)}
        columns = [column for column in columns if column.name not in existing]
        if table == "publishing_tasks":
            with op.batch_alter_table(table) as batch:
                for column in columns:
                    batch.add_column(column)
                if "batch_id" not in existing:
                    batch.create_foreign_key("fk_publishing_task_batch", "publishing_batches", ["batch_id"], ["id"])
                    batch.create_index("ix_publishing_tasks_batch_id", ["batch_id"])
        else:
            for column in columns:
                op.add_column(table, column)
    create_table("publish_runs",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("publishing_task_id", sa.String(100), sa.ForeignKey("publishing_tasks.id"), nullable=False),
        sa.Column("batch_id", sa.String(100), sa.ForeignKey("publishing_batches.id"), nullable=False),
        sa.Column("status", sa.String(40), nullable=False), sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        *[sa.Column(name, sa.DateTime(timezone=True)) for name in ("started_at", "finished_at", "last_heartbeat")],
        sa.Column("error_code", sa.String(100)), sa.Column("error_message", sa.Text()),
        sa.Column("logs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.CheckConstraint("attempt >= 1", name="ck_publish_run_attempt"))
    create_table("publishing_step_executions",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("publish_run_id", sa.String(100), sa.ForeignKey("publish_runs.id"), nullable=False),
        sa.Column("publishing_task_id", sa.String(100), sa.ForeignKey("publishing_tasks.id"), nullable=False),
        sa.Column("name", sa.String(40), nullable=False), sa.Column("status", sa.String(40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("checkpoint", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_code", sa.String(100)), sa.Column("error_message", sa.Text()))
    create_table("publishing_resource_locks",
        sa.Column("resource_key", sa.String(200), primary_key=True),
        sa.Column("publishing_task_id", sa.String(100), sa.ForeignKey("publishing_tasks.id"), nullable=False),
        sa.Column("publish_run_id", sa.String(100), sa.ForeignKey("publish_runs.id")),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("released_at", sa.DateTime(timezone=True)))
    create_table("publishing_description_usage",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("product_id", sa.String(100), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("account_id", sa.String(100), sa.ForeignKey("publishing_accounts.id"), nullable=False),
        sa.Column("publishing_task_id", sa.String(100), sa.ForeignKey("publishing_tasks.id"), nullable=False),
        sa.Column("description_id", sa.String(100), nullable=False), sa.Column("description_sha256", sa.String(64), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("publishing_task_id", name="uq_description_usage_task"))
    for table, columns in {"publish_runs": ["publishing_task_id", "batch_id"],
            "publishing_step_executions": ["publish_run_id", "publishing_task_id"],
            "publishing_resource_locks": ["publishing_task_id"],
            "publishing_description_usage": ["product_id", "account_id"]}.items():
        for column in columns:
            name = f"ix_{table}_{column}"
            existing = set() if offline else {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table)}
            if name not in existing:
                op.create_index(name, table, [column])


def downgrade():
    raise RuntimeError("Retain publishing submission history and locks; use a forward migration.")
