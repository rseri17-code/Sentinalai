#!/usr/bin/env node
/**
 * PreToolUse / Bash guard
 *
 * Reads the pending tool call from stdin (Claude Code hook protocol) and
 * warns — or blocks — if the command contains patterns that should not run
 * automatically during an incident analysis session:
 *   - Destructive filesystem ops  (rm -rf, truncate)
 *   - Force-push / hard-reset git ops
 *   - Direct writes to production databases
 *   - curl/wget piped directly to bash (supply-chain risk)
 *
 * Exit codes:
 *   0  — allow the command
 *   2  — block the command (Claude Code will surface the reason to the user)
 */

const readline = require('readline');

const BLOCKED_PATTERNS = [
  { re: /rm\s+-[a-zA-Z]*r[a-zA-Z]*f/,  reason: 'Destructive: rm -rf detected' },
  { re: /git\s+push\s+.*--force/,       reason: 'Destructive: force-push detected' },
  { re: /git\s+reset\s+--hard/,         reason: 'Destructive: git reset --hard detected' },
  { re: /DROP\s+TABLE/i,                reason: 'Dangerous: DROP TABLE detected' },
  { re: /DELETE\s+FROM/i,               reason: 'Dangerous: unguarded DELETE FROM detected' },
  { re: /curl\s+.*\|\s*(ba)?sh/,        reason: 'Supply-chain risk: curl | sh detected' },
  { re: /wget\s+.*\|\s*(ba)?sh/,        reason: 'Supply-chain risk: wget | sh detected' },
  { re: /truncate\s+--size\s+0/,        reason: 'Destructive: truncate --size 0 detected' },
];

async function main() {
  const rl = readline.createInterface({ input: process.stdin, terminal: false });
  const lines = [];
  for await (const line of rl) lines.push(line);
  const raw = lines.join('\n');

  let toolCall = {};
  try { toolCall = JSON.parse(raw); } catch { /* no JSON, allow */ process.exit(0); }

  const command = (toolCall?.tool_input?.command || toolCall?.input?.command || '').toString();
  if (!command) process.exit(0);

  for (const { re, reason } of BLOCKED_PATTERNS) {
    if (re.test(command)) {
      console.error(`[sentinalai-hook] BLOCKED: ${reason}`);
      console.error(`[sentinalai-hook] Command: ${command.slice(0, 120)}`);
      process.exit(2);
    }
  }

  process.exit(0);
}

main().catch(() => process.exit(0)); // never block on hook error
