// Run from the handoff root using a current Node.js runtime with built-in fetch:
// node examples/node-client.mjs
// Set the SAME DALEEL_API_TOKEN used by the Python service in this process too.
import { readFile } from 'node:fs/promises';

const base = (process.env.DALEEL_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const token = process.env.DALEEL_API_TOKEN;
if (!token) throw new Error('Set DALEEL_API_TOKEN');

async function predict(route, filename) {
  const body = await readFile(new URL(filename, import.meta.url), 'utf8');
  const response = await fetch(`${base}${route}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body,
    signal: AbortSignal.timeout(120_000),
  });
  const text = await response.text();
  const value = response.headers.get('content-type')?.includes('application/json')
    ? JSON.parse(text) : { message: text };
  if (!response.ok) {
    throw new Error(`Daleel HTTP ${response.status}; request=${response.headers.get('x-request-id')}; ${JSON.stringify(value)}`);
  }
  if (value.count !== value.predictions.length) throw new Error('Unexpected result count');
  return value;
}

console.log(await predict('/v1/transactions/predict', './transactions.request.json'));
console.log(await predict('/v1/ledger/predict', './ledger.request.json'));
