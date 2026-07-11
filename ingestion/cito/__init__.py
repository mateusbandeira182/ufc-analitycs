"""Cliente da Cito API (``mmaapi.dev``) para a ingestão incremental (M1).

Expõe o ``CitoClient`` (fetch tipado de um evento, com modo fixture para testar sem
consumir a quota do free tier) e os DTOs Pydantic que tipam o payload na borda.
"""
