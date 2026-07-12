/*
  Cliente HTTP central da SPA. É o ÚNICO lugar do app com `fetch`.
  Componentes e hooks consomem a API sempre através das funções de dados
  (ex.: getFighters), nunca com fetch solto.
*/

// Base da API. Em desenvolvimento o proxy do Vite encaminha /api para o backend;
// o padrão relativo funciona sem configuração adicional.
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

/** Erro tipado da borda HTTP. Expõe o `status` para o tratamento por rota (ex.: 404). */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "ApiError";
    this.status = status;
  }
}

/** GET tipado. Lança `ApiError` em resposta não-ok; senão devolve o JSON como `T`. */
export async function apiGet<T>(
  path: string,
  params?: Record<string, string | number | undefined>,
): Promise<T> {
  const url = buildUrl(path, params);

  let response: Response;
  try {
    response = await fetch(url, {
      headers: { Accept: "application/json" },
    });
  } catch (cause) {
    throw new ApiError(0, "Não foi possível conectar à API.", { cause });
  }

  if (!response.ok) {
    throw new ApiError(
      response.status,
      `A API respondeu com o status ${String(response.status)}.`,
    );
  }

  return (await response.json()) as T;
}

function buildUrl(
  path: string,
  params?: Record<string, string | number | undefined>,
): string {
  const query = new URLSearchParams();
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") {
        query.set(key, String(value));
      }
    }
  }
  const suffix = query.toString();
  const base = `${API_BASE_URL}${path}`;
  return suffix ? `${base}?${suffix}` : base;
}
