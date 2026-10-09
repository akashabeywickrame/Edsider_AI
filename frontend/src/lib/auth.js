// Azure App Service's own Easy Auth endpoints - /.auth/login/<provider>
// and /.auth/logout are real, published by Azure automatically once an
// identity provider is configured on the App Service (see
// current_reviewer() in app.py, the same identity system this whole
// project already relies on). Neither is an endpoint this app defines,
// and neither exists on a local dev server - there is no Easy Auth
// proxy on localhost, so both 404 there. isDevAuth is true only for
// `npm run dev`; a real production build never takes the bypass.
export const isDevAuth = import.meta.env.DEV

// Easy Auth is not wired up on the current deployment yet - there is
// nothing live at /.auth/login/* or /.auth/logout there either, so
// sending people through either one 404s (or, worse, silently falls
// through to the app's own router and renders "Not found") instead of
// actually signing in or out. Until that is set up for real, sign-in
// and sign-out both bypass it, the same way local dev already does.
// TEMPORARY: set back to false once Easy Auth is live, so both flows
// go through real Easy Auth in production again.
export const BYPASS_SIGN_IN = true

export function loginUrl(provider, dest) {
  return `/.auth/login/${provider}?post_login_redirect_uri=${encodeURIComponent(dest)}`
}

export function logoutUrl(dest = '/') {
  return `/.auth/logout?post_logout_redirect_uri=${encodeURIComponent(dest)}`
}

// Local development only: the API reads who is calling from Easy Auth's
// header, which a dev server never receives. A name typed on the teacher
// page is sent as that header instead, so the teacher flow can be tried
// locally. Ignored entirely in a production build (isDevAuth is false there).
const DEV_IDENTITY_KEY = 'edsider_dev_identity'

export function devIdentity() {
  if (!isDevAuth) return null
  try {
    return window.localStorage.getItem(DEV_IDENTITY_KEY) || null
  } catch {
    return null
  }
}

export function setDevIdentity(name) {
  try {
    if (name) window.localStorage.setItem(DEV_IDENTITY_KEY, name)
    else window.localStorage.removeItem(DEV_IDENTITY_KEY)
  } catch {
    // Storage blocked - the page still works, just as the default identity.
  }
}
