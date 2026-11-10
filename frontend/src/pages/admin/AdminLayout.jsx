import { Navigate, Outlet, useLocation } from 'react-router-dom'
import TopNav from '../../components/TopNav.jsx'
import RequireUser from '../../components/RequireUser.jsx'
import { isDataEntryOnly } from '../../lib/flow.js'

// Same shell as the data-entry app (see DataEntryLayout): the admin app's own
// top nav, then the shared content column, gated by RequireUser - admin and
// superadmin only (RequireUser always admits those two; `roles` adds none).
// With no teacher review here (lib/flow.js) there is no paper to assign:
// Manage users stays, and the assignment page sends them to Paper intake.
export default function AdminLayout() {
  const { pathname } = useLocation()
  return (
    <RequireUser roles={[]}>
      {(me) =>
        isDataEntryOnly(me) && pathname.replace(/\/+$/, '') === '/app/admin' ? (
          <Navigate to="/app/data-entry/paper-intake" replace />
        ) : (
          <div className="es-root">
            <TopNav app="admin" who={`Signed in as ${me.name}`} role={me.role}
                    dataEntryOnly={isDataEntryOnly(me)} />
            <main className="scr-main">
              <Outlet context={me} />
            </main>
          </div>
        )
      }
    </RequireUser>
  )
}
