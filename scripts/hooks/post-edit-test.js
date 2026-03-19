#!/usr/bin/env node
/**
 * PostToolUse / Write+Edit — auto-test runner
 *
 * After Claude writes or edits a file, this hook checks whether a
 * corresponding test file exists and, if so, runs it with pytest.
 *
 * Mapping rules (first match wins):
 *   workers/foo_worker.py       → tests/test_foo_worker.py
 *   supervisor/bar.py           → tests/test_bar.py
 *   tests/test_*.py             → run the test itself
 *
 * Only runs when the changed file is a .py file.
 * Runs asynchronously so it doesn't block Claude's response.
 */

const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const readline = require('readline');

async function main() {
  const rl = readline.createInterface({ input: process.stdin, terminal: false });
  const lines = [];
  for await (const line of rl) lines.push(line);
  const raw = lines.join('\n');

  let toolCall = {};
  try { toolCall = JSON.parse(raw); } catch { process.exit(0); }

  const filePath = (
    toolCall?.tool_input?.file_path ||
    toolCall?.input?.file_path ||
    ''
  ).toString();

  if (!filePath.endsWith('.py')) process.exit(0);

  const repoRoot = path.resolve(__dirname, '../..');
  const rel = path.relative(repoRoot, filePath);

  let testFile = null;

  if (rel.startsWith('tests/')) {
    testFile = filePath;
  } else {
    const base = path.basename(filePath, '.py');
    const candidates = [
      path.join(repoRoot, 'tests', `test_${base}.py`),
      path.join(repoRoot, 'tests', `test_${base.replace('_worker', '')}.py`),
    ];
    testFile = candidates.find(f => fs.existsSync(f)) || null;
  }

  if (!testFile) {
    console.log(`[sentinalai-hook] No test file found for ${rel}, skipping auto-test.`);
    process.exit(0);
  }

  console.log(`[sentinalai-hook] Auto-running tests: ${path.relative(repoRoot, testFile)}`);

  try {
    execSync(
      `python -m pytest "${testFile}" -q --tb=short --no-header 2>&1 | tail -20`,
      { cwd: repoRoot, stdio: 'inherit', timeout: 60_000, shell: true },
    );
    console.log('[sentinalai-hook] Tests passed.');
  } catch {
    console.error('[sentinalai-hook] Tests FAILED — review output above before continuing.');
  }

  process.exit(0);
}

main().catch(() => process.exit(0));
