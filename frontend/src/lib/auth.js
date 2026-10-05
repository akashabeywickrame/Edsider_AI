// Azure App Service's own Easy Auth endpoints - /.auth/login/<provider>
// and /.auth/logout are real, published by Azure automatically once an
// identity provider is configured on the App Service (see
// current_reviewer() in app.py, the same identity system this whole
// project already relies on). Neither is an endpoint this app defines,
// and neither exists on a local dev server - there is no Easy Auth
// proxy on localhost, so both 404 there. isDevAuth is true only for
// `npm run dev`; a real production build never takes the bypass.
export const isDevAuth = import.meta.env.DEV

export function loginUrl(provider, dest) {
  return `/.auth/login/${provider}?post_login_redirect_uri=${encodeURIComponent(dest)}`
}

export function logoutUrl(dest = '/') {
  return `/.auth/logout?post_logout_redirect_uri=${encodeURIComponent(dest)}`
}
