"""bout_predictions

Registro de predições do modelo (SPEC 007, M6, Slice 07 -- RF-10). Migration
**aditiva** e reversível: cria uma única tabela nova e não altera nenhuma coluna,
tabela ou tipo enum pré-existente.

``bout_predictions`` guarda uma linha por (luta, versão do modelo):

- ``bout_id`` (FK ``bouts.id``, NOT NULL, **indexado**) -- o índice serve o acesso por
  luta, que é como a leitura do previsto x ocorrido entra na tabela.
- ``predicted_winner_id`` (FK ``fighters.id``, nullable) -- nullable por simetria com
  ``bouts.winner_id``: a comparação previsto x ocorrido é de três estados nos dois lados.
- ``prob_predicted_winner``, ``n_features`` -- a afirmação do modelo e o tamanho do vetor
  de features que a produziu.
- ``model_version`` (``String(64)``, NOT NULL) -- o ``trained_at`` do artefato em ISO-8601,
  gravado exatamente como persistido.
- ``predicted_at`` (``DateTime(timezone=True)``, NOT NULL) -- instante tz-aware, nunca naive.
- ``source`` (``String(32)``, NOT NULL) -- rastreio de origem (``"api"`` | ``"walk_forward"``).

A unique ``uq_bout_prediction (bout_id, model_version)`` é o coração da slice: torna a
gravação idempotente na chave natural (mesma luta + mesmo modelo não duplica) e permite
que versões de modelo distintas coexistam sobre a mesma luta -- é isso que faz a série
temporal do walk-forward existir como dado. Ambas as colunas são NOT NULL, então o upsert
``ON CONFLICT ... DO UPDATE`` é determinístico (o Postgres trataria cada ``NULL`` como
distinto num índice único, problema que o seed de ``fighters`` enfrenta com ``date_of_birth``).

**Nenhuma coluna de acerto**: o acerto deriva-se de ``bouts.winner_id`` contra
``predicted_winner_id`` sob demanda -- persistir "acertou" seria pré-agregação destrutiva
e ficaria errado se o resultado fosse corrigido depois.

Autogerada via ``alembic revision --autogenerate`` e **revisada à mão**: o autogenerate
não emitiu nenhuma operação sobre os enums (``corner``, ``stance``, ``bout_method``) nem
sobre tabelas existentes, e o ``downgrade`` é simétrico (dropa só o índice e a tabela nova).

Revision ID: c584e0a18606
Revises: fd5a802eac7b
Create Date: 2026-08-31 23:46:58.363317

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c584e0a18606'
down_revision: Union[str, Sequence[str], None] = 'fd5a802eac7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Cria ``bout_predictions`` com a chave natural ``(bout_id, model_version)``."""
    op.create_table(
        'bout_predictions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bout_id', sa.Integer(), nullable=False),
        sa.Column('predicted_winner_id', sa.Integer(), nullable=True),
        sa.Column('prob_predicted_winner', sa.Float(), nullable=False),
        sa.Column('model_version', sa.String(length=64), nullable=False),
        sa.Column('n_features', sa.Integer(), nullable=False),
        sa.Column('predicted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['bout_id'], ['bouts.id'], ),
        sa.ForeignKeyConstraint(['predicted_winner_id'], ['fighters.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('bout_id', 'model_version', name='uq_bout_prediction'),
    )
    op.create_index(
        op.f('ix_bout_predictions_bout_id'), 'bout_predictions', ['bout_id'], unique=False
    )


def downgrade() -> None:
    """Dropa só o índice e a tabela ``bout_predictions`` (migration aditiva)."""
    op.drop_index(op.f('ix_bout_predictions_bout_id'), table_name='bout_predictions')
    op.drop_table('bout_predictions')
