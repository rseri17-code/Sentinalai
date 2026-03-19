// AG UI WebSocket client
// Manages connection lifecycle, reconnect, and event routing

import type { AGUIEvent, WSStatus } from '@/types'

const WS_URL = import.meta.env.VITE_WS_URL || ''

export type WSMessageHandler = (event: AGUIEvent) => void
export type WSStatusHandler = (status: WSStatus) => void

interface WSOptions {
  investigationId: string
  lastSeq?: number
  onEvent: WSMessageHandler
  onStatus: WSStatusHandler
  onConnectionAck?: (connectionId: string) => void
}

const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 16000] // exponential backoff

export class AGUIWebSocket {
  private ws: WebSocket | null = null
  private investigationId: string
  private lastSeq: number
  private onEvent: WSMessageHandler
  private onStatus: WSStatusHandler
  private onConnectionAck?: (id: string) => void
  private reconnectAttempt = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private pingInterval: ReturnType<typeof setInterval> | null = null
  private shouldReconnect = true
  connectionId: string | null = null

  constructor(opts: WSOptions) {
    this.investigationId = opts.investigationId
    this.lastSeq = opts.lastSeq ?? 0
    this.onEvent = opts.onEvent
    this.onStatus = opts.onStatus
    this.onConnectionAck = opts.onConnectionAck
    this.connect()
  }

  private connect(): void {
    const token = localStorage.getItem('agui_token') || ''
    const wsBase = WS_URL || window.location.origin.replace(/^http/, 'ws')
    const url = `${wsBase}/ws/investigations/${this.investigationId}?token=${encodeURIComponent(token)}&last_seq=${this.lastSeq}`

    this.onStatus('connecting')
    try {
      this.ws = new WebSocket(url)
    } catch {
      this.scheduleReconnect()
      return
    }

    this.ws.onopen = () => {
      this.reconnectAttempt = 0
      this.onStatus('connected')
      this.startPing()
    }

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data as string)
        this.handleMessage(data)
      } catch {
        // ignore parse errors
      }
    }

    this.ws.onclose = (event) => {
      this.stopPing()
      if (event.code === 4001) {
        // Auth failure — don't reconnect
        this.onStatus('error')
        return
      }
      this.onStatus('disconnected')
      if (this.shouldReconnect) {
        this.scheduleReconnect()
      }
    }

    this.ws.onerror = () => {
      this.onStatus('error')
    }
  }

  private handleMessage(data: Record<string, unknown>): void {
    const type = data.type as string
    if (type === 'connection.ack') {
      this.connectionId = data.connection_id as string
      this.onConnectionAck?.(this.connectionId)
      return
    }
    if (type === 'heartbeat' || type === 'pong') {
      return
    }
    // Route as AGUIEvent
    if (data.event_type) {
      const event = data as unknown as AGUIEvent
      this.lastSeq = Math.max(this.lastSeq, event.sequence_num)
      this.onEvent(event)
    }
  }

  private scheduleReconnect(): void {
    const delay = RECONNECT_DELAYS[
      Math.min(this.reconnectAttempt, RECONNECT_DELAYS.length - 1)
    ]
    this.reconnectAttempt++
    this.reconnectTimer = setTimeout(() => {
      if (this.shouldReconnect) {
        this.connect()
      }
    }, delay)
  }

  private startPing(): void {
    this.pingInterval = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'ping' }))
      }
    }, 25_000)
  }

  private stopPing(): void {
    if (this.pingInterval) {
      clearInterval(this.pingInterval)
      this.pingInterval = null
    }
  }

  send(data: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data))
    }
  }

  subscribe(investigationId: string): void {
    this.investigationId = investigationId
    this.send({ type: 'subscribe', investigation_id: investigationId })
  }

  disconnect(): void {
    this.shouldReconnect = false
    this.stopPing()
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
    }
    this.ws?.close()
    this.ws = null
  }

  get status(): WSStatus {
    if (!this.ws) return 'disconnected'
    switch (this.ws.readyState) {
      case WebSocket.CONNECTING: return 'connecting'
      case WebSocket.OPEN: return 'connected'
      default: return 'disconnected'
    }
  }
}
