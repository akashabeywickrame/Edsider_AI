import { Outlet } from 'react-router-dom'
import TopNav from '../../components/TopNav.jsx'
import RequireUser from '../../components/RequireUser.jsx'

// Same shell as the data-entry app (see DataEntryLayout): the admin app's own
// top nav, then the shared content column, gated by RequireUser - admin and
// superadmin only (RequireUser always admits those two; `roles` adds none).
export default function AdminLayout() {
  return (
    <RequireUser roles={[]}>
      {(me) => (
        <div className="es-root">
          <TopNav app="admin" who={`Signed in as ${me.name}`} role={me.role} />
          <main className="scr-main">
            <Outlet context={me} />
          </main>
        </div>
      )}
    </RequireUser>
  )
}
