# AGENTS.md — MMA Analytics Platform

Contexto de engenharia para agentes de código (Copilot, Claude, etc.). Descreve a
**arquitetura real, as convenções e o fluxo de trabalho** do repositório para que
qualquer mudança nasça no lugar certo, no padrão certo.

> As **decisões de produto e de dados** (escopo do MVP, fontes, granularidade,
> wide-vs-long) vivem em [`CLAUDE.md`](./CLAUDE.md) e nos ADRs em
> [`docs/system-design/adr/`](./docs/system-design/adr/). **Não re-litigar** aqui —
> este arquivo é sobre *como o código é escrito*, não *o que* se decidiu construir.

---

## Visão de 30 segundos

Plataforma de coleta, armazenamento e análise de dados de MMA (UFC). Backend
**Python** (FastAPI + SQLAlchemy/Alembic + Pandas), banco **PostgreSQL**, SPA
**React 19 + TypeScript** (Vite). A API v1 é **somente-leitura**; a escrita acontece
só pelos scripts de ingestão. Há uma camada de **análise/preditivo** (scikit-learn)
como fase 2 reaberta.

Tudo em **português (pt-BR)**: nomes de domínio, docstrings, comentários, mensagens
de commit. Mantenha esse idioma ao editar.

## Layout do repositório

```
apps/                # Apps de domínio (FastAPI + SQLAlchemy). Um pacote por recurso.
  bouts/  events/  fighters/  predictions/  features/
mma_analytics/       # Núcleo da aplicação: app factory, agregador /api/v1, db, settings, spa.
ingestion/           # Scripts de ingestão (seed Kaggle, incremental Cito, features, backfills).
  cito/  features/  sources/
analysis/            # Camada preditiva: dataset, train, model, predict, metrics (fase 2).
alembic/             # Migrations (versions/) + env.py. Schema NUNCA se altera à mão.
tests/               # Suíte de infraestrutura/ingestão/análise (testes de app vivem em apps/**/tests).
docs/system-design/  # ADRs (decisões arquiteturais).
project/             # Artefatos do fluxo de planejamento (specs/sprints/plans/implementations/audits/reviews).
web/                 # SPA React (repo lógico separado; ver "Frontend").
CLAUDE.md            # Contexto de produto e invariantes travadas.
Makefile             # Gate de qualidade do backend: `make ci`.
```

## Backend — arquitetura por camadas

Cada app de domínio segue o **mesmo padrão de camadas**. Ao criar/editar um recurso,
espelhe esta separação (exemplo canônico: `apps/bouts/`):

| Arquivo         | Responsabilidade | Regra |
|-----------------|------------------|-------|
| `models.py`     | Models SQLAlchemy (`Mapped[...]`, `mapped_column`). | Fonte da verdade do schema via `Base.metadata`. |
| `enums.py`      | Enums de domínio (ex.: `Corner`, `Method`). | Enum tem **um dono**; nunca recriar em migration. |
| `schemas.py`    | Schemas Pydantic v2 de resposta (`*Out`). | Fronteira de saída da API; nada de `Any` no domínio. |
| `selectors.py`  | **Leitura** pura (sem efeito colateral). Query + composição em dataclasses `frozen`. | Toda query vive aqui, nunca no router. |
| `api.py`        | Routers FastAPI **finos**. | Valida entrada, delega ao selector, monta o `*Out`. Sem query no router. |
| `tests/`        | pytest do app (selector + API). | Contra Postgres real. |

Princípios que o código já segue e que você deve preservar:

- **Router fino, selector gordo.** O router só valida (`422`), checa existência
  (`404`) e traduz o resultado do selector no schema. A query mora no selector.
- **Somente-leitura na API.** `get_session` (em `mma_analytics/db.py`) nunca abre
  transação de escrita nem faz commit. Escrita = ingestão.
- **Sem N+1.** Relationships de leitura são carregados com `selectinload`/`aliased`
  explicitamente no selector.
- **Granularidade inegociável.** Stats são servidas **como foram gravadas** (por
  luta, por canto, por round), nunca como médias pré-agregadas. Médias/derivadas
  (acurácia, shares) calculam-se *on demand*. Ver ADR 0001.
- **Router novo → registrar no agregador** `mma_analytics/api_v1.py` (ponto único de
  montagem sob `/api/v1`). Todas as rotas são versionadas por prefixo.
- **`source` em toda escrita.** Toda linha rastreia a origem do dado (`"kaggle"`,
  `"cito"`).

### Migrations (Alembic)

- **Sempre** via Alembic; **nunca** alterar schema à mão. `Base.metadata` é a fonte,
  consumida por `alembic/env.py`.
- Migrations **aditivas** (colunas nullable / defaults), com `downgrade` explícito.
  Roundtrip `upgrade head` → `downgrade` deve ser limpo.
- Reusar enums existentes (dono único); nunca recriar um `ENUM` já existente.

### Ingestão (`ingestion/`)

- Scripts **idempotentes** por chave natural (upsert / `UPDATE` nas linhas
  existentes / `ON CONFLICT`). Rodar de novo não duplica nem muda contagem.
- **Entity resolution**: deduplicar por **nome normalizado (+ DOB como desempate)**
  *antes* de inserir. O mesmo lutador aparece como canto R numa luta e B noutra.
- **Fronteira dinâmica tipada**: JSON da Cito (Pydantic v2, `extra="ignore"`) e
  DataFrame do Pandas são tipados **na borda**. Nenhum `Any` propaga para o domínio.
- **Quota da Cito** (free tier 500 req/mês): backfills de rede ficam atrás de **gate
  humano**, com `CallBudget` e **cache em disco resumável**. Modo *fixture* (JSON
  local, 0 rede) é o caminho de teste.

## Frontend (`web/`)

React 19 + TypeScript strict + Vite 6 + Tailwind v4. Dados via **TanStack Query**;
rotas via **React Router v7**. Componentes UI em `components/ui` (shadcn-style, CVA).

Organização por **feature** (`web/src/features/<feature>/`): componentes `.tsx`,
hooks `use*.ts`, helpers `format.ts`/`statsFormat.ts` e seus `*.test.ts(x)` colados.

Padrões que o código já segue:

- **`fetch` só no cliente central** `web/src/api/client.ts` (`apiGet`, `ApiError` com
  `status`). Componentes/hooks nunca usam `fetch` solto — sempre via funções de dados.
- **Server-state em hooks `useX`** com TanStack Query. As **query keys são
  centralizadas** em `web/src/queryKeys.ts` (fonte única de cache/invalidação).
  Desabilitar a query (`enabled`) para id inválido (`NaN`).
- **Tipos gerados do OpenAPI.** `web/src/api/types.ts` vem de `npm run gen:api`
  (`openapi-typescript` sobre `web/openapi.json`). Ao mudar um schema no backend,
  **regenere os tipos** — não edite `types.ts` à mão.
- **Nunca renderizar `NaN`/`Infinity`.** Denominador zero / valor não-finito →
  estado "sem dado" explícito. Estados de loading/erro (404/422/503) são amigáveis.

## Comandos

**Backend** (via `uv` e `make` — gate único é `make ci`):

```bash
make ci          # lint + typecheck + test  (contrato dev == CI)
make lint        # ruff check + ruff format --check
make typecheck   # mypy --strict (mma_analytics apps ingestion analysis tests ...)
make test        # cria o banco de teste e roda pytest (APP_ENV=test)
make format      # ruff format + ruff check --fix
make security    # bandit + pip-audit
make run         # builda a SPA e sobe uvicorn servindo /api/v1 + SPA
```

**Frontend** (`cd web`):

```bash
npm run ci        # lint + format(check) + typecheck + test(coverage) + build
npm run dev       # Vite (proxy /api -> backend)
npm run gen:api   # regenera src/api/types.ts a partir de openapi.json
npm run test      # vitest run --coverage
```

## Qualidade e testes (o que precisa passar)

- **Python**: `ruff` (lint + format, line-length 100), `mypy --strict` com plugin
  `pydantic.mypy`. Regras em `pyproject.toml`. `make ci` **verde** ao fim de cada
  mudança.
- **pytest** roda contra **Postgres real**, não SQLite/mocks. Isolamento por teste:
  a fixture `db_session` (em `conftest.py` na raiz) abre transação + `create_all` e
  faz **rollback** no teardown. A fixture `client` (TestClient) sobrepõe
  `get_session` pela sessão transacional. Testes exigem `APP_ENV=test` (banco
  `ufc_bum_test`) — há guarda que aborta se apontar para outro banco.
- **Factories** com `polyfactory`; asserts do pytest permitidos em `tests/**` e
  `apps/**/tests/**` (ignore de `S101`).
- **TypeScript**: `tsc --noEmit` strict, `eslint` (+ jsx-a11y, react-hooks),
  `prettier`. Testes com **Vitest + Testing Library + MSW** (mock da API FastAPI).

## Convenções ao contribuir

- **Idioma pt-BR** em identificadores de domínio, docstrings, comentários e commits.
- **Type hints sempre** (Python) e **strict** (TS). Funções pequenas e coesas.
- **Comentário só para o que o código não mostra** (uma invariante, um porquê não
  óbvio). Não narrar o óbvio nem deixar recado de PR no código. Espelhe a densidade
  de comentário do arquivo vizinho.
- **Commits**: `tipo(escopo): descrição` em pt-BR (ex.:
  `feat(api): expõe splits na leitura`, `feat(web): tela de matchup`).
- **Não introduzir**: web scraper (regra "SEM WEB SCRAPER" do CLAUDE.md); dependência
  nova sem necessidade clara; média pré-agregada de forma destrutiva; `Any` cruzando
  a fronteira para o domínio; `fetch` fora do cliente central.

## Onde uma mudança típica encosta

- **Novo campo servido pela API**: model (`apps/*/models.py`) → migration Alembic →
  selector → schema `*Out` → router → **regenerar** `web/openapi.json` + `types.ts` →
  consumir no hook/componente. Testes em cada camada tocada.
- **Novo endpoint**: selector + router no app; registrar no `mma_analytics/api_v1.py`;
  hook `useX` + query key no frontend.
- **Novo dado ingerido**: script idempotente em `ingestion/` com `source`, entity
  resolution quando aplicável, e teste de idempotência contra Postgres real.
