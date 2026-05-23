#!/usr/bin/env node
/**
 * PreCompact hook — handoff context before compaction
 *
 * Before Claude compacts the context:
 * 1. Writes memory/hot/active_task.md with current objective + open state
 * 2. Writes .claude/session-state.json (existing behavior — restored at SessionStart)
 */

const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const repoRoot = path.resolve(__dirname, '../..');

function safeExec(cmd) {
  try {
    return execSync(cmd, { cwd: repoRoot, encoding: 'utf8', timeout: 5000 }).trim();
  } catch { return ''; }
}

const now = new Date().toISOString();
const branch      = safeExec('git rev-parse --abbrev-ref HEAD');
const lastCommit  = safeExec('git log --oneline -1');
const recentLog   = safeExec('git log --oneline -5').split('\n').filter(Boolean);
const gitStatus   = safeExec('git status --short').split('\n').filter(Boolean).slice(0, 15);
const modifiedPy  = safeExec("git diff --name-only HEAD -- '*.py'").split('\n').filter(Boolean);
const staged      = safeExec('git diff --cached --name-only').split('\n').filter(Boolean);

// --- Derive objective from current_decisions.md if available ---
const decisionsPath = path.join(repoRoot, 'memory', 'hot', 'current_decisions.md');
let objectiveHint = '_(not set — update memory/hot/current_decisions.md during task)_';
if (fs.existsSync(decisionsPath)) {
  try {
    const raw = fs.readFileSync(decisionsPath, 'utf8');
    const firstDecision = raw.split('\n').find(l => l.startsWith('- '));
    if (firstDecision) objectiveHint = firstDecision.replace(/^- /, '');
  } catch { /* non-critical */ }
}

// --- Check for open risks ---
const risks = [];
if (staged.length > 0) risks.push(`${staged.length} file(s) staged but not committed`);
if (modifiedPy.length > 0) risks.push(`${modifiedPy.length} Python file(s) modified vs HEAD`);

const cacheDir = path.join(repoRoot, '.pytest_cache');
if (!fs.existsSync(cacheDir)) risks.push('pytest cache not present — tests may not have run');

const risksMd = risks.length > 0
  ? risks.map(r => `- ⚠ ${r}`).join('\n')
  : '_(none detected)_';

const statusMd = gitStatus.length > 0
  ? gitStatus.map(l => `- \`${l}\``).join('\n')
  : '_(clean)_';

// --- Write memory/hot/active_task.md ---
const activeTaskMd = `# Active Task — Compaction Handoff
<!-- Written by PreCompact hook at ${now} -->
<!-- Restored by SessionStart hook — also shown if .claude/session-state.json is present -->

## Objective
${objectiveHint}

## Branch
${branch} | ${lastCommit}

## Git Status at Compaction
${statusMd}

## Changed Python Files (vs HEAD)
${modifiedPy.length > 0 ? modifiedPy.map(f => `- ${f}`).join('\n') : '_(none)_'}

## Staged for Commit
${staged.length > 0 ? staged.map(f => `- ${f}`).join('\n') : '_(none)_'}

## Open Risks
${risksMd}

## Next Recommended Action
- Continue on branch \`${branch}\`
- Check memory/hot/current_decisions.md for in-progress notes
- Run \`git status\` and \`python -m pytest --tb=short -q\` to re-orient
`;

try {
  const hotDir = path.join(repoRoot, 'memory', 'hot');
  fs.mkdirSync(hotDir, { recursive: true });
  fs.writeFileSync(path.join(hotDir, 'active_task.md'), activeTaskMd);
} catch { /* non-critical */ }

// --- Legacy: write .claude/session-state.json (preserve existing behavior) ---
const stateFile = path.join(repoRoot, '.claude', 'session-state.json');
const state = {
  compacted_at: now,
  branch,
  last_commit: lastCommit,
  recent_commits: recentLog,
  git_status: gitStatus,
  modified_py: modifiedPy,
  staged,
  open_risks: risks,
  objective: objectiveHint,
  next_action: `Continue on branch ${branch} — review memory/hot/active_task.md`,
  // Legacy fields (kept for backward compat with SessionStart restore)
  open_tasks: (() => {
    const tasksDir = path.join(repoRoot, 'tasks');
    if (!fs.existsSync(tasksDir)) return [];
    return fs.readdirSync(tasksDir).filter(f => f.endsWith('.md')).slice(0, 5);
  })(),
  workers: [
    'ops_worker', 'log_worker', 'metrics_worker', 'apm_worker',
    'knowledge_worker', 'itsm_worker', 'devops_worker',
  ],
  note: 'See memory/hot/active_task.md for full handoff context.',
};

try {
  fs.mkdirSync(path.dirname(stateFile), { recursive: true });
  fs.writeFileSync(stateFile, JSON.stringify(state, null, 2));
  console.log('[sentinalai-hook] Compaction handoff written to memory/hot/active_task.md');
} catch (e) {
  console.error(`[sentinalai-hook] Could not save session state: ${e.message}`);
}

process.exit(0);
