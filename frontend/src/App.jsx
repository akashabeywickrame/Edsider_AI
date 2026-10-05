import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import Landing from './pages/Landing.jsx'
import SignIn from './pages/SignIn.jsx'
import DataEntryLayout from './pages/data-entry/DataEntryLayout.jsx'
import CheckQuestion from './pages/data-entry/CheckQuestion.jsx'
import FindDuplicates from './pages/data-entry/FindDuplicates.jsx'
import WithinPaper from './pages/data-entry/WithinPaper.jsx'
import Search from './pages/data-entry/Search.jsx'
import PaperIntake from './pages/data-entry/PaperIntake.jsx'
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
          <Route path="duplicates" element={<FindDuplicates />} />
          <Route path="within-paper" element={<WithinPaper />} />
          <Route path="search" element={<Search />} />
          <Route path="paper-intake" element={<PaperIntake />} />
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
