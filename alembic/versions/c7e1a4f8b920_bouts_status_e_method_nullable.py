"""bouts: status e method nullable

Recorte "luta realizada" x "luta agendada" em ``bouts`` (SPEC 011, M9, Slice 02 -- RF-07).
A tabela passa a comportar luta **sem** resultado, para que a ingestão de cards por vir
(Slice 04) tenha onde escrever sem contaminar a pipeline de features nem o histórico do
lutador.

Duas mudanças, que são a mesma decisão vista de dois lados:

- ``status`` (enum ``bout_status``, NOT NULL) -- ``completed`` | ``scheduled``. Esta migration
  é a **dona** do tipo ``bout_status``: ela o cria e o ``downgrade`` o dropa. Deixar um enum
  órfão em ``pg_type`` faria o próximo ``upgrade`` estourar com "type already exists".
- ``method`` passa a **nullable** -- luta agendada não tem desfecho, e fabricar um método
  placeholder gravaria dado falso exatamente no rótulo que o modelo treina.

**A ordem do ``upgrade`` é load-bearing**: criar o tipo, adicionar a coluna *nullable*,
backfillar e só então apertar para ``NOT NULL``. O autogenerate produz a coluna já ``NOT
NULL`` num passo só, o que estoura em qualquer banco povoado -- e toda luta já persistida é,
por definição, realizada, então o ``UPDATE`` é a tradução literal dessa semântica.

**O ``downgrade`` exige ausência de luta agendada no banco**: restaurar ``method NOT NULL``
estoura em luta sem desfecho. A partir da Slice 04, que passa a escrever agendadas, reverter
esta migration exige apagá-las antes.

Autogerada via ``alembic revision --autogenerate`` e **revisada à mão**: a ordem do backfill,
o ``create``/``drop`` explícito do tipo e o ``server_default`` ausente são decisões manuais.
Sem ``server_default`` de propósito: o default é do lado Python (``BoutStatus.COMPLETED`` no
``mapped_column``), e toda escrita de ``bouts`` passa pelo ORM -- um default no servidor seria
um segundo lugar dizendo a mesma coisa.

Revision ID: c7e1a4f8b920
Revises: a1c9e5d73b40
Create Date: 2026-09-21 16:08:05.480360

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e1a4f8b920"
down_revision: str | Sequence[str] | None = "a1c9e5d73b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# O tipo é declarado uma vez e reusado nos dois sentidos -- é esta migration que o cria e o
# dropa. ``create_type=False`` na coluna evita que o ``add_column`` tente criá-lo de novo.
bout_status = sa.Enum("completed", "scheduled", name="bout_status")

# ``method`` já existe; o ENUM abaixo só descreve o tipo corrente para o ``alter_column``.
bout_method = postgresql.ENUM(
    "ko_tko", "submission", "decision", "dq", "no_contest", name="bout_method"
)


def upgrade() -> None:
    """Cria ``bout_status``, adiciona ``bouts.status`` com backfill e afrouxa ``method``."""
    bout_status.create(op.get_bind(), checkfirst=False)
    op.add_column(
        "bouts",
        sa.Column(
            "status",
            postgresql.ENUM(name="bout_status", create_type=False),
            nullable=True,
        ),
    )
    # Backfill ANTES do NOT NULL: toda luta já persistida é, por definição, realizada.
    op.execute("UPDATE bouts SET status = 'completed'")
    op.alter_column("bouts", "status", nullable=False)
    op.alter_column("bouts", "method", existing_type=bout_method, nullable=True)


def downgrade() -> None:
    """Restaura ``method NOT NULL``, dropa ``bouts.status`` e o tipo ``bout_status``.

    Só reverte com o banco **sem** luta agendada: ``method NOT NULL`` estoura em luta sem
    desfecho (e apagar essas lutas aqui seria destruir dado em silêncio).
    """
    op.alter_column("bouts", "method", existing_type=bout_method, nullable=False)
    op.drop_column("bouts", "status")
    bout_status.drop(op.get_bind(), checkfirst=False)
