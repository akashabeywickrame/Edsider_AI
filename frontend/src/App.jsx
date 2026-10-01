import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import Landing from './pages/Landing.jsx'
import SignIn from './pages/SignIn.jsx'
import DataEntryLayout from './pages/data-entry/DataEntryLayout.jsx'
import CheckQuestion from './pages/data-entry/CheckQuestion.jsx'
import ComingSoon from './pages/data-entry/ComingSoon.jsx'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/sign-in" element={<SignIn />} />

        <Route path="/app/data-entry" element={<DataEntryLayout />}>
          <Route index element={<Navigate to="check" replace />} />
          <Route path="check" element={<CheckQuestion />} />
          <Route path="duplicates" element={<ComingSoon title="Find duplicates" />} />
          <Route path="within-paper" element={<ComingSoon title="Duplicates within a paper" />} />
          <Route path="decisions" element={<ComingSoon title="Reviewed decisions" />} />
          <Route path="search" element={<ComingSoon title="Search by meaning" />} />
          <Route path="questions" element={<ComingSoon title="Question bank" />} />
          <Route path="paper-intake" element={<ComingSoon title="Paper intake" />} />
        </Route>

        {/* The teacher and admin apps aren't built yet - SignIn's own
            role picker already links here (see its isDev fallback), so
            these need to exist as something better than a blank page. */}
        <Route path="/app/teacher" element={<ComingSoon title="Teacher review" />} />
        <Route path="/app/admin" element={<ComingSoon title="Paper assignment" />} />
        <Route path="*" element={<ComingSoon title="Not found" />} />
      </Routes>
    </BrowserRouter>
  )
}
