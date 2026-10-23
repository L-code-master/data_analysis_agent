import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { Spin } from 'antd'
import { AuthProvider, useAuth } from './stores/auth'
import AppLayout from './components/AppLayout'

// ★ 路由级代码分割（docs/16 P3-18）：原先是单一 1260KB chunk，
//   全部页面静态 import。按路由 lazy 拆分 + vite manualChunks
//   分离体积大户（antd / 图表 / React 核心），首屏只加载登录页所需。
const LoginPage = lazy(() => import('./pages/Login'))
const RegisterPage = lazy(() => import('./pages/Register'))
const VerifyEmailPage = lazy(() => import('./pages/VerifyEmail'))
const DashboardPage = lazy(() => import('./pages/Dashboard'))
const UploadPage = lazy(() => import('./pages/Upload'))
const MappingPage = lazy(() => import('./pages/Mapping'))
const ProgressPage = lazy(() => import('./pages/Progress'))
const ReportPage = lazy(() => import('./pages/Report'))
const BillingPage = lazy(() => import('./pages/Billing'))
const SettingsPage = lazy(() => import('./pages/Settings'))
const ChatPage = lazy(() => import('./pages/Chat'))

function PageLoading() {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 120 }}>
      <Spin size="large" />
    </div>
  )
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, initialized } = useAuth()
  const location = useLocation()

  if (!initialized) {
    return <PageLoading />
  }
  if (!user) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />
  }
  return <>{children}</>
}

/** lazy 页面统一包一层 Suspense。 */
function Lazy({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<PageLoading />}>{children}</Suspense>
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<Lazy><LoginPage /></Lazy>} />
      <Route path="/register" element={<Lazy><RegisterPage /></Lazy>} />
      <Route path="/verify-email" element={<Lazy><VerifyEmailPage /></Lazy>} />

      <Route
        path="/"
        element={
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<Lazy><DashboardPage /></Lazy>} />
        <Route path="upload" element={<Lazy><UploadPage /></Lazy>} />
        <Route path="mapping/:sourceId" element={<Lazy><MappingPage /></Lazy>} />
        <Route path="progress/:diagnosisId" element={<Lazy><ProgressPage /></Lazy>} />
        <Route path="report/:diagnosisId" element={<Lazy><ReportPage /></Lazy>} />
        <Route path="billing" element={<Lazy><BillingPage /></Lazy>} />
        <Route path="settings" element={<Lazy><SettingsPage /></Lazy>} />
        <Route path="chat" element={<Lazy><ChatPage /></Lazy>} />
      </Route>

      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  )
}
