import React, { useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AppShell } from '@/components/layout/AppShell'
import { useAuthStore } from '@/store/investigationStore'
import { authApi } from '@/api/client'

function App() {
  const setUser = useAuthStore((s) => s.setUser)

  // Auto-login in dev mode
  useEffect(() => {
    const existing = localStorage.getItem('agui_token')
    if (!existing) {
      authApi
        .getDevToken('admin', 'dev-user')
        .then((data) => {
          localStorage.setItem('agui_token', data.token)
          setUser({ actor_id: data.actor_id, role: data.role })
        })
        .catch(() => {
          // Auth server unavailable — set anonymous user
          setUser({ actor_id: 'anonymous', role: 'viewer' })
        })
    } else {
      // Parse role from stored token (simplified — use proper JWT decode in prod)
      setUser({ actor_id: 'dev-user', role: 'admin' })
    }
  }, [setUser])

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/investigations" replace />} />
        <Route path="/*" element={<AppShell />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
