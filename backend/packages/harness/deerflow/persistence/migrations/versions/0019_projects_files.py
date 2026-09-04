"""persistent user-scoped projects and files."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_projects_files"
down_revision: str | Sequence[str] | None = "0018_oauth_identity_pg_partial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("projects"):
        op.create_table(
            "projects",
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("project_id"),
            sa.UniqueConstraint("user_id", "name", name="uq_projects_user_name"),
        )
        op.create_index("ix_projects_user_id", "projects", ["user_id"])
    if not inspector.has_table("project_files"):
        op.create_table(
            "project_files",
            sa.Column("file_id", sa.String(length=64), nullable=False),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("display_name", sa.String(length=256), nullable=False),
            sa.Column("media_type", sa.String(length=128), nullable=True),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("relative_path", sa.String(length=512), nullable=False),
            sa.Column("source_type", sa.String(length=32), nullable=False),
            sa.Column("source_thread_id", sa.String(length=64), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("file_id"),
            sa.UniqueConstraint("project_id", "display_name", name="uq_project_files_project_name"),
        )
        op.create_index("ix_project_files_project_id", "project_files", ["project_id"])
        op.create_index("ix_project_files_user_id", "project_files", ["user_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("project_files"):
        op.drop_index("ix_project_files_user_id", table_name="project_files")
        op.drop_index("ix_project_files_project_id", table_name="project_files")
        op.drop_table("project_files")
    if inspector.has_table("projects"):
        op.drop_index("ix_projects_user_id", table_name="projects")
        op.drop_table("projects")
