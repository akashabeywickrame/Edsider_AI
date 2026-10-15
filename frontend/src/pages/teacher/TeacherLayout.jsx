import { Outlet } from 'react-router-dom'
import TopNav from '../../components/TopNav.jsx'
import RequireUser from '../../components/RequireUser.jsx'

// Same shell as the other apps (see DataEntryLayout): the teacher app's own
// top nav, then the shared content column, gated by RequireUser.
export default function TeacherLayout() {
  return (
    <RequireUser roles={['teacher']}>
      {(me) => (
        <div className="es-root">
          <TopNav app="teacher" who={`Signed in as ${me.name}`} />
          <main className="scr-main">
            <Outlet />
          </main>
        </div>
      )}
    </RequireUser>
  )
}
