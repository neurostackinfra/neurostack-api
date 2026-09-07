let baseUrl = "";
let authTokenGetter = () => null;
export function setBaseUrl(url) {
    baseUrl = url;
}
export function setAuthTokenGetter(getter) {
    authTokenGetter = getter;
}
export async function customFetch(url, options = {}) {
    const token = await authTokenGetter();
    const headers = {
        "Content-Type": "application/json",
        ...options.headers,
    };
    if (token)
        headers["Authorization"] = `Bearer ${token}`;
    const response = await fetch(`${baseUrl}${url}`, { ...options, headers });
    if (!response.ok)
        throw new Error(`HTTP ${response.status}`);
    return response.json();
}
