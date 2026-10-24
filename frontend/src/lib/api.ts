import "server-only";

// The backend is only ever called from the Next.js server. The browser never sees its URL,
// the access token, or any Authorization header.
const API_URL = process.env.API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly fieldErrors: Record<string, string> = {},
    readonly code?: string,
  ) {
    super(message);
  }
}

type Options = {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  token?: string;
  body?: unknown;
  forwardedFor?: string;
};

export async function apiFetch<T>(
  path: string,
  { method = "GET", token, body, forwardedFor }: Options = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  if (forwardedFor) headers["X-Forwarded-For"] = forwardedFor;

  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
  } catch {
    throw new ApiError(0, "Cannot reach the server. Please try again in a moment.");
  }
  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// Some endpoints answer { code, message } so the UI can branch on a stable code.
function isCodedDetail(detail: unknown): detail is { code: string; message: string } {
  return (
    typeof detail === "object" &&
    detail !== null &&
    !Array.isArray(detail) &&
    typeof (detail as { code?: unknown }).code === "string" &&
    typeof (detail as { message?: unknown }).message === "string"
  );
}

type ValidationItem = { loc?: (string | number)[]; msg?: string };

async function toApiError(res: Response): Promise<ApiError> {
  let detail: unknown;
  try {
    detail = ((await res.json()) as { detail?: unknown }).detail;
  } catch {
    // non-JSON error body
  }
  if (typeof detail === "string") return new ApiError(res.status, detail);
  if (isCodedDetail(detail)) return new ApiError(res.status, detail.message, {}, detail.code);
  if (Array.isArray(detail)) {
    const fieldErrors: Record<string, string> = {};
    for (const item of detail as ValidationItem[]) {
      const field = String(item.loc?.at(-1) ?? "");
      const msg = (item.msg ?? "Invalid value").replace(/^Value error, /, "");
      if (field && !(field in fieldErrors)) fieldErrors[field] = msg;
    }
    return new ApiError(res.status, "Please check the highlighted fields.", fieldErrors);
  }
  return new ApiError(res.status, "Something went wrong. Please try again.");
}
