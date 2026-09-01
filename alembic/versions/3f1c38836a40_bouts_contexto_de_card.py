"""bouts: contexto de card

Contexto de card da luta (SPEC 007, M6, Slice 05 -- CA-06). Migration **aditiva** e
reversível, no padrão da ADR 0004; o dado vem do mesmo payload que o backfill
round-a-round já paga (``ingestion.cito.backfill_rounds``), então capturá-lo aqui evita
re-gastar quota depois por dois campos que já estavam na resposta:

- ``bouts.card_section`` (``String(32)``, nullable) -- o rótulo **cru** da seção do card
  na Cito ('Main Card' / 'Prelims'), sem normalizar nem mapear para enum. Proxy do nível
  de oposição (topo do card vs. preliminares).
- ``bouts.bout_order`` (``Integer``, nullable) -- a posição da luta no card. É
  **ordenável, não sequencial nem denso**: o payload real traz 1001 na luta principal do
  card sondado. Nada pode assumir intervalo, densidade, ou que 1 seja a principal.

``cardPosition`` ('Main Card 1') NÃO ganha coluna: é derivável dos dois campos acima.

Ambas nullable por decisão: luta sem contexto no payload permanece nula -- ausência
explícita, nunca sentinela. Autogerada via ``alembic revision --autogenerate`` e
**revisada à mão**: nenhuma coluna pré-existente muda de tipo ou de nulidade, nenhum enum
é tocado, e o ``downgrade`` é simétrico (dropa só as duas colunas aditivas).

Revision ID: 3f1c38836a40
Revises: c584e0a18606
Create Date: 2026-09-01 01:38:20.145123

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f1c38836a40'
down_revision: Union[str, Sequence[str], None] = 'c584e0a18606'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('bouts', sa.Column('card_section', sa.String(length=32), nullable=True))
    op.add_column('bouts', sa.Column('bout_order', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('bouts', 'bout_order')
    op.drop_column('bouts', 'card_section')
