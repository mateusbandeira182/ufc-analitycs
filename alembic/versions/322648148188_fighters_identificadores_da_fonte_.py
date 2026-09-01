"""fighters: identificadores da fonte oficial

Identificadores do lutador na API JSON oficial da UFC (SPEC 008, M7, Slice 03 -- RF-07).
Migration **aditiva** e reversível, aplicada antes do backfill (o dado vem do comando
``ingestion.ufc_official.fighter_ids``):

- ``fighters.ufc_fighter_id`` (``String(32)``, nullable, **indexado**) -- o ``FighterId`` do
  lutador na fonte oficial, observado nos cantos das lutas dos eventos já mapeados pela
  Slice 02; o índice serve a entity resolution, que passa a resolver por ele **antes** do
  nome normalizado.
- ``fighters.ufc_mma_id`` (``String(32)``, nullable, **sem índice**) -- o ``MMAId`` do mesmo
  payload. Acompanha o dado e não resolve nada: índice sem consulta é custo de escrita sem
  contrapartida.

Ambos guardam o inteiro da fonte como **texto opaco**: identificador externo nunca entra em
aritmética (mesmo padrão de ``events.ufc_event_id`` e ``cito_event_id``).

Nullable por decisão, e a ausência é o **estado normal**, nunca falha: lutador ativo apenas
antes de 2010-03-21 está fora da janela da fonte (RF-03) e nunca receberá id, e a Cito não
publica este identificador. É por isso que o id **complementa** a chave
``(name_normalized, date_of_birth)`` com precedência, em vez de substituí-la.

Autogerada via ``alembic revision --autogenerate`` e **revisada à mão**: nenhuma coluna
pré-existente muda de tipo ou de nulidade, nenhum enum é tocado, o identificador do evento
(Slice 02) e os da Cito (M6) permanecem intactos, e o ``downgrade`` é simétrico (dropa só o
índice e as duas colunas aditivas).

Revision ID: 322648148188
Revises: b1c4f2a90d37
Create Date: 2026-09-01 18:36:46.355551

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '322648148188'
down_revision: Union[str, Sequence[str], None] = 'b1c4f2a90d37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('fighters', sa.Column('ufc_fighter_id', sa.String(length=32), nullable=True))
    op.add_column('fighters', sa.Column('ufc_mma_id', sa.String(length=32), nullable=True))
    op.create_index(op.f('ix_fighters_ufc_fighter_id'), 'fighters', ['ufc_fighter_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_fighters_ufc_fighter_id'), table_name='fighters')
    op.drop_column('fighters', 'ufc_mma_id')
    op.drop_column('fighters', 'ufc_fighter_id')
