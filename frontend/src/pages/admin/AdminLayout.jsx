import { Outlet } from 'react-router-dom'
import TopNav from '../../components/TopNav.jsx'

// Same shell as the data-entry app (see DataEntryLayout): the admin app's own
// top nav, then the shared content column.
export default function AdminLayout() {
  return (
    <div className="es-root">
      <TopNav app="admin" who="Signed in as administrator" />
      <main className="scr-main">
        <Outlet />
      </main>
    </div>
  )
}
