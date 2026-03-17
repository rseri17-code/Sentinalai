#!/usr/bin/env node
/**
 * PreCompact hook — investigation state summarizer
 *
 * Before Claude compacts the context, this hook writes a brief
 * summary of the current session state to .claude/session-state.json
 * so the next session (or post-compaction continuation) can reload it.
 *
 * Captures:
 *   - Timestamp of compaction
 *   - List of recently modified Python files (git status)
 *   - Last N lines of the pytest summary cache (if present)
 *   - Open TODOs from tasks/ directory
 */

const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const repoRoot = path.resolve(__dirname, '../..');
const stateFile = path.join(repoRoot, '.claude', 'session-state.json');

function safeExec(cmd) {
  try {
    return execSync(cmd, { cwd: repoRoot, encoding: 'utf8', timeout: 5000 }).trim();
  } catch {
    return '';
  }
}

const state = {
  compacted_at: new Date().toISOString(),
  git_status: safeExec('git status --short').split('\n').filter(Boolean).slice(0, 20),
  recent_commits: safeExec('git log --oneline -5').split('\n').filter(Boolean),
  modified_py: safeExec("git diff --name-only HEAD -- '*.py'").split('\n').filter(Boolean),
  open_tasks: (() => {
    const tasksDir = path.join(repoRoot, 'tasks');
    if (!fs.existsSync(tasksDir)) return [];
    return fs.readdirSync(tasksDir)
      .filter(f => f.endsWith('.md') || f.endsWith('.txt'))
      .slice(0, 5);
  })(),
  workers: [
    'ops_worker', 'log_worker', 'metrics_worker', 'apm_worker',
    'knowledge_worker', 'itsm_worker', 'devops_worker', 'confluence_worker',
  ],
  coverage_target: '80%',
  note: 'Reload this file at SessionStart to restore context after compaction.',
};

try {
  fs.mkdirSync(path.dirname(stateFile), { recursive: true });
  fs.writeFileSync(stateFile, JSON.stringify(state, null, 2));
  console.log(`[sentinalai-hook] Session state saved to .claude/session-state.json`);
} catch (e) {
  console.error(`[sentinalai-hook] Could not save session state: ${e.message}`);
}

process.exit(0);
