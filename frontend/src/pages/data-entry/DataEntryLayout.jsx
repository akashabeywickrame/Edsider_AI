import { Outlet } from 'react-router-dom'
import TopNav from '../../components/TopNav.jsx'
import RequireUser from '../../components/RequireUser.jsx'

// .es-root sets the app's own font/colour/background (bundle.css);
// .scr-main is the shared content column every mockup screen uses
// (edsider-tokens.css). RequireUser resolves who is actually signed
// in (falling back to "reviewer" on a local dev server with no Easy
// Auth header, same as current_identity() always has).
export default function DataEntryLayout() {
  return (
    <RequireUser roles={['employee']}>
      {(me) => (
        <div className="es-root">
          <TopNav app="data" who={`Signed in as ${me.name}`} />
          <main className="scr-main">
            <Outlet context={me} />
          </main>
        </div>
      )}
    </RequireUser>
  )
}
