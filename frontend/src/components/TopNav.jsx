import { Link, NavLink } from 'react-router-dom'
import './TopNav.css'

// Mirrors design-system/screens/project/Header.dc.html's own per-app
// link sets. Only "data" has real pages behind it so far - "teacher"
// and "admin" are here so the shape is right when those apps exist.
const LINKS = {
  data: [
    { to: '/app/data-entry/check', label: 'Check a question' },
    { to: '/app/data-entry/duplicates', label: 'Find duplicates' },
    { to: '/app/data-entry/within-paper', label: 'Within a paper' },
    { to: '/app/data-entry/decisions', label: 'Decisions' },
    { to: '/app/data-entry/search', label: 'Search' },
    { to: '/app/data-entry/questions', label: 'Question bank' },
    { to: '/app/data-entry/paper-intake', label: 'Paper intake' },
  ],
  teacher: [{ to: '/app/teacher', label: 'My papers' }],
  admin: [{ to: '/app/admin', label: 'Assign papers' }],
}

const APP_LABEL = {
  data: 'Question review',
  teacher: 'Teacher review',
  admin: 'Paper assignment',
}

export default function TopNav({ app, who }) {
  const links = LINKS[app] || []
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
    </nav>
  )
}
