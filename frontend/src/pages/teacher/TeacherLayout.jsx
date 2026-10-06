import { Outlet } from 'react-router-dom'
import TopNav from '../../components/TopNav.jsx'

// Same shell as the other apps (see DataEntryLayout): the teacher app's own
// top nav, then the shared content column.
export default function TeacherLayout() {
  return (
    <div className="es-root">
      <TopNav app="teacher" who="Signed in as teacher" />
      <main className="scr-main">
        <Outlet />
      </main>
    </div>
  )
}
