import { Link, NavLink } from 'react-router-dom'
import { logoutUrl, BYPASS_SIGN_IN } from '../lib/auth.js'
import './TopNav.css'

// Mirrors design-system/screens/project/Header.dc.html's own per-app
// link sets. Only "data" has real pages behind it so far - "teacher"
// and "admin" are here so the shape is right when those apps exist.
const LINKS = {
  data: [
    { to: '/app/data-entry/check', label: 'Check a question' },
    { to: '/app/data-entry/duplicates', label: 'Find duplicates' },
    { to: '/app/data-entry/within-paper', label: 'Within a paper' },
    { to: '/app/data-entry/search', label: 'Search' },
    { to: '/app/data-entry/paper-intake', label: 'Paper intake' },
  ],
  teacher: [{ to: '/app/teacher', label: 'My papers' }],
  admin: [
    { to: '/app/admin', label: 'Assign papers' },
    { to: '/app/admin/users', label: 'Manage users', superadminOnly: true },
  ],
}

const APP_LABEL = {
  data: 'Question review',
  teacher: 'Teacher review',
  admin: 'Paper assignment',
}

// role is null on a local run with no Easy Auth header - everything is
// open there, same as service.py's require_superadmin().
export default function TopNav({ app, who, role = null }) {
  const canManageUsers = role === null || role === 'superadmin'
  const links = (LINKS[app] || []).filter((l) => !l.superadminOnly || canManageUsers)
  return (
    <nav className="es-nav tn" aria-label="Pages">
      <Link to={links[0]?.to || '/'} aria-label="EdSider AI home" className="tn__logo">
        <img src="/edsider-ai-logo.png" alt="EdSider AI" />
      </Link>
      <span className="tn__badge">{APP_LABEL[app]}</span>
      <ul className="tn__links">
        {links.map((l) => (
          <li key={l.to}>
            <NavLink to={l.to}>{l.label}</NavLink>
          </li>
        ))}
      </ul>
      {who ? <span className="es-nav__who">{who}</span> : null}
      {/* A plain <a>: /.auth/logout is Azure's, not a page of this app - a
          router <Link> would never reach it (see SignIn.jsx). */}
      <a className="tn__signout" href={BYPASS_SIGN_IN ? '/' : logoutUrl('/')}>
        Sign out
      </a>
    </nav>
  )
}
