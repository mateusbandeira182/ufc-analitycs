"""bout_features derived cache

Cria a tabela derivada ``bout_features`` (Slice 05 da SPEC 005 -- M4): cache
reconstrutível da matriz de confronto, uma linha por luta, chaveada por ``bout_id``
(PK/FK para ``bouts.id``). O granular permanece a fonte de verdade -- esta tabela pode
ser dropada e reconstruída a qualquer momento.

Autogerada via ``alembic revision --autogenerate`` e **revisada à mão**: o enum
``corner`` já existe (criado pela migration inicial ``c62506a8b256``, dono:
``bout_fighters``). O autogenerate emite ``sa.Enum('red', 'blue', name='corner')`` sem
``create_type``, o que faria o ``create_table`` tentar recriar o tipo e quebrar com
"type corner already exists". A revisão adiciona ``create_type=False`` para **referenciar**
o tipo existente sem recriá-lo. O downgrade dropa **somente** a tabela ``bout_features`` --
nunca o enum ``corner``, que pertence a ``bout_fighters`` (a migration inicial é a dona do
tipo e a única que o dropa).

Revision ID: f3d1bfc5aa70
Revises: c62506a8b256
Create Date: 2026-07-12 19:33:16.947242

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f3d1bfc5aa70'
down_revision: Union[str, Sequence[str], None] = 'c62506a8b256'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'bout_features',
        sa.Column('bout_id', sa.Integer(), nullable=False),
        sa.Column('features', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # Revisão à mão: postgresql.ENUM com create_type=False reusa o enum ``corner`` já
        # existente (dono: bout_fighters). O ``sa.Enum`` genérico não propaga create_type
        # para o dialeto e o create_table tentaria recriar o tipo ("type corner already
        # exists") ao reaplicar sobre um enum preservado por um downgrade de um passo.
        sa.Column(
            'target_winner_corner',
            postgresql.ENUM('red', 'blue', name='corner', create_type=False),
            nullable=True,
        ),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['bout_id'], ['bouts.id'], ),
        sa.PrimaryKeyConstraint('bout_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Dropa SÓ a tabela derivada. O enum ``corner`` pertence a ``bout_fighters`` e é
    # dropado apenas pela migration inicial -- nunca aqui, senão o granular quebraria.
    op.drop_table('bout_features')
