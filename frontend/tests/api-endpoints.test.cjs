/* eslint-disable @typescript-eslint/no-require-imports -- CommonJS harness tests the actual TypeScript endpoint helper. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/lib/api.ts'), 'utf8');
const handler = source.match(/export function productionSocketUrl[\s\S]*?\n\}/)?.[0];
assert.ok(handler, 'test the actual endpoint helper, not a separate example');
const compiled = ts.transpileModule(handler, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText;
const context = {exports: {}, URL};
vm.runInNewContext(compiled, context);
const {productionSocketUrl} = context.exports;
test('tailnet HTTPS browser uses same-origin WSS, not client localhost', () => {
  assert.equal(productionSocketUrl('/factory-api', '/ws/production', 'https://factory.example.ts.net/dashboard'), 'wss://factory.example.ts.net/factory-api/ws/production');
});
test('local acceptance websocket preserves same-origin prefix and task', () => {
  assert.equal(productionSocketUrl('/factory-api/', '/ws/tasks/tsk-123', 'http://127.0.0.1:3040/tasks/tsk-123'), 'ws://127.0.0.1:3040/factory-api/ws/tasks/tsk-123');
});
test('existing explicit Mac API origin remains compatible', () => {
  assert.equal(productionSocketUrl('http://127.0.0.1:8000', '/ws/production', 'http://127.0.0.1:3000/'), 'ws://127.0.0.1:8000/ws/production');
});
test('app shell health uses the shared API base without a client-localhost fallback', () => {
  const shell = fs.readFileSync(path.join(__dirname, '../src/components/app-shell.tsx'), 'utf8');
  assert.match(shell, /import \{ API_URL,/);
  assert.ok(shell.includes('fetch(`${API_URL}/api/health`'));
  assert.doesNotMatch(shell, /NEXT_PUBLIC_API_URL|https?:\/\/(?:localhost|127\.0\.0\.1):8000/);
});
