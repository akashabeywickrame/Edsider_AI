import { Navigate, useLocation } from 'react-router-dom'
import { useMe } from '../lib/useMe.js'
import { BYPASS_SIGN_IN, devIdentity, isDevAuth, logoutUrl, setDevIdentity } from '../lib/auth.js'

// Wraps every top-level Layout (data-entry, teacher, admin). A 403 from
// /me means someone signed in with Google or Microsoft whose email is not
// allowed - not on the staff list (see service.py's _resolve_identity):
// EdSider's own "You are not a user! Please contact System Administrator!"
// rejection, shown here instead of the app. Any other failure (service
// down, wrong API key) is reported as itself, not dressed up as a sign-in
// problem.
export default function RequireUser({ children }) {
  const { me, loading, error } = useMe()
  const location = useLocation()
  if (loading) return null
  // Not signed in at all (EDSIDER_REQUIRE_SIGNIN on the server): to the
  // sign-in page, which sends them back here afterwards.
  if (error && error.status === 401) {
    const next = encodeURIComponent(location.pathname + location.search)
    return <Navigate to={`/sign-in?next=${next}`} replace />
  }
  if (error) {
    const refused = error.status === 403
    // Locally the identity being checked is the test one kept in this
    // browser (see auth.js devIdentity) - offer to drop it, since a
    // leftover test name is the usual reason for a local 403.
    const testIdentity = isDevAuth ? devIdentity() : null
    return (
      <div className="es-root">
        <main className="scr-main" style={{ maxWidth: 640 }}>
          {refused ? (
            <>
              <h1 className="scr-h2">This email is not allowed</h1>
              {/* The server's message names the refused email - the one
                  thing someone signed in with the wrong Google account
                  needs, to know to switch. */}
              <p>{error.message}</p>
              <p className="es-caption">
                Only people a super admin has added can use EdsiderAI. If you should have access,
                send this email address to your administrator.
              </p>
              {testIdentity ? (
                <button
                  type="button"
                  className="es-btn es-btn--secondary"
                  onClick={() => {
                    setDevIdentity(null)
                    window.location.reload()
                  }}
                >
                  Clear the test identity ({testIdentity})
                </button>
              ) : (
                // Signs out of EdsiderAI only, then back to the sign-in
                // page to pick another account. A plain <a>: /.auth/logout
                // is Azure's, not a page of this app.
                <a
                  className="es-btn es-btn--primary"
                  href={BYPASS_SIGN_IN ? '/sign-in' : logoutUrl('/sign-in')}
                >
                  Sign in with a different account
                </a>
              )}
            </>
          ) : (
            <>
              <h1 className="scr-h2">Could not reach EdsiderAI</h1>
              <p className="es-caption">
                The service did not answer as expected ({error.message}). Check that it is
                running, then reload.
              </p>
            </>
          )}
        </main>
      </div>
    )
  }
  return children(me)
}
