"""events: identificadores cito

Identificadores externos do evento na Cito (SPEC 007, M6, Slice 03 -- RF-04).
Migration **aditiva** e reversível, aplicada antes de qualquer sincronização de
catálogo (o dado vem do comando ``ingestion.cito.sync_catalog``):

- ``events.cito_slug`` (``String(128)``, nullable, **indexado**) -- o slug do evento
  no catálogo da Cito, lido do catálogo real; o índice serve o acesso do backfill,
  que parte do slug para o evento persistido.
- ``events.cito_event_id`` (``String(64)``, nullable) -- o uuid textual do evento na
  Cito, o outro identificador que o catálogo expõe.

Ambas nullable por decisão: um evento sem correspondência no catálogo permanece nulo
-- ausência explícita, nunca sentinela. Autogerada via ``alembic revision
--autogenerate`` e **revisada à mão**: nenhuma coluna pré-existente muda de tipo ou de
nulidade, nenhum enum é tocado, e o ``downgrade`` é simétrico (dropa só o índice e as
duas colunas aditivas).

Revision ID: fd5a802eac7b
Revises: ff6591f2d146
Create Date: 2026-08-31 23:10:57.539135

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fd5a802eac7b'
down_revision: Union[str, Sequence[str], None] = 'ff6591f2d146'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('events', sa.Column('cito_slug', sa.String(length=128), nullable=True))
    op.add_column('events', sa.Column('cito_event_id', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_events_cito_slug'), 'events', ['cito_slug'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_events_cito_slug'), table_name='events')
    op.drop_column('events', 'cito_event_id')
    op.drop_column('events', 'cito_slug')
