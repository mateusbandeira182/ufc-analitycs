"""events: ufc_event_id

Identificador do evento na API JSON oficial da UFC (SPEC 008, M7, Slice 02 -- RF-07).
Migration **aditiva** e reversível, aplicada antes de qualquer descoberta de catálogo
(o dado vem do comando ``ingestion.ufc_official.discovery``):

- ``events.ufc_event_id`` (``String(32)``, nullable, **indexado**) -- o ``EventId`` do
  evento na fonte oficial, resolvido casando data local + nome normalizado contra o
  catálogo varrido; o índice serve o acesso da Slice 04, que parte do id externo para o
  evento persistido.

Nullable por decisão: evento sem correspondência na fonte oficial permanece nulo --
ausência explícita, nunca sentinela. Aqui a ausência é preferível ao palpite, porque a
Slice 04 corrige canto a partir deste id e um id errado corrigiria o canto com o card do
evento errado. Autogerada via ``alembic revision --autogenerate`` e **revisada à mão**:
nenhuma coluna pré-existente muda de tipo ou de nulidade, nenhum enum é tocado, os
identificadores da Cito (M6) permanecem intactos, e o ``downgrade`` é simétrico (dropa só
o índice e a coluna aditiva).

Revision ID: b1c4f2a90d37
Revises: 3f1c38836a40
Create Date: 2026-09-01 16:49:43.894975

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c4f2a90d37'
down_revision: Union[str, Sequence[str], None] = '3f1c38836a40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('events', sa.Column('ufc_event_id', sa.String(length=32), nullable=True))
    op.create_index(op.f('ix_events_ufc_event_id'), 'events', ['ufc_event_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_events_ufc_event_id'), table_name='events')
    op.drop_column('events', 'ufc_event_id')
