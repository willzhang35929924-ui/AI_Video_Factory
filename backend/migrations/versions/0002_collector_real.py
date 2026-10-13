"""Persist Collector real integration and retain existing Mock records."""
from alembic import context, op
import sqlalchemy as sa

revision = "0002_collector_real"
down_revision = "0001_phase1"
branch_labels = None
depends_on = None


def _add(table, column):
    if context.is_offline_mode() or column.name not in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}:
        op.add_column(table, column)


def _index(name, table, columns, unique=False):
    if context.is_offline_mode() or name not in {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns, unique=unique)


def upgrade():
    if context.is_offline_mode() or not sa.inspect(op.get_bind()).has_table("legacy_bindings"):
        op.create_table('legacy_bindings',
        sa.Column('task_id', sa.String(length=100), nullable=False),
        sa.Column('project_id', sa.String(length=100), nullable=False),
        sa.Column('product_id', sa.String(length=100), nullable=False),
        sa.Column('source_system', sa.String(length=100), server_default='TikTok_Auto', nullable=False),
        sa.Column('context_id', sa.String(length=100), nullable=True),
        sa.Column('source_document', sa.Text(), nullable=True),
        sa.Column('source_row', sa.Integer(), nullable=True),
        sa.Column('video_id', sa.String(length=100), nullable=True),
        sa.Column('download_task_id', sa.String(length=100), nullable=True),
        sa.Column('source_path', sa.Text(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('legacy_reference', sa.JSON(), server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('id', sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
    _index("ix_legacy_bindings_context_id", "legacy_bindings", ["context_id"])
    _index("ix_legacy_bindings_task_id", "legacy_bindings", ["task_id"])
    _add('collector_rules', sa.Column('input_source', sa.String(length=30), server_default='manual', nullable=False))
    _add('collector_rules', sa.Column('video_url', sa.Text(), nullable=True))
    _add('collector_rules', sa.Column('raw_keyword', sa.String(length=300), nullable=True))
    _add('collector_rules', sa.Column('product_images', sa.JSON(), server_default='[]', nullable=False))
    _add('collector_rules', sa.Column('context_id', sa.String(length=100), nullable=True))
    _add('collector_rules', sa.Column('source_document', sa.Text(), nullable=True))
    _add('collector_rules', sa.Column('source_row', sa.Integer(), nullable=True))
    _add('collector_rules', sa.Column('last_task_id', sa.String(length=100), nullable=True))
    _add('collector_rules', sa.Column('last_run_id', sa.String(length=100), nullable=True))
    _add('collector_rules', sa.Column('result', sa.JSON(), server_default='{}', nullable=False))
    _add('error_logs', sa.Column('provider', sa.String(length=100), server_default='mock', nullable=False))
    _add('error_logs', sa.Column('details', sa.JSON(), server_default='{}', nullable=False))
    _add('error_logs', sa.Column('retryable', sa.Boolean(), server_default=sa.false(), nullable=False))
    _add('processing_runs', sa.Column('provider', sa.String(length=100), server_default='mock', nullable=False))
    _add('processing_runs', sa.Column('command', sa.JSON(), server_default='[]', nullable=False))
    _add('processing_runs', sa.Column('cwd', sa.Text(), nullable=True))
    _add('processing_runs', sa.Column('pid', sa.Integer(), nullable=True))
    _add('processing_runs', sa.Column('started_at', sa.DateTime(timezone=True), nullable=True))
    _add('processing_runs', sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True))
    _add('processing_runs', sa.Column('duration_ms', sa.Integer(), nullable=True))
    _add('processing_runs', sa.Column('exit_code', sa.Integer(), nullable=True))
    _add('processing_runs', sa.Column('stdout_log_path', sa.Text(), nullable=True))
    _add('processing_runs', sa.Column('stderr_log_path', sa.Text(), nullable=True))
    _add('processing_runs', sa.Column('error_code', sa.String(length=100), nullable=True))
    _add('processing_runs', sa.Column('error_message', sa.Text(), nullable=True))
    _add('processing_runs', sa.Column('retry_count', sa.Integer(), server_default='0', nullable=False))
    _add('processing_runs', sa.Column('idempotency_key', sa.String(length=200), nullable=True))
    _index(op.f('ix_processing_runs_idempotency_key'), 'processing_runs', ['idempotency_key'], unique=False)
    _add('source_videos', sa.Column('external_path', sa.Text(), nullable=True))
    _add('source_videos', sa.Column('sha256', sa.String(length=64), nullable=True))
    _add('source_videos', sa.Column('size', sa.BigInteger(), nullable=True))
    _add('source_videos', sa.Column('mime_type', sa.String(length=100), nullable=True))
    _add('source_videos', sa.Column('source_system', sa.String(length=100), nullable=True))
    _add('source_videos', sa.Column('legacy_video_id', sa.String(length=100), nullable=True))
    _add('source_videos', sa.Column('legacy_context_id', sa.String(length=100), nullable=True))
    _add('source_videos', sa.Column('download_task_id', sa.String(length=100), nullable=True))
    _add('source_videos', sa.Column('r_frame_rate', sa.String(length=50), nullable=True))
    _add('source_videos', sa.Column('avg_frame_rate', sa.String(length=50), nullable=True))
    _add('source_videos', sa.Column('legacy_reference', sa.JSON(), server_default='{}', nullable=False))
    _add('source_videos', sa.Column('probe_metadata', sa.JSON(), server_default='{}', nullable=False))
    _add('tasks', sa.Column('execution_mode', sa.String(length=30), server_default='mock', nullable=False))
    _add('tasks', sa.Column('context', sa.JSON(), server_default='{}', nullable=False))
    _add('tasks', sa.Column('collector_result', sa.JSON(), server_default='{}', nullable=False))
    _add('video_assets', sa.Column('asset_type', sa.String(length=50), nullable=True))
    _add('video_assets', sa.Column('external_path', sa.Text(), nullable=True))
    _add('video_assets', sa.Column('sha256', sa.String(length=64), nullable=True))
    _add('video_assets', sa.Column('size', sa.BigInteger(), nullable=True))
    _add('video_assets', sa.Column('mime_type', sa.String(length=100), nullable=True))
    _add('video_assets', sa.Column('source_system', sa.String(length=100), nullable=True))
    _add('video_assets', sa.Column('source_url', sa.Text(), nullable=True))
    _add('video_assets', sa.Column('legacy_video_id', sa.String(length=100), nullable=True))
    _add('video_assets', sa.Column('legacy_context_id', sa.String(length=100), nullable=True))
    _add('video_assets', sa.Column('registry_status', sa.String(length=30), server_default='mock', nullable=False))
    _add('video_assets', sa.Column('legacy_reference', sa.JSON(), server_default='{}', nullable=False))
    _add('video_assets', sa.Column('probe_metadata', sa.JSON(), server_default='{}', nullable=False))


def downgrade():
    raise RuntimeError("Collector integration records are retained; destructive downgrade is not supported.")
