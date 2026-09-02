"""fighters: URLs de imagem do lutador

URLs de imagem do lutador vindas da **Cito** (SPEC 008, M7, Slice 06 -- RF-06). Migration
**aditiva** e reversível, aplicada antes do backfill (o dado vem do comando
``ingestion.cito.images``):

- ``fighters.headshot_url`` (``String(512)``, nullable, **sem índice**) -- o retrato, estilo
  ``event_results_athlete_headshot`` do ufc.com.
- ``fighters.body_image_url`` (``String(512)``, nullable, **sem índice**) -- o corpo inteiro,
  estilo ``athlete_bio_full_body``.

Nenhuma das duas é a arte do card (``event_fight_card_upper_body_of_standing_athlete``), que é
por luta e é de onde o M6 recupera o canto REAL -- confundi-las corromperia o alvo do modelo em
silêncio (o ``athlete_bio_full_body`` também carrega sufixo ``_L_``, e nos **dois** cantos da
mesma luta).

``512`` porque a URL real leva query string de cache (``?itok=...``) além do caminho. Sem
índice nas duas: não há consulta por URL, e índice sem consulta é custo de escrita sem
contrapartida (mesmo critério que deixou ``ufc_mma_id`` sem índice na Slice 03).

Guardamos só a URL: baixar e hospedar arquivo está explicitamente fora do escopo (decisão do
humano). Nullable por decisão, e a ausência é o **estado normal**: a Cito só publica imagem
para parte do elenco, e lutador fora da janela de 2010-03-21 (RF-03) nunca é tocado.

Autogerada via ``alembic revision --autogenerate`` e **revisada à mão**: nenhuma coluna
pré-existente muda de tipo ou de nulidade, nenhum enum é tocado, os identificadores da fonte
oficial (Slices 02 e 03) e os da Cito (M6) permanecem intactos, e o ``downgrade`` é simétrico
(dropa só as duas colunas aditivas).

Revision ID: f75aacf624eb
Revises: 322648148188
Create Date: 2026-09-01 19:47:51.977068

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f75aacf624eb'
down_revision: Union[str, Sequence[str], None] = '322648148188'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('fighters', sa.Column('headshot_url', sa.String(length=512), nullable=True))
    op.add_column('fighters', sa.Column('body_image_url', sa.String(length=512), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('fighters', 'body_image_url')
    op.drop_column('fighters', 'headshot_url')
