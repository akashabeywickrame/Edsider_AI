import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { ToastProvider } from './components/Toast.jsx'
import Landing from './pages/Landing.jsx'
import SignIn from './pages/SignIn.jsx'
import DataEntryLayout from './pages/data-entry/DataEntryLayout.jsx'
import CheckQuestion from './pages/data-entry/CheckQuestion.jsx'
import FindDuplicates from './pages/data-entry/FindDuplicates.jsx'
import WithinPaper from './pages/data-entry/WithinPaper.jsx'
import Search from './pages/data-entry/Search.jsx'
import PaperIntake from './pages/data-entry/PaperIntake.jsx'
import PaperPreview from './pages/data-entry/PaperPreview.jsx'
import ComingSoon from './pages/data-entry/ComingSoon.jsx'
import AdminLayout from './pages/admin/AdminLayout.jsx'
import PaperAssignment from './pages/admin/PaperAssignment.jsx'
import TeacherLayout from './pages/teacher/TeacherLayout.jsx'
import TeacherReview from './pages/teacher/TeacherReview.jsx'

export default function App() {
  return (
    <ToastProvider>
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
          <Route path="paper-intake/preview" element={<PaperPreview />} />
        </Route>

        {/* The teacher and admin apps aren't built yet - SignIn's own
            role picker already links here (see its isDev fallback), so
            these need to exist as something better than a blank page. */}
        <Route path="/app/teacher" element={<TeacherLayout />}>
          <Route index element={<TeacherReview />} />
        </Route>
        <Route path="/app/admin" element={<AdminLayout />}>
          <Route index element={<PaperAssignment />} />
        </Route>
        <Route path="*" element={<ComingSoon title="Not found" />} />
      </Routes>
    </BrowserRouter>
    </ToastProvider>
  )
}
