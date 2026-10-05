import { Link, useSearchParams } from 'react-router-dom'
import Footer from '../components/Footer.jsx'
import { isDevAuth as isDev, loginUrl as authUrl } from '../lib/auth.js'
import './SignIn.css'

// Where each role lands once signed in. These routes don't exist in
// the app yet (same as /sign-in itself didn't exist before this page
// was built) - they're the real destination to come back to once the
// role's own app is built, not a guess at a URL outside this project.
const ROLES = [
  {
    key: 'data',
    label: 'Data entry',
    blurb: 'Upload papers, check new questions and review possible duplicates in the bank.',
    dest: '/app/data-entry',
  },
  {
    key: 'teacher',
    label: 'Teacher',
    blurb: 'Review the papers assigned to you, question by question, and send them on or back.',
    dest: '/app/teacher',
  },
  {
    key: 'admin',
    label: 'Administrator',
    blurb: 'Hand checked papers to teachers and follow each paper through review.',
    dest: '/app/admin',
  },
]

export default function SignIn() {
  const [params] = useSearchParams()
  const requested = params.get('role')
  const role = ROLES.find((r) => r.key === requested) || ROLES[0]

  return (
    <div className="si-page">
      <main className="si-main">
        <img className="si-logo" src="/edsider-ai-logo.png" alt="EdSider AI" />
        <h1 className="si-h1">Welcome!</h1>
        <p className="si-lede">Sign in to EdSider AI to review and check questions</p>
        <div className="si-rule" aria-hidden="true" />

        <section className="si-card" aria-label="Sign in">
          {/* Shared across every role for now - a distinct photo per
              role (the design mockup pairs "Administrator" with a photo
              of someone at a desk) can replace this once one exists to
              use honestly; see hero.png's own note on Landing. */}
          <div className="si-card__photo">
            <img src="/hero.png" alt="Two teachers in a classroom with books" />
          </div>
          <div className="si-card__body">
            <div className="si-roles-wrap">
              <p className="si-roles-label">I am signing in as</p>
              <div className="si-roles" role="group" aria-label="Role">
                {ROLES.map((r) => (
                  <a
                    key={r.key}
                    className="si-role"
                    aria-pressed={r.key === role.key}
                    href={`/sign-in?role=${r.key}`}
                  >
                    {r.label}
                  </a>
                ))}
              </div>
            </div>

            <p className="si-blurb">{role.blurb}</p>

            {/* Real Easy Auth only exists once this is deployed behind
                it (see authUrl's own note) - in local dev there is
                nothing at /.auth/login/* to redirect through, so these
                go straight to the role's app instead. This is a dev-only
                convenience, not a second auth system: isDev is false in
                any real build. */}
            <Link className="si-btn" to={isDev ? role.dest : authUrl('aad', role.dest)}>
              <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
                <rect x="0" y="0" width="8.5" height="8.5" fill="#ffffff" />
                <rect x="9.5" y="0" width="8.5" height="8.5" fill="#ffffff" opacity=".85" />
                <rect x="0" y="9.5" width="8.5" height="8.5" fill="#ffffff" opacity=".85" />
                <rect x="9.5" y="9.5" width="8.5" height="8.5" fill="#ffffff" opacity=".7" />
              </svg>
              Sign in with Microsoft
            </Link>
            <div className="si-or" aria-hidden="true">
              or
            </div>
            <Link className="si-google" to={isDev ? role.dest : authUrl('google', role.dest)}>
              <svg width="20" height="20" viewBox="0 0 48 48" aria-hidden="true">
                <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
                <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
                <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
                <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
              </svg>
              Sign in with Google
            </Link>
            <p className="es-caption">
              {isDev
                ? "Local dev - these buttons skip real sign-in and go straight to the role's app."
                : 'Use the Microsoft or Google account your EdSider profile is linked to. New here? Ask the EdSider team to add you.'}
            </p>
          </div>
        </section>
      </main>
      <Footer />
    </div>
  )
}
