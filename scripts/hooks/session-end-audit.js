#!/usr/bin/env node
/**
 * SessionEnd hook — structured reflection + promotion queue
 *
 * 1. Appends structured reflection to memory/hot/stop_log.md
 * 2. Harvests memory/hot/current_decisions.md if present
 * 3. Scans for PROMOTE markers and echoes count
 * 4. Appends git-state entry to .claude/audit.jsonl (existing behavior preserved)
 * 5. Clears current_decisions.md after harvest
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const repoRoot = path.resolve(__dirname, '../..');

function safeExec(cmd) {
  try {
    return execSync(cmd, { cwd: repoRoot, encoding: 'utf8', timeout: 5000 }).trim();
  } catch { return ''; }
}

const now = new Date().toISOString();

// --- Git facts ---
const branch       = safeExec('git rev-parse --abbrev-ref HEAD');
const headCommit   = safeExec('git log --oneline -1');
const changedVsHead = safeExec('git diff --name-only HEAD').split('\n').filter(Boolean);
const staged        = safeExec('git diff --cached --name-only').split('\n').filter(Boolean);
const commitsSince  = safeExec('git log --oneline -3').split('\n').filter(Boolean);

// --- Tests ran? ---
const cacheDir = path.join(repoRoot, '.pytest_cache');
const testsRan = fs.existsSync(cacheDir)
  ? `yes (cache mtime: ${fs.statSync(cacheDir).mtime.toISOString()})`
  : 'not detected';

// --- Harvest current_decisions.md ---
const decisionsPath = path.join(repoRoot, 'memory', 'hot', 'current_decisions.md');
let decisionsContent = '';
let promoteCandidates = [];

if (fs.existsSync(decisionsPath)) {
  try {
    const raw = fs.readFileSync(decisionsPath, 'utf8');
    // Skip if still at default template
    if (!raw.includes('_No decisions recorded for this session yet._')) {
      decisionsContent = raw;
      promoteCandidates = [...raw.matchAll(/\[PROMOTE:\s*([^\]]+)\]/g)].map(m => m[1].trim());
    }
  } catch { /* non-critical */ }
}

// --- Build stop_log entry ---
const changedMd = changedVsHead.length > 0
  ? changedVsHead.map(f => `- ${f}`).join('\n')
  : '_(none)_';

const stagedMd = staged.length > 0
  ? staged.map(f => `- ${f}`).join('\n')
  : '_(none)_';

const commitsMd = commitsSince.length > 0
  ? commitsSince.map(c => `- \`${c}\``).join('\n')
  : '_(none)_';

const decisionsMd = decisionsContent
  ? decisionsContent
      .split('\n')
      .filter(l => l.startsWith('- '))
      .join('\n') || '_(none recorded)_'
  : '_(current_decisions.md not updated this session)_';

const promoteSection = promoteCandidates.length > 0
  ? promoteCandidates.map(p => `- [ ] Promote to: \`${p}\``).join('\n')
  : '_(none — add [PROMOTE: target] to current_decisions.md to flag candidates)_';

const entry = `
## Session ${now}

**Branch**: ${branch}
**Head commit**: ${headCommit}

### Files Changed vs HEAD
${changedMd}

### Staged for Commit
${stagedMd}

### Recent Commits This Session
${commitsMd}

### Tests Run
${testsRan}

### Decisions & Notes (from current_decisions.md)
${decisionsMd}

### Promotion Candidates
${promoteSection}

---`;

// --- Append to stop_log.md ---
const stopLogPath = path.join(repoRoot, 'memory', 'hot', 'stop_log.md');
try {
  fs.mkdirSync(path.dirname(stopLogPath), { recursive: true });
  // Replace placeholder if first entry
  if (fs.existsSync(stopLogPath)) {
    const existing = fs.readFileSync(stopLogPath, 'utf8');
    if (existing.includes('_No sessions recorded yet._')) {
      fs.writeFileSync(stopLogPath, existing.replace('_No sessions recorded yet._', '') + entry);
    } else {
      fs.appendFileSync(stopLogPath, entry);
    }
  } else {
    fs.writeFileSync(stopLogPath, `# Stop Log — Session Reflection & Promotion Queue\n${entry}`);
  }
} catch { /* non-critical */ }

// --- Clear current_decisions.md after harvest ---
if (decisionsContent) {
  try {
    fs.writeFileSync(decisionsPath, `# Current Session Decisions
<!-- Written by agent during session, harvested by SessionEnd hook -->
<!-- Cleared after SessionEnd harvests it into stop_log.md -->

_No decisions recorded for this session yet._
`);
  } catch { /* non-critical */ }
}

// --- Echo promotion count ---
if (promoteCandidates.length > 0) {
  console.log(`[sentinalai-hook] ${promoteCandidates.length} PROMOTE candidate(s) written to stop_log.md`);
}

// --- Legacy: append to .claude/audit.jsonl (preserve existing behavior) ---
const auditFile = path.join(repoRoot, '.claude', 'audit.jsonl');
try {
  fs.mkdirSync(path.dirname(auditFile), { recursive: true });
  fs.appendFileSync(auditFile, JSON.stringify({
    session_end: now,
    branch,
    head_commit: headCommit,
    files_changed_vs_head: changedVsHead,
    staged_for_commit: staged,
    tests_cache_modified: fs.existsSync(cacheDir) ? fs.statSync(cacheDir).mtime.toISOString() : null,
  }) + '\n');
} catch { /* non-critical */ }

process.exit(0);
