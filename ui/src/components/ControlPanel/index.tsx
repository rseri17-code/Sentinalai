// Panel 6: Control Panel
// Human-in-the-loop controls: approve/reject/pause/resume
// RBAC-gated: operators can pause/resume, approvers can approve/reject

import React, { useState, useEffect } from 'react'
import { useInvestigationStore, useAuthStore } from '@/store/investigationStore'
import { controlApi } from '@/api/client'
import type { ControlAction } from '@/types'
import { Shield, CheckCircle, XCircle, Pause, Play, AlertTriangle, Clock } from 'lucide-react'
import clsx from 'clsx'
import { formatDistanceToNow } from 'date-fns'

interface ActionButtonProps {
  action: string
  label: string
  icon: React.ReactNode
  variant: 'approve' | 'reject' | 'neutral' | 'pause'
  minRole: 'operator' | 'approver'
  onAction: (action: string, reason: string) => void
}

function ActionButton({ action, label, icon, variant, minRole, onAction }: ActionButtonProps) {
  const { hasRole } = useAuthStore()
  const [showReason, setShowReason] = useState(false)
  const [reason, setReason] = useState('')
  const canAct = hasRole(minRole)

  const colors = {
    approve: 'bg-green-700 hover:bg-green-600 text-white',
    reject: 'bg-red-700 hover:bg-red-600 text-white',
    pause: 'bg-yellow-700 hover:bg-yellow-600 text-white',
    neutral: 'bg-slate-700 hover:bg-slate-600 text-slate-200',
  }

  const handleClick = () => {
    if (!canAct) return
    if (action === 'pause' || action === 'resume' || action === 'escalate') {
      onAction(action, '')
    } else {
      setShowReason(!showReason)
    }
  }

  const handleConfirm = () => {
    onAction(action, reason)
    setShowReason(false)
    setReason('')
  }

  return (
    <div>
      <button
        onClick={handleClick}
        disabled={!canAct}
        className={clsx(
          'btn flex items-center gap-1.5 w-full justify-center',
          canAct ? colors[variant] : 'bg-slate-800 text-slate-600 cursor-not-allowed'
        )}
        title={!canAct ? `Requires ${minRole} role` : label}
      >
        {icon}
        {label}
        {!canAct && <Shield size={11} className="ml-1" />}
      </button>

      {showReason && (
        <div className="mt-2 space-y-2">
          <textarea
            className="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-blue-600 resize-none"
            placeholder="Reason (required for audit)"
            rows={2}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="flex gap-2">
            <button onClick={handleConfirm} className="btn-primary text-xs flex-1">
              Confirm
            </button>
            <button onClick={() => setShowReason(false)} className="btn-ghost text-xs flex-1">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function ControlActionRow({ action }: { action: ControlAction }) {
  const roleColors: Record<string, string> = {
    admin: 'text-red-400', approver: 'text-purple-400',
    operator: 'text-blue-400', viewer: 'text-slate-400',
  }
  const actionIcons: Record<string, React.ReactNode> = {
    approve: <CheckCircle size={11} className="text-green-400" />,
    reject: <XCircle size={11} className="text-red-400" />,
    pause: <Pause size={11} className="text-yellow-400" />,
    resume: <Play size={11} className="text-green-400" />,
    override: <AlertTriangle size={11} className="text-orange-400" />,
  }

  return (
    <div className="flex items-start gap-2 py-2 px-2 hover:bg-slate-800/50 rounded text-xs">
      <div className="mt-0.5 shrink-0">{actionIcons[action.action_type] || <Shield size={11} />}</div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium text-slate-200 capitalize">{action.action_type}</span>
          <span className={clsx('text-[10px]', roleColors[action.actor_role] || 'text-slate-400')}>
            {action.actor_id} ({action.actor_role})
          </span>
        </div>
        {action.reason && (
          <div className="text-slate-400 mt-0.5 line-clamp-2">{action.reason}</div>
        )}
      </div>
      <div className="text-slate-600 text-[10px] shrink-0 flex items-center gap-1">
        <Clock size={9} />
        {formatDistanceToNow(new Date(action.timestamp), { addSuffix: true })}
      </div>
    </div>
  )
}

export function ControlPanel() {
  const {
    investigation,
    investigationId,
    controlActions,
    pendingApproval,
    sendControlAction,
    refreshInvestigation,
  } = useInvestigationStore()
  const { hasRole } = useAuthStore()
  const [loading, setLoading] = useState(false)
  const [pendingActions, setPendingActions] = useState<ControlAction[]>([])

  useEffect(() => {
    if (!investigationId) return
    controlApi.getPending(investigationId)
      .then((res) => setPendingActions(res.pending_approvals))
      .catch(console.error)
  }, [investigationId, controlActions.length])

  const handleAction = async (action: string, reason: string) => {
    if (!investigationId) return
    setLoading(true)
    try {
      await sendControlAction(action, reason)
      await refreshInvestigation()
    } catch (err) {
      console.error(err)
    } finally {
      setLoading(false)
    }
  }

  const isRunning = investigation?.status === 'running'
  const isPaused = investigation?.status === 'paused'
  const isAwaitingApproval = investigation?.status === 'awaiting_approval' || !!pendingApproval

  return (
    <div className="flex flex-col h-full p-4 space-y-4">
      {/* Pending approval alert */}
      {isAwaitingApproval && (
        <div className="panel border-yellow-700 bg-yellow-900/20 p-3 animate-pulse">
          <div className="flex items-center gap-2 mb-2">
            <AlertTriangle size={14} className="text-yellow-400" />
            <span className="text-sm font-medium text-yellow-300">Awaiting Approval</span>
          </div>
          <p className="text-xs text-yellow-200/70">
            {pendingApproval || investigation?.awaiting_approval_for || 'Agent is waiting for human approval before proceeding.'}
          </p>
        </div>
      )}

      {/* Action buttons */}
      <div className="panel p-4 space-y-3">
        <div className="text-xs text-slate-500 uppercase tracking-wider font-medium flex items-center gap-1.5">
          <Shield size={11} />
          Control Actions
        </div>

        <div className="grid grid-cols-2 gap-2">
          <ActionButton
            action="approve"
            label="Approve"
            icon={<CheckCircle size={13} />}
            variant="approve"
            minRole="approver"
            onAction={handleAction}
          />
          <ActionButton
            action="reject"
            label="Reject"
            icon={<XCircle size={13} />}
            variant="reject"
            minRole="approver"
            onAction={handleAction}
          />
          {isRunning && (
            <ActionButton
              action="pause"
              label="Pause"
              icon={<Pause size={13} />}
              variant="pause"
              minRole="operator"
              onAction={handleAction}
            />
          )}
          {isPaused && (
            <ActionButton
              action="resume"
              label="Resume"
              icon={<Play size={13} />}
              variant="neutral"
              minRole="operator"
              onAction={handleAction}
            />
          )}
          <ActionButton
            action="escalate"
            label="Escalate"
            icon={<AlertTriangle size={13} />}
            variant="neutral"
            minRole="operator"
            onAction={handleAction}
          />
          <ActionButton
            action="override"
            label="Override"
            icon={<AlertTriangle size={13} />}
            variant="reject"
            minRole="approver"
            onAction={handleAction}
          />
        </div>

        {loading && (
          <div className="text-xs text-blue-400 text-center animate-pulse">Applying action...</div>
        )}
      </div>

      {/* Role info */}
      <div className="panel p-3 text-xs space-y-1">
        <div className="text-slate-500 uppercase tracking-wider mb-1.5">Role Permissions</div>
        {[
          { role: 'viewer', perms: 'Read only' },
          { role: 'operator', perms: 'Pause / Resume / Escalate' },
          { role: 'approver', perms: 'Approve / Reject / Override' },
          { role: 'admin', perms: 'All actions' },
        ].map(({ role, perms }) => (
          <div key={role} className="flex justify-between">
            <span className={clsx(hasRole(role as 'viewer') ? 'text-green-400' : 'text-slate-600')}>
              {hasRole(role as 'viewer') ? '✓' : '✗'} {role}
            </span>
            <span className="text-slate-500">{perms}</span>
          </div>
        ))}
      </div>

      {/* Audit log */}
      <div className="panel flex-1 flex flex-col overflow-hidden">
        <div className="panel-header">
          <span className="text-xs font-medium text-slate-300">Audit Log</span>
          <span className="badge bg-slate-800 text-slate-400">{controlActions.length}</span>
        </div>
        <div className="flex-1 scroll-panel divide-y divide-slate-800/50">
          {controlActions.length === 0 ? (
            <div className="text-center text-slate-600 py-6 text-xs">No control actions yet.</div>
          ) : (
            [...controlActions].reverse().map((a) => (
              <ControlActionRow key={a.action_id} action={a} />
            ))
          )}
        </div>
      </div>
    </div>
  )
}
