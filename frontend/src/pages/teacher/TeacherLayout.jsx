import { Outlet } from 'react-router-dom'
import TopNav from '../../components/TopNav.jsx'
import RequireUser from '../../components/RequireUser.jsx'
import { isDataEntryOnly } from '../../lib/flow.js'

// Same shell as the other apps (see DataEntryLayout): the teacher app's own
// top nav, then the shared content column, gated by RequireUser. Off when
// data entry sends papers to EdSider itself (lib/flow.js) - reviewed there.
export default function TeacherLayout() {
  return (
    <RequireUser roles={['teacher']}>
      {(me) =>
        isDataEntryOnly(me) ? (
          <div className="es-root">
            <main className="scr-main" style={{ maxWidth: 640 }}>
              <h1 className="scr-h2">Teacher review is off here</h1>
              <p className="es-caption">
                Data entry sends each paper to EdSider, where it is reviewed.
              </p>
              <a className="es-btn es-btn--primary" href="/app/data-entry/paper-intake">
                Go to Paper intake
              </a>
            </main>
          </div>
        ) : (
          <div className="es-root">
            <TopNav app="teacher" who={`Signed in as ${me.name}`} />
            <main className="scr-main">
              <Outlet />
            </main>
          </div>
        )
      }
    </RequireUser>
  )
}
