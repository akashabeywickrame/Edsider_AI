import { Outlet } from 'react-router-dom'
import TopNav from '../../components/TopNav.jsx'

// .es-root sets the app's own font/colour/background (bundle.css);
// .scr-main is the shared content column every mockup screen uses
// (edsider-tokens.css). No EasyAuth-derived name is available in a
// local dev server, so this falls back to "reviewer" - the exact
// fallback current_reviewer() already uses in app.py.
export default function DataEntryLayout() {
  return (
    <div className="es-root">
      <TopNav app="data" who="Signed in as reviewer" />
      <main className="scr-main">
        <Outlet />
      </main>
    </div>
  )
}
