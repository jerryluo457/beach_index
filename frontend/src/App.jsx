import { Navigate, Route, Routes } from 'react-router-dom'
import Home from './pages/Home.jsx'
import BeachDetail from './pages/BeachDetail.jsx'
import Upload from './pages/Upload.jsx'

/**
 * App — the routing table, nothing else.
 *
 * /upload is deliberately absent from any nav; it's reachable from a small
 * footer link because it's an admin tool, not a visitor feature.
 */
export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/beach/:id" element={<BeachDetail />} />
      <Route path="/upload" element={<Upload />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
