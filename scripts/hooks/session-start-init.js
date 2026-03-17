#!/usr/bin/env node
/**
 * SessionStart hook — context initialization
 *
 * Runs at the start of every Claude Code session:
 *   1. Prints a concise repo health summary (branch, last commit, test count)
 *   2. Loads .claude/session-state.json if it exists (post-compaction reload)
 *   3. Detects package manager (npm/pnpm/yarn/bun) for this repo
 *   4. Warns if workers/ has uncommitted changes
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

// --- Repo health summary ---
const branch = safeExec('git rev-parse --abbrev-ref HEAD');
const lastCommit = safeExec('git log --oneline -1');
const testCount = safeExec('find tests -name "test_*.py" | wc -l').trim();
const workerChanges = safeExec("git diff --name-only -- 'workers/*.py' 'supervisor/*.py'").split('\n').filter(Boolean);

console.log('╔══════════════════════════════════════════════════╗');
console.log('║         SentinalAI — Session Initialized         ║');
console.log('╚══════════════════════════════════════════════════╝');
console.log(`  Branch      : ${branch}`);
console.log(`  Last commit : ${lastCommit}`);
console.log(`  Test files  : ${testCount}`);
if (workerChanges.length > 0) {
  console.log(`  ⚠  Uncommitted changes in: ${workerChanges.join(', ')}`);
}

// --- Restore state after compaction ---
const stateFile = path.join(repoRoot, '.claude', 'session-state.json');
if (fs.existsSync(stateFile)) {
  try {
    const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
    console.log(`\n  Restoring session state from compaction at ${state.compacted_at}`);
    if (state.modified_py?.length) {
      console.log(`  Modified files: ${state.modified_py.join(', ')}`);
    }
    if (state.open_tasks?.length) {
      console.log(`  Open tasks: ${state.open_tasks.join(', ')}`);
    }
  } catch { /* non-critical */ }
}

// --- Package manager detection ---
const pkgManagers = [
  ['pnpm-lock.yaml', 'pnpm'],
  ['yarn.lock',      'yarn'],
  ['bun.lockb',      'bun'],
  ['package-lock.json', 'npm'],
];
for (const [lockFile, name] of pkgManagers) {
  if (fs.existsSync(path.join(repoRoot, lockFile))) {
    console.log(`\n  Package manager: ${name}`);
    break;
  }
}

console.log('');
process.exit(0);
