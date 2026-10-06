"""fighter_asof_features: cache as-of por lutador

Cache as-of no grão **lutador** (SPEC 011, M9, Slice 01 -- RF-01). Irmã de ``bout_features``
(grão luta): guarda o vetor de features "agora" de cada lutador, para que o serving monte a
matriz de confronto com um ``SELECT`` de duas linhas em vez de reconstruir a pipeline inteira
a cada request (os ~67s que a RF-04 existe para eliminar).

Migration **aditiva**: cria uma tabela nova e não toca nenhuma coluna, índice ou enum
pré-existente. Nenhum tipo enum é criado aqui, logo o ``downgrade`` não dropa nenhum -- ele
dropa apenas a tabela.

- ``fighter_id`` -- PK e FK para ``fighters.id``. Chave natural não-nula, o que torna o upsert
  ``INSERT ... ON CONFLICT (fighter_id) DO UPDATE`` limpo e determinístico (RF-02), mesmo
  argumento de ``bout_features.bout_id``.
- ``features`` (``JSONB`` NOT NULL) -- o vetor as-of, alinhável a ``feature_names``.
- ``style_history`` (``JSONB`` NOT NULL) -- coluna **separada** de propósito (D-15): é o insumo
  de ``similar_style_win_rate_prior``, que é feature do PAR (depende de quem é o adversário) e
  por isso jamais pode ser alinhada a ``feature_names``. Uma coluna só convidaria ao erro.
- ``n_bouts_source`` (NOT NULL) -- lutas que a pipeline as-of consumiu nesta materialização.
  É o denominador da guarda **barata** de defasagem (RF-05): a API a compara com o ``count(*)``
  real e devolve o diagnóstico em toda resposta que lê o cache.
- ``source`` (``String(32)`` NOT NULL) -- rastreio da origem da linha, como em toda tabela.
- ``materialized_at`` (``timestamptz`` NOT NULL) -- instante da materialização, exposto na API.

**Sem índice** além da PK: as únicas consultas são por ``fighter_id`` (coberto pela PK) e as
agregações da guarda de defasagem (``max(materialized_at)``/``max(n_bouts_source)``), que
varrem uma tabela da ordem de 2.700 linhas. Índice sem consulta é custo de escrita sem
contrapartida (mesmo critério da Slice 06 do M7).

Autogerada via ``alembic revision --autogenerate`` e **revisada à mão**: ``down_revision``
confirmado em ``f75aacf624eb`` (head único da cadeia linear antes desta), nulidade conferida
nas duas colunas JSONB e ``downgrade`` simétrico.

Revision ID: a1c9e5d73b40
Revises: f75aacf624eb
Create Date: 2026-09-21 15:19:55.910094

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1c9e5d73b40'
down_revision: Union[str, Sequence[str], None] = 'f75aacf624eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'fighter_asof_features',
        sa.Column('fighter_id', sa.Integer(), nullable=False),
        sa.Column('features', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('style_history', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('n_bouts_source', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('materialized_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['fighter_id'], ['fighters.id'], ),
        sa.PrimaryKeyConstraint('fighter_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('fighter_asof_features')
