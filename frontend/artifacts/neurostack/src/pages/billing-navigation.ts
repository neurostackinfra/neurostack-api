/**
 * Full-page navigation to the provider-hosted checkout returned by the API.
 * Kept in its own module so tests can observe the redirect without touching
 * the non-configurable `window.location` of the test DOM.
 */
export function navigateToHostedCheckout(url: string): void {
  window.location.assign(url);
}
