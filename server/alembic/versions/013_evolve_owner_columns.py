"""Add row-level owner (user_id) to evolve_salience and evolve_feedback

Backfills from the mem0_memories vector-store payload so existing rows are
immediately usable by the ownership fast path.

Revision ID: 013
Revises: 012
Create Date: 2026-08-23

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("evolve_salience", sa.Column("user_id", sa.String(length=255), nullable=True))
    op.add_column("evolve_feedback", sa.Column("user_id", sa.String(length=255), nullable=True))
    op.create_index("ix_evolve_salience_user_id", "evolve_salience", ["user_id"])
    op.create_index("ix_evolve_feedback_user_id", "evolve_feedback", ["user_id"])

    # Backfill owners from the vector-store payload. mem0_memories.id is uuid,
    # evolve_*.memory_id is varchar — compare via CAST. Rows whose memory has
    # since been deleted keep NULL and fall back to the payload lookup path.
    op.execute(
        """
        UPDATE evolve_salience s
        SET user_id = m.owner
        FROM (
            SELECT CAST(id AS VARCHAR) AS mid, payload->>'user_id' AS owner
            FROM mem0_memories
            WHERE payload->>'user_id' IS NOT NULL
        ) m
        WHERE s.memory_id = m.mid AND s.user_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE evolve_feedback f
        SET user_id = s.user_id
        FROM evolve_salience s
        WHERE f.memory_id = s.memory_id AND f.user_id IS NULL AND s.user_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_evolve_feedback_user_id", table_name="evolve_feedback")
    op.drop_index("ix_evolve_salience_user_id", table_name="evolve_salience")
    op.drop_column("evolve_feedback", "user_id")
    op.drop_column("evolve_salience", "user_id")
