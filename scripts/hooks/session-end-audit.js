#!/usr/bin/env node
/**
 * SessionEnd hook — audit trail
 *
 * Appends a final session summary to .claude/audit.jsonl including:
 *   - Files changed this session (git diff vs HEAD)
 *   - Whether tests were run (presence of .pytest_cache mtime)
 *   - Final git branch + HEAD commit
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const repoRoot = path.resolve(__dirname, '../..');
const auditFile = path.join(repoRoot, '.claude', 'audit.jsonl');

function safeExec(cmd) {
  try {
    return execSync(cmd, { cwd: repoRoot, encoding: 'utf8', timeout: 5000 }).trim();
  } catch { return ''; }
}

const entry = {
  session_end: new Date().toISOString(),
  branch: safeExec('git rev-parse --abbrev-ref HEAD'),
  head_commit: safeExec('git log --oneline -1'),
  files_changed_vs_head: safeExec('git diff --name-only HEAD').split('\n').filter(Boolean),
  staged_for_commit: safeExec('git diff --cached --name-only').split('\n').filter(Boolean),
  tests_cache_modified: (() => {
    const cacheDir = path.join(repoRoot, '.pytest_cache');
    if (!fs.existsSync(cacheDir)) return null;
    return fs.statSync(cacheDir).mtime.toISOString();
  })(),
};

try {
  fs.mkdirSync(path.dirname(auditFile), { recursive: true });
  fs.appendFileSync(auditFile, JSON.stringify(entry) + '\n');
  console.log(`[sentinalai-hook] Session audit written to .claude/audit.jsonl`);
} catch { /* non-critical */ }

process.exit(0);
