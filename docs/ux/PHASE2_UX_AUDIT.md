# SentinelAI — Phase 2 UX Audit (audit cycle, no implementation)

Per the Phase 2 FIRST TASK: **audit before any UI code.** Every finding traces
to current UI code (file cited); where evidence is absent it is marked
`NOT_PROVEN`. No implementation, refactor, or redesign was performed this cycle.

## Method / evidence base
- `ui/src/components/layout/AppShell.tsx` — the investigation view + routing.
- `ui/src/components/layout/Sidebar.tsx` — navigation + panel list.
- Component inventory: `ui/src/components/*` (21 feature components).
- Accessibility scan across `ui/src/**/*.tsx`: **aria-\* = 1, role= = 0,
  tabIndex = 0, onKeyDown = 1**.
- Responsive scan: **5 breakpoint utilities total** across the app.

## The central finding (drives most others)
`AppShell.InvestigationView` renders **exactly one panel at a time** via a single
`activePanel` value (`timeline | graph | evidence | memory | replay | control |
reflection | tools | mtti`). Only `RiskConfidenceLayer` (risk + confidence) is
persistent. Therefore the five workflow questions (what / why / evidence /
confidence / next) are **spread across mutually-exclusive, click-gated panels** —
the operator must switch panels and hold prior context in working memory.

---

## Findings (Problem · Impact · Severity · Recommendation · Expected benefit)

### C-1 · Single-panel mutual exclusivity — **CRITICAL**
- **Problem:** one panel visible at a time (`AppShell.tsx` InvestigationView
  `activePanel === …` switch). Evidence, timeline, topology, hypothesis, and MTTI
  are never co-visible.
- **Impact:** answering "what/why/evidence/confidence/next" requires ≥4 panel
  switches and heavy working-memory load; directly raises operator MTTI.
- **Recommendation:** add a default **Investigation Summary** view that answers
  all five questions on one screen, with the existing panels as drill-downs.
  Additive — no panel removed.
- **Expected benefit:** the 60-second workflow test passes without context
  switching; fewer clicks to comprehension.

### H-1 · Evidence is behind a click — **HIGH**
- **Problem:** decisive evidence lives only in the `evidence` panel
  (`EvidenceDrawer`), not visible by default.
- **Impact:** violates "reduce clicks to reach evidence"; operators must navigate
  to verify a conclusion.
- **Recommendation:** surface top **decisive** evidence (receipt ids + one line)
  in the summary header; full list stays in the panel.
- **Expected benefit:** faster trust; evidence reachable in 0 clicks.

### H-2 · "Why / owner / next action" not persistent — **HIGH**
- **Problem:** only risk+confidence persist (`RiskConfidenceLayer`). Root cause
  ("why"), owner, and recommendation ("next") are inside panels.
- **Impact:** the operator cannot see the answer + its confidence together;
  comprehension is fragmented.
- **Recommendation:** a persistent **summary header**: root cause · confidence ·
  owner · next action · verifiable badge.
- **Expected benefit:** the decision-critical facts are always on screen.

### H-3 · Accessibility near-absent — **HIGH (enterprise) / MEDIUM (pilot)** — **RESOLVED (Iter 2)**
- **Status:** Investigation navigation is now a WAI-ARIA vertical tablist —
  `role=tablist/tab/tabpanel`, roving `tabIndex`, Arrow/Home/End keyboard nav,
  `aria-selected`/`aria-controls`/`aria-labelledby`, visible `focus-visible`
  ring (`Sidebar.tsx` + `AppShell.tsx`). Estate-wide a11y beyond investigation
  navigation remains partially open (tracked).
- **Problem:** across all `ui/src`: 1 aria attribute, 0 `role=`, 0 `tabIndex`,
  1 `onKeyDown`. Panels switch via `<button onClick>` with no keyboard model or
  focus management; no skip links.
- **Impact:** fails WCAG / Section 508 procurement; unusable keyboard-only or
  with a screen reader; operator fatigue on long shifts.
- **Recommendation:** roles on the panel tablist, roving `tabIndex`, keyboard
  panel switching, visible focus, a skip-to-content link. (Implementation in a
  later iteration.)
- **Expected benefit:** keyboard-driven investigation (faster than mouse for
  power users); procurement compliance.

### H-4 · No responsive / large-display adaptation — **MEDIUM**
- **Problem:** 5 breakpoints total; layout is fixed `h-screen` flex.
- **Impact:** large OCC wall displays waste space (one panel) while the operator
  could see several; small/secondary screens break.
- **Recommendation:** on wide viewports, allow the summary + one drill-down
  side-by-side (progressive, additive); wrap gracefully below.
- **Expected benefit:** uses the operator's real estate; fewer switches on big
  displays.

### M-1 · Dead panels shipped — **MEDIUM (cognitive + bundle)**
- **Problem:** `ArchitectureMiniMap`, `IntelligenceFeed`, `NeuralArchitecturePanel`
  are built but imported nowhere (component inventory vs `AppShell`/`Sidebar`).
- **Impact:** bundle weight; reviewer/operator confusion; maintenance drag.
- **Recommendation:** remove or wire. (Deletion is a candidate iteration — it
  reduces cognitive load and bundle size with zero capability loss.)
- **Expected benefit:** smaller bundle, less confusion.

### M-2 · Panel discoverability / navigation depth — **MEDIUM**
- **Problem:** the panel list (`Sidebar` PANELS) appears only when viewing an
  investigation; each question is a separate click; `CommandPalette` (⌘K) is the
  only keyboard affordance.
- **Impact:** operators must learn which panel answers which question.
- **Recommendation:** the summary view (C-1) makes the common answers zero-click;
  keep panels for depth; label panels by the question they answer.
- **Expected benefit:** lower learning curve; higher adoption.

### L-1 · Information duplication — **LOW**
- **Problem:** MTTI panel + `RiskConfidenceLayer` both surface timing/confidence;
  `MissionControl` and the new `OperationalHealth` overlap on service rollups.
- **Impact:** minor redundancy / mild confusion.
- **Recommendation:** the summary consolidates the headline numbers; panels keep
  the detail. No removal of capability.
- **Expected benefit:** single source of headline truth.

**Severity tally:** Critical 1 · High 3 (one is High/Medium) · Medium 2 · Low 1.

---

## Information Architecture — the Investigation Workspace (design only)

**Principle:** arrange by investigation workflow, not visual preference; add a
summary layer over the existing panels without removing any capability.

```
┌─ Persistent Summary Header (always visible) ───────────────────────────┐
│ WHAT: incident + service   WHY: root cause   CONF: NN% ✓verifiable      │
│ OWNER: team   NEXT: recommended action        [MTTI: to-actionable]     │
│ decisive evidence: ev1, ev2  →  open Evidence                          │
└────────────────────────────────────────────────────────────────────────┘
┌─ Workflow tabs (existing panels, ordered by flow) ─────────────────────┐
│ Summary · Timeline · Evidence · Topology · Hypothesis · Recommendation │
│ · Decision/Replay · MTTI                                                │
│  (wide viewport: Summary + one drill-down side-by-side)                 │
└────────────────────────────────────────────────────────────────────────┘
```

Mapping to the required workspace elements — every one is an existing component,
re-surfaced, none redesigned:

| Workspace element | Existing source | Change proposed |
|---|---|---|
| Investigation Summary | (new composition of existing store fields) | surface, don't compute |
| Timeline | `IncidentCommandCenter` | keep; reachable as tab |
| Evidence | `EvidenceDrawer` | keep; decisive items lifted to header |
| Topology | `CausalGraph` / `ExecutionGraph` | keep |
| Hypothesis / Confidence | `RiskConfidenceLayer` + store | header shows headline; panel shows detail |
| Recommendations | store `next_action` / control | lift to header |
| Decision History / Replay | `ControlPanel` / `ReplayMode` | keep |
| MTTI | `MttiTimeline` | header shows headline; panel shows breakdown |
| External Context | Operational Health drill-down | keep |
| Operator Notes | **NOT_PROVEN** — no existing notes component found | out of scope unless a component exists |

The summary composes **only fields the store/endpoints already provide** (root
cause, confidence, owner via Operational Health drill-down, next action,
verifiable, decisive evidence, MTTI). No new backend, workflow, metric, or
runtime behavior. Deterministic behavior is untouched (UI-only).

---

## Iteration plan (small, validated)
1. **Persistent Investigation Summary header** (addresses C-1, H-1, H-2) — the
   single highest-MTTI-impact change; additive, no panel removed.
   **STATUS: IMPLEMENTED** — `ui/src/components/InvestigationSummary` mounted in
   `AppShell.InvestigationView` above the risk/confidence bar. Displays only
   existing investigation fields (status, service/owner, root cause, confidence,
   evidence count → Evidence panel, next action = `awaiting_approval_for`,
   verification = `replay_available`); derives nothing, adds no backend call,
   flat + semantic + keyboard/screen-reader friendly + responsive.
2. **Accessibility baseline** (H-3) — roles + keyboard panel switching + focus.
3. **Remove dead panels** (M-1) — pure subtraction, zero capability loss.
4. **Wide-viewport summary + drill-down** (H-4) — progressive enhancement.

Each iteration: one workflow, regression green, existing functionality and
determinism preserved, documented.

---

## Quality gates (this audit cycle)
| Gate | Result |
|---|---|
| Regression | PASS — no code changed this cycle (last full run 5982 passed) |
| Determinism / Evidence / Confidence / Telemetry | PASS — untouched (audit only) |
| Accessibility | audited (H-3) — remediation is a later iteration |
| Documentation | PASS — this audit |

## Scope
Audit only. No UI implemented, refactored, or redesigned. Every claim cites
current UI code; unverifiable items (Operator Notes) are `NOT_PROVEN`.
