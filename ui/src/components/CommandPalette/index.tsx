import { useEffect, useState, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { Search } from 'lucide-react'

interface CommandItem {
  id: string
  icon: string
  label: string
  sub?: string
  badge?: string
  category: string
  action: () => void
}

export function CommandPalette() {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [recentInvestigations, setRecentInvestigations] = useState<Array<{ incident_id: string; service: string; status: string; confidence: number }>>([])
  const [services, setServices] = useState<Array<{ id: string; health: number }>>([])
  const inputRef = useRef<HTMLInputElement>(null)
  const navigate = useNavigate()

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault()
        setOpen(o => !o)
      }
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  useEffect(() => {
    if (open) {
      setQuery('')
      setSelectedIndex(0)
      setTimeout(() => inputRef.current?.focus(), 50)
      // fetch recent investigations
      fetch('/api/investigations?limit=5')
        .then(r => r.ok ? r.json() : null)
        .then((data: unknown) => {
          if (data && typeof data === 'object' && 'investigations' in data) {
            const d = data as { investigations: unknown }
            if (Array.isArray(d.investigations)) {
              setRecentInvestigations(d.investigations.slice(0, 5).map((inv: unknown) => {
                if (inv && typeof inv === 'object') {
                  const i = inv as Record<string, unknown>
                  return {
                    incident_id: String(i['incident_id'] ?? ''),
                    service: String(i['service'] ?? ''),
                    status: String(i['status'] ?? ''),
                    confidence: typeof i['confidence'] === 'number' ? i['confidence'] : 0,
                  }
                }
                return { incident_id: '', service: '', status: '', confidence: 0 }
              }))
            }
          }
        })
        .catch(() => {})
      // fetch services
      fetch('/api/graph/health-summary')
        .then(r => r.ok ? r.json() : null)
        .then((data: unknown) => {
          if (data && typeof data === 'object' && 'services' in data) {
            const d = data as { services: unknown }
            if (Array.isArray(d.services)) {
              setServices(d.services.map((s: unknown) => {
                if (s && typeof s === 'object') {
                  const sv = s as Record<string, unknown>
                  return { id: String(sv['id'] ?? sv['name'] ?? ''), health: typeof sv['health'] === 'number' ? sv['health'] : 1 }
                }
                return { id: '', health: 1 }
              }))
            }
          }
        })
        .catch(() => {})
    }
  }, [open])

  const quickActions: CommandItem[] = [
    { id: 'new-inv', icon: '▶', label: 'New Investigation', sub: 'Open intake form', badge: 'Action', category: 'QUICK ACTIONS', action: () => { navigate('/investigations'); setOpen(false) } },
    { id: 'dashboard', icon: '📊', label: 'MTTI / MTTR Dashboard', sub: 'View key metrics', badge: '', category: 'QUICK ACTIONS', action: () => { navigate('/dashboard'); setOpen(false) } },
    { id: 'graph', icon: '🗺', label: 'Knowledge Graph', sub: 'Service topology', badge: '', category: 'QUICK ACTIONS', action: () => { navigate('/graph'); setOpen(false) } },
    { id: 'patterns', icon: '⚡', label: 'Pattern Intelligence', sub: 'Failure pattern analysis', badge: '', category: 'QUICK ACTIONS', action: () => { navigate('/'); setOpen(false) } },
  ]

  const recentItems: CommandItem[] = recentInvestigations.map(inv => ({
    id: `inv-${inv.incident_id}`,
    icon: '🔍',
    label: inv.incident_id || 'Unknown',
    sub: `${inv.service} · ${inv.status}`,
    badge: inv.confidence > 0 ? `${Math.round(inv.confidence * 100)}%` : undefined,
    category: 'RECENT INVESTIGATIONS',
    action: () => { navigate(`/investigations/${inv.incident_id}`); setOpen(false) },
  }))

  const FALLBACK_SERVICES = [
    { id: 'api-gateway', health: 0.95 }, { id: 'payment-service', health: 0.62 },
    { id: 'auth-service', health: 0.88 }, { id: 'order-service', health: 0.75 },
    { id: 'postgres-primary', health: 0.55 },
  ]
  const svcList = services.length > 0 ? services : FALLBACK_SERVICES
  const serviceItems: CommandItem[] = svcList.map(s => ({
    id: `svc-${s.id}`,
    icon: s.health > 0.9 ? '🟢' : s.health > 0.7 ? '🟡' : '🔴',
    label: s.id,
    sub: `Health: ${Math.round(s.health * 100)}%`,
    category: 'SERVICES',
    action: () => { navigate(`/graph?service=${s.id}`); setOpen(false) },
  }))

  const allItems: CommandItem[] = [...quickActions, ...recentItems, ...serviceItems]

  const filtered = query.trim()
    ? allItems.filter(item =>
        item.label.toLowerCase().includes(query.toLowerCase()) ||
        (item.sub ?? '').toLowerCase().includes(query.toLowerCase()) ||
        item.category.toLowerCase().includes(query.toLowerCase())
      )
    : allItems

  const grouped = filtered.reduce<Record<string, CommandItem[]>>((acc, item) => {
    if (!acc[item.category]) acc[item.category] = []
    acc[item.category].push(item)
    return acc
  }, {})

  const flatItems = Object.values(grouped).flat()

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setSelectedIndex(i => Math.min(i + 1, flatItems.length - 1)) }
    if (e.key === 'ArrowUp') { e.preventDefault(); setSelectedIndex(i => Math.max(i - 1, 0)) }
    if (e.key === 'Enter') { e.preventDefault(); flatItems[selectedIndex]?.action() }
    if (e.key === 'Escape') setOpen(false)
  }, [flatItems, selectedIndex])

  useEffect(() => { setSelectedIndex(0) }, [query])

  if (!open) return null

  let flatIndex = 0

  return createPortal(
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50" onClick={() => setOpen(false)}>
      <div
        className="max-w-2xl w-full mx-auto mt-[15vh] bg-slate-900 border border-slate-700/80 rounded-xl shadow-2xl shadow-black/60 overflow-hidden"
        onClick={e => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        <div className="flex items-center gap-2 border-b border-slate-800 px-3">
          <Search size={14} className="text-slate-500 shrink-0"/>
          <input
            ref={inputRef}
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search commands, services, investigations..."
            className="bg-transparent text-slate-100 placeholder-slate-500 text-sm px-1 py-3.5 outline-none w-full"
          />
        </div>
        <div className="max-h-96 overflow-y-auto">
          {Object.entries(grouped).map(([category, items]) => (
            <div key={category}>
              <div className="px-4 py-1.5 text-[10px] font-semibold text-slate-500 tracking-widest uppercase">{category}</div>
              {items.map(item => {
                const idx = flatIndex++
                const isSelected = idx === selectedIndex
                return (
                  <div
                    key={item.id}
                    className={`flex items-center gap-3 px-4 py-2.5 cursor-pointer transition ${isSelected ? 'bg-slate-800/80' : 'hover:bg-slate-800/70'}`}
                    onClick={item.action}
                    onMouseEnter={() => setSelectedIndex(idx)}
                  >
                    <span className="text-slate-400 text-sm w-5 text-center">{item.icon}</span>
                    <div className="flex-1 min-w-0">
                      <div className="text-slate-200 text-sm truncate">{item.label}</div>
                      {item.sub && <div className="text-slate-500 text-xs truncate">{item.sub}</div>}
                    </div>
                    {item.badge && <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700 text-slate-400">{item.badge}</span>}
                  </div>
                )
              })}
            </div>
          ))}
          {filtered.length === 0 && (
            <div className="px-4 py-8 text-center text-slate-500 text-sm">No results for &quot;{query}&quot;</div>
          )}
        </div>
        <div className="px-4 py-2 border-t border-slate-800 flex gap-4 text-[10px] text-slate-600">
          <span><kbd className="font-mono">↑↓</kbd> navigate</span>
          <span><kbd className="font-mono">↵</kbd> select</span>
          <span><kbd className="font-mono">esc</kbd> dismiss</span>
        </div>
      </div>
    </div>,
    document.body
  )
}
