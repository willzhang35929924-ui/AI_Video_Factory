// UNIT_ONLY frontend identity/selection contracts; no production task or paid request.
/* eslint-disable @typescript-eslint/no-require-imports -- CommonJS harness loads the actual application TSX. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

function load(filename, mocks) {
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "..", filename), "utf8"), {
    fileName: filename, compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
  const loadedModule = { exports: {} };
  new Function("require", "module", "exports", code)(name => Object.hasOwn(mocks, name) ? mocks[name] : require(name), loadedModule, loadedModule.exports);
  return loadedModule.exports;
}
const empty = () => null;
const children = props => React.createElement("div", null, props.children, props.action);
const link = props => React.createElement("a", { href: props.href }, props.children);
const task = (id, status, identity) => ({ id, country: "MX", keyword: "UNIT_ONLY", status,
  context: { task_context: { video_id: "12345" }, ...(identity ? { collection_identity: identity } : {}) },
  collector_result: { video_id: "12345", decision: "manual_review" } });
const identity = { status: "DUPLICATE_SKIPPED", canonical_task_id: "UNIT_CANONICAL", source_identity_key: "tiktok:video:12345" };
const duplicate = task("UNIT_ALIAS", "WAITING_REVIEW", identity);
const canonical = task("UNIT_CANONICAL", "WAITING_REVIEW", { ...identity, status: "CANONICAL", duplicate_encounters: 4 });
const requests = [];
const baseMocks = {
  react: { ...React, useState: value => [value, () => {}], useEffect: () => {}, useRef: () => ({ current: null }) },
  "next/link": link,
  "lucide-react": Object.fromEntries(["Check", "Copy", "ExternalLink", "RefreshCw", "X", "FileSpreadsheet", "ImagePlus", "Pause", "Play", "Plus", "Save", "Square", "Trash2"].map(name => [name, empty])),
  "@/components/ui/button": { Button: props => React.createElement("button", { disabled: props.disabled }, props.children) },
  "@/components/ui/input": { Input: empty },
  "@/components/ui/textarea": { Textarea: empty },
  "@/components/ui/dialog": Object.fromEntries(["Dialog", "DialogContent", "DialogDescription", "DialogHeader", "DialogTitle"].map(name => [name, empty])),
  "@/components/factory-ui": { Panel: children, Field: children, EmptyState: children, StatusBadge: props => React.createElement("span", null, props.status) },
  "@/components/providers": { useNotice: () => () => {} },
  "@/lib/types": { timeLabel: () => "UNIT_ONLY" },
  "@/lib/api": { request: async (...args) => { requests.push(args); return {}; } },
  "@tanstack/react-query": { useQueryClient: () => ({ invalidateQueries: async () => {} }) },
};
const candidates = load("src/components/collector-candidate-review.tsx", baseMocks);

test("duplicate aliases keep their historical status but cannot create another candidate review", () => {
  assert.equal(candidates.collectorDuplicate(duplicate), true);
  assert.equal(candidates.candidateState(duplicate), "DUPLICATE_SKIPPED");
  assert.equal(candidates.candidateReviewable(duplicate), false);
  assert.equal(duplicate.status, "WAITING_REVIEW", "UI does not rewrite historical Task state");
  assert.equal(candidates.candidateReviewable(canonical), true);
  assert.equal(candidates.candidateState(canonical), "WAITING_REVIEW");
});
test("ordinary skipped tasks are not classified as duplicate without identity evidence", () => {
  const ordinary = task("UNIT_SKIPPED", "SKIPPED");
  assert.equal(candidates.collectorDuplicate(ordinary), false);
  assert.equal(candidates.candidateState(ordinary), "SKIPPED");
});
test("duplicate notice points to canonical history and exposes the stable source key", () => {
  const html = renderToStaticMarkup(React.createElement(candidates.CollectorIdentityNotice, { task: duplicate }));
  assert.ok(html.includes('href="/tasks/UNIT_CANONICAL"'));
  assert.ok(html.includes(identity.source_identity_key));
  assert.ok(html.includes("不新增有效采集，不重复分析或审核"));
  assert.equal(requests.length, 0);
});
test("canonical encounter count is displayed without inventing a decision", () => {
  const html = renderToStaticMarkup(React.createElement(candidates.CollectorIdentityNotice, { task: canonical }));
  assert.ok(html.includes("重复遇见 4 次"));
  assert.ok(!html.includes("已通过"));
  const unknown = renderToStaticMarkup(React.createElement(candidates.CollectorIdentityNotice, { task: task("UNIT_NO_IDENTITY", "CREATED") }));
  assert.equal(unknown, "");
});
test("alias drawer links to existing Task without issuing candidate API or approval", () => {
  const html = renderToStaticMarkup(React.createElement(candidates.CollectorCandidateDrawer, { task: duplicate }));
  assert.ok(html.includes('href="/tasks/UNIT_CANONICAL"'));
  assert.ok(html.includes("查看历史采集"));
  assert.ok(!html.includes("同意采集"));
  assert.equal(requests.length, 0);
});

function elements(value, result = []) {
  if (Array.isArray(value)) value.forEach(item => elements(item, result));
  else if (React.isValidElement(value)) { result.push(value); elements(value.props.children, result); }
  return result;
}
function text(value) {
  if (Array.isArray(value)) return value.map(text).join("");
  if (typeof value === "string" || typeof value === "number") return String(value);
  return React.isValidElement(value) ? text(value.props.children) : "";
}
function boardHarness(item) {
  const values = [];
  values[11] = [item.id];
  values[12] = [item.id];
  let cursor = 0;
  const board = { items: [item], runs: [], total: 2, matching_total: 2, effective_total: 1,
    duplicate_total: 1, duplicate_encounters: 4, page: 1, page_size: 10, total_pages: 1,
    categories: [{ status: "DUPLICATE_SKIPPED", label: "历史重复", count: 1 }],
    filters: { countries: ["MX"], project_ids: [], platforms: ["TikTok"] } };
  const calls = [];
  const mocks = { ...baseMocks,
    react: { ...baseMocks.react, useState: initial => { const index = cursor++; if (!(index in values)) values[index] = initial;
      return [values[index], next => { values[index] = typeof next === "function" ? next(values[index]) : next; }]; } },
    "@tanstack/react-query": { ...baseMocks["@tanstack/react-query"],
      useQuery: () => ({ data: board, isFetching: false, isPending: false, isError: false, refetch: async () => {} }),
      useMutation: () => ({ isPending: false, mutateAsync: async args => { calls.push(args); return {}; } }) },
    "@/components/production-ui": { ProductionReadinessPanel: empty, RunLogDrawer: empty },
    "@/components/cancelled-batch-restore": { CancelledBatchRestore: empty },
    "@/components/collector-unattended": { CollectorUnattendedPanel: empty, useUnattendedPolicy: () => ({ data: { enabled: true } }) },
    "@/components/collector-candidate-review": { ...candidates, CollectorCandidateDrawer: empty, CollectorBatchReview: empty },
    "@/lib/api": { ...baseMocks["@/lib/api"], useCollectorRules: () => ({ data: { rules: {} } }),
      useProductionQueue: () => ({ data: { status: "PAUSED", batch_id: "UNIT_PAUSED" }, isError: false }),
      useProductionReadiness: () => ({ data: { ready: true, checks: [] }, isError: false }),
      useProductionEvents: () => {}, useFactory: () => ({ data: {} }) },
  };
  const { IntegrationCollector } = load("src/components/integration-collector.tsx", mocks);
  const tree = IntegrationCollector();
  return { tree, rows: elements(tree), calls, values };
}
test("created selection cannot re-enable migrated duplicates for collection or production", () => {
  const h = boardHarness(task("UNIT_ALIAS", "CREATED", identity));
  for (const label of ["开始采集", "开始生产"]) {
    const button = h.rows.find(row => typeof row.props.onClick === "function" && text(row.props.children).startsWith(label));
    assert.ok(button, label);
    assert.equal(button.props.disabled, true, label);
  }
  const checkbox = h.rows.find(row => row.props["aria-label"] === "选择 UNIT_ONLY MX UNIT_ALIAS");
  assert.equal(checkbox.props.disabled, true);
  assert.equal(checkbox.props.checked, false, "historical aliases are never displayed as selected for production");
  assert.equal(h.calls.length, 0);
});
test("board displays effective totals separately from duplicate history and canonical links", () => {
  const h = boardHarness(duplicate);
  const stats = h.rows.find(row => row.props["aria-label"] === "历史查重统计");
  assert.ok(stats);
  assert.match(text(stats.props.children), /有效采集 1 项 · 历史重复 1 项 · 重复遇见 4 次/);
  assert.ok(h.rows.some(row => row.type === candidates.CollectorIdentityNotice && row.props.task === duplicate));
  assert.equal(h.calls.length, 0);
});
