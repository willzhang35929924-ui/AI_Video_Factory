"""Adopt the existing Phase 1 schema, or create its fixed baseline."""
from alembic import context, op
import sqlalchemy as sa

revision = "0001_phase1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    if not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table("tasks"):
        return
    op.create_table('countries',
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('factory_settings',
    sa.Column('theme', sa.String(length=20), nullable=False),
    sa.Column('language', sa.String(length=20), nullable=False),
    sa.Column('output_width', sa.Integer(), nullable=False),
    sa.Column('output_height', sa.Integer(), nullable=False),
    sa.Column('output_fps', sa.Float(), nullable=False),
    sa.Column('default_provider', sa.String(length=80), nullable=False),
    sa.Column('mock_delay_ms', sa.Integer(), nullable=False),
    sa.Column('review_required', sa.Boolean(), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('users',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('products',
    sa.Column('country_id', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('country', sa.String(length=100), nullable=False),
    sa.Column('image', sa.Text(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('binding_reference', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('keywords', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['country_id'], ['countries.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('projects',
    sa.Column('user_id', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('publishing_accounts',
    sa.Column('country_id', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('country', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('authorization', sa.String(length=200), nullable=False),
    sa.Column('secret_ref', sa.String(length=200), nullable=True),
    sa.Column('last_publish', sa.DateTime(timezone=True), nullable=True),
    sa.Column('queue', sa.Integer(), nullable=False),
    sa.Column('errors', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['country_id'], ['countries.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('account_products',
    sa.Column('account_id', sa.String(length=100), nullable=False),
    sa.Column('product_id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['publishing_accounts.id'], ),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.PrimaryKeyConstraint('account_id', 'product_id')
    )
    op.create_table('keywords',
    sa.Column('country_id', sa.String(length=100), nullable=False),
    sa.Column('product_id', sa.String(length=100), nullable=True),
    sa.Column('text', sa.String(length=300), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['country_id'], ['countries.id'], ),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('collector_rules',
    sa.Column('country_id', sa.String(length=100), nullable=False),
    sa.Column('keyword_id', sa.String(length=100), nullable=False),
    sa.Column('country', sa.String(length=100), nullable=False),
    sa.Column('keyword', sa.String(length=300), nullable=False),
    sa.Column('product_id', sa.String(length=100), nullable=True),
    sa.Column('product_image', sa.Text(), nullable=False),
    sa.Column('min_views', sa.Integer(), nullable=False),
    sa.Column('min_likes', sa.Integer(), nullable=False),
    sa.Column('min_ratio', sa.Float(), nullable=False),
    sa.Column('match_threshold', sa.Float(), nullable=False),
    sa.Column('filter_overlay', sa.Boolean(), nullable=False),
    sa.Column('filter_watermark', sa.Boolean(), nullable=False),
    sa.Column('filter_subtitle', sa.Boolean(), nullable=False),
    sa.Column('views', sa.Integer(), nullable=False),
    sa.Column('likes', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['country_id'], ['countries.id'], ),
    sa.ForeignKeyConstraint(['keyword_id'], ['keywords.id'], ),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('tasks',
    sa.Column('project_id', sa.String(length=100), nullable=False),
    sa.Column('country_id', sa.String(length=100), nullable=False),
    sa.Column('keyword_id', sa.String(length=100), nullable=False),
    sa.Column('country', sa.String(length=100), nullable=False),
    sa.Column('keyword', sa.String(length=300), nullable=False),
    sa.Column('product_id', sa.String(length=100), nullable=False),
    sa.Column('source_video_id', sa.String(length=100), nullable=True),
    sa.Column('current_stage', sa.String(length=30), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('progress', sa.Float(), nullable=False),
    sa.Column('thumbnail', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['country_id'], ['countries.id'], ),
    sa.ForeignKeyConstraint(['keyword_id'], ['keywords.id'], ),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['source_video_id'], ['source_videos.id'], name='fk_task_source', use_alter=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('audit_logs',
    sa.Column('task_id', sa.String(length=100), nullable=True),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('entity_id', sa.String(length=100), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_logs_task_id'), 'audit_logs', ['task_id'], unique=False)
    op.create_table('error_logs',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('stage', sa.String(length=30), nullable=False),
    sa.Column('code', sa.String(length=100), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_error_logs_task_id'), 'error_logs', ['task_id'], unique=False)
    op.create_table('processing_runs',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('stage', sa.String(length=30), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('progress', sa.Float(), nullable=False),
    sa.Column('logs', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_processing_runs_task_id'), 'processing_runs', ['task_id'], unique=False)
    op.create_table('prompts',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('negative_prompt', sa.Text(), nullable=False),
    sa.Column('provider', sa.String(length=80), nullable=False),
    sa.Column('scene_type', sa.String(length=100), nullable=False),
    sa.Column('style', sa.String(length=100), nullable=False),
    sa.Column('lighting', sa.String(length=100), nullable=False),
    sa.Column('color', sa.String(length=100), nullable=False),
    sa.Column('aspect_ratio', sa.String(length=30), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_prompts_task_id'), 'prompts', ['task_id'], unique=False)
    op.create_table('source_videos',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('original_filename', sa.Text(), nullable=False),
    sa.Column('source_url', sa.Text(), nullable=True),
    sa.Column('fps', sa.Float(), nullable=False),
    sa.Column('width', sa.Integer(), nullable=False),
    sa.Column('height', sa.Integer(), nullable=False),
    sa.Column('duration', sa.Float(), nullable=False),
    sa.Column('codec', sa.String(length=50), nullable=False),
    sa.Column('views', sa.Integer(), nullable=False),
    sa.Column('likes', sa.Integer(), nullable=False),
    sa.Column('ai_match_score', sa.Float(), nullable=True),
    sa.Column('product_match_score', sa.Float(), nullable=True),
    sa.Column('overlay_detected', sa.Boolean(), nullable=True),
    sa.Column('watermark_detected', sa.Boolean(), nullable=True),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_source_videos_task_id'), 'source_videos', ['task_id'], unique=False)
    op.create_table('task_stages',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=30), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('start_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('end_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration', sa.Float(), nullable=False),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_task_stages_task_id'), 'task_stages', ['task_id'], unique=False)
    op.create_table('video_assets',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('type', sa.String(length=30), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('thumbnail', sa.Text(), nullable=False),
    sa.Column('duration', sa.Float(), nullable=False),
    sa.Column('width', sa.Integer(), nullable=False),
    sa.Column('height', sa.Integer(), nullable=False),
    sa.Column('fps', sa.Float(), nullable=False),
    sa.Column('codec', sa.String(length=50), nullable=False),
    sa.Column('country', sa.String(length=100), nullable=False),
    sa.Column('keyword', sa.String(length=300), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('storage_path', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_video_assets_task_id'), 'video_assets', ['task_id'], unique=False)
    op.create_table('workflow_snapshots',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('version', sa.String(length=80), nullable=False),
    sa.Column('workflow', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_workflow_snapshots_task_id'), 'workflow_snapshots', ['task_id'], unique=False)
    op.create_table('background_assets',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('asset_id', sa.String(length=100), nullable=False),
    sa.Column('prompt_id', sa.String(length=100), nullable=True),
    sa.Column('provider', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['video_assets.id'], ),
    sa.ForeignKeyConstraint(['prompt_id'], ['prompts.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_background_assets_task_id'), 'background_assets', ['task_id'], unique=False)
    op.create_table('cutout_assets',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('asset_id', sa.String(length=100), nullable=False),
    sa.Column('workflow_id', sa.String(length=100), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=False),
    sa.Column('model_version', sa.String(length=80), nullable=False),
    sa.Column('alpha_quality', sa.Float(), nullable=False),
    sa.Column('temporal_stability', sa.Float(), nullable=False),
    sa.Column('missing_body_score', sa.Float(), nullable=False),
    sa.Column('flicker_score', sa.Float(), nullable=False),
    sa.Column('intervals', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['video_assets.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflow_snapshots.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cutout_assets_task_id'), 'cutout_assets', ['task_id'], unique=False)
    op.create_table('publishing_tasks',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('video_id', sa.String(length=100), nullable=False),
    sa.Column('account_id', sa.String(length=100), nullable=True),
    sa.Column('country', sa.String(length=100), nullable=False),
    sa.Column('product_id', sa.String(length=100), nullable=False),
    sa.Column('caption', sa.Text(), nullable=False),
    sa.Column('product_binding', sa.String(length=200), nullable=False),
    sa.Column('schedule', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['publishing_accounts.id'], ),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.ForeignKeyConstraint(['video_id'], ['video_assets.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_publishing_tasks_task_id'), 'publishing_tasks', ['task_id'], unique=False)
    op.create_table('reviews',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('asset_id', sa.String(length=100), nullable=False),
    sa.Column('reviewer_id', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('issues', sa.JSON(), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['video_assets.id'], ),
    sa.ForeignKeyConstraint(['reviewer_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_reviews_task_id'), 'reviews', ['task_id'], unique=False)
    op.create_table('composition_assets',
    sa.Column('task_id', sa.String(length=100), nullable=False),
    sa.Column('asset_id', sa.String(length=100), nullable=False),
    sa.Column('source_video_id', sa.String(length=100), nullable=True),
    sa.Column('background_id', sa.String(length=100), nullable=True),
    sa.Column('person_asset_id', sa.String(length=100), nullable=True),
    sa.Column('parameters', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['video_assets.id'], ),
    sa.ForeignKeyConstraint(['background_id'], ['background_assets.id'], ),
    sa.ForeignKeyConstraint(['person_asset_id'], ['video_assets.id'], ),
    sa.ForeignKeyConstraint(['source_video_id'], ['source_videos.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_composition_assets_task_id'), 'composition_assets', ['task_id'], unique=False)
    if op.get_bind().dialect.name != "sqlite":
        op.create_foreign_key("fk_task_source", "tasks", "source_videos", ["source_video_id"], ["id"])


def downgrade():
    raise RuntimeError("The adopted Phase 1 data is retained; destructive baseline downgrade is not supported.")

