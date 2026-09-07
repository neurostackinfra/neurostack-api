export type AuthTokenGetter = () => string | null | Promise<string | null>;

let baseUrl = "";
let authTokenGetter: AuthTokenGetter = () => null;

export function setBaseUrl(url: string) {
  baseUrl = url;
}

export function setAuthTokenGetter(getter: AuthTokenGetter) {
  authTokenGetter = getter;
}

export async function customFetch<T>(
  url: string,
  options: RequestInit = {}
): Promise<T> {
  const token = await authTokenGetter();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const response = await fetch(`${baseUrl}${url}`, { ...options, headers });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
