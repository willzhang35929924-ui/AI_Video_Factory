// UNIT_ONLY component/API contracts; no real browser, backend, or public request.
/* eslint-disable @typescript-eslint/no-require-imports -- CommonJS harness transpiles application TSX. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const React = require("react");

const task = { id: "UNIT_PUBLISH", task_id: "UNIT_TASK", account_id: "UNIT_ACCOUNT", product_id: "UNIT_PRODUCT",
  country: "MX", status: "WAITING_MANUAL", caption: "UNIT_ONLY", checkpoint: {} };
const checks = [
  { name: "vpn", status: "WAITING_MANUAL", reason: "NETWORK_PROBE_NOT_AUTHORIZED" },
  { name: "vpn", status: "WAITING_MANUAL", reason: "OBSERVED_NETWORK_COUNTRY_REQUIRED" },
];

function harness({ requestError, preflightError = "", checkpoint = task.checkpoint, taskRows = [task] } = {}) {
  const calls = [], mutations = [], timeouts = [], notices = [];
  const values = [true, false, "", "", "", "", [], false, { ...task, checkpoint }, checks, preflightError];
  let state = 0;
  const stub = () => null;
  const mocks = {
    react: { ...React, useState: () => { const index = state++; return [values[index], value => mutations.push({ index, value })]; } },
    "next/link": stub,
    "lucide-react": Object.fromEntries(["Plus", "RefreshCw", "Play", "Save", "FileText"].map(name => [name, stub])),
    "@tanstack/react-query": { useQuery: options => ({ data: options.queryKey[1] === "config" ? { product_selection_mode: "runtime_match" }
      : options.queryKey[1] === "tasks" ? taskRows : [], refetch: async () => {}, error: null }),
      useQueryClient: () => ({ invalidateQueries: async () => {} }) },
    "@/lib/api": { API_URL: "http://UNIT_ONLY.invalid", useFactory: () => ({ data: { assets: [] } }),
      request: async (url, options) => { calls.push({ url, options, mutationsAtRequest: [...mutations] });
        if (requestError) throw requestError;
        return { status: "WAITING_MANUAL", checks }; } },
    "@/components/providers": { useNotice: () => message => notices.push(message) },
    "@/components/ui/button": { Button: stub },
    "@/components/ui/input": { Input: stub },
    "@/components/ui/textarea": { Textarea: stub },
    "@/components/ui/dialog": Object.fromEntries(["Dialog", "DialogContent", "DialogHeader", "DialogTitle", "DialogDescription", "DialogFooter"].map(name => [name, stub])),
    "@/components/factory-ui": Object.fromEntries(["PageHeading", "Panel", "Field", "EmptyState", "StatusBadge", "SmallMetric", "Thumbnail"].map(name => [name, stub])),
    "@/components/description-generator": { DescriptionGenerator: stub },
    "@/components/publishing-submit-control": { PublishingSubmitControl: stub },
  };
  const filename = "src/components/publishing-control.tsx";
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "..", filename), "utf8"), { fileName: filename, compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  } }).outputText;
  const loaded = { exports: {} };
  new Function("require", "module", "exports", "AbortSignal", code)(name => Object.hasOwn(mocks, name) ? mocks[name] : require(name), loaded, loaded.exports,
    { timeout: milliseconds => { timeouts.push(milliseconds); return new AbortController().signal; } });
  const root = loaded.exports.PublishingConsole();
  const elements = [];
  function walk(value) {
    if (Array.isArray(value)) value.forEach(walk);
    else if (React.isValidElement(value)) {
      elements.push(value);
      walk(value.props.children);
      walk(value.props.action);
    }
  }
  walk(root);
  return { elements, calls, mutations, timeouts, notices };
}

test("completed counter includes verified publication only, never queued or unknown submission", () => {
  const taskRows = [
    { ...task, id: "UNIT_COMPLETE", status: "COMPLETED" },
    { ...task, id: "UNIT_PUBLISHED", status: "PUBLISHED", submission_state: "VERIFIED" },
    { ...task, id: "UNIT_UNVERIFIED", status: "PUBLISHED", submission_state: "SUBMITTED" },
    { ...task, id: "UNIT_UNKNOWN", status: "WAITING_MANUAL", submission_state: "UNKNOWN" },
    { ...task, id: "UNIT_SUBMITTED", status: "SUBMITTED", submission_state: "SUBMITTED" },
    { ...task, id: "UNIT_QUEUED", status: "QUEUED" },
    { ...task, id: "UNIT_RUNNING", status: "RUNNING" },
  ];
  const h = harness({ taskRows });
  assert.equal(h.elements.find(element => element.props.label === "完成").props.value, 2);
  assert.equal(h.elements.find(element => element.props.label === "运行中").props.value, 1);
  assert.equal(h.elements.find(element => element.props.label === "等待人工").props.value, 1);
  assert.equal(h.calls.length, 0, "statistics do not submit or reconcile a publication");
});

test("preflight retains duplicate-named adapter and service observations with unique React keys", () => {
  const h = harness();
  const rows = h.elements.filter(element => element.type === "div" && element.props.className === "border-b py-2 text-xs");
  assert.equal(rows.length, 2, "both actual observations must remain visible");
  assert.equal(new Set(rows.map(row => row.key)).size, 2);
  assert.deepEqual(rows.map(row => row.key), ["vpn-0", "vpn-1"]);
  for (let i = 0; i < rows.length; i++) {
    assert.equal(rows[i].props.children[0], "vpn");
    assert.equal(rows[i].props.children[2], "WAITING_MANUAL");
    assert.equal(rows[i].props.children[3].props.children, checks[i].reason);
  }
  assert.deepEqual(h.calls, [], "rendering must not perform a preflight or publish");
});

test("check conditions uses existing real preflight API contract and preserves the full response", async () => {
  const h = harness();
  const button = h.elements.find(element => element.props.children === "检查发布条件");
  assert.ok(button);
  assert.equal(button.props.disabled, false);
  await button.props.onClick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, "/api/publishing/tasks/UNIT_PUBLISH/preflight");
  assert.equal(h.calls[0].options.method, "GET");
  assert.equal(h.calls[0].options.body, undefined);
  assert.deepEqual(h.calls[0].mutationsAtRequest.find(row => row.index === 9).value, [], "previous observations must clear before network work starts");
  assert.deepEqual(h.mutations.filter(row => row.index === 9).map(row => row.value), [[], checks]);
  assert.deepEqual(h.timeouts, [120000], "preflight retains the existing explicit deadline");
  assert.ok(h.calls[0].options.signal instanceof AbortSignal);
  assert.ok(!h.calls.some(row => /confirm-submit|batches/.test(row.url)));
});

test("failed preflight clears stale checks and reports failure without retrying or claiming readiness", async () => {
  const h = harness({ requestError: new Error("UNIT_PREFLIGHT_TIMEOUT") });
  await h.elements.find(element => element.props.children === "检查发布条件").props.onClick();
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.mutations.filter(row => row.index === 9).map(row => row.value), [[]]);
  assert.deepEqual(h.notices, ["UNIT_PREFLIGHT_TIMEOUT"]);
  assert.deepEqual(h.timeouts, [120000]);
  assert.deepEqual(h.mutations.filter(row => row.index === 10).map(row => row.value), ["", "UNIT_PREFLIGHT_TIMEOUT"]);
  assert.equal(h.mutations.at(-1).index, 7);
  assert.equal(h.mutations.at(-1).value, false);
});

test("safe-stage retry mutation retains its existing deadline and does not alter preflight checks", async () => {
  const h = harness();
  await h.elements.find(element => element.props.children === "继续 / 重试安全步骤").props.onClick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, "/api/publishing/tasks/UNIT_PUBLISH/retry");
  assert.equal(h.calls[0].options.method, "POST");
  assert.ok(h.calls[0].options.signal instanceof AbortSignal);
  assert.deepEqual(h.timeouts, [120000], "mutation deadline must not change on an unverified timeout theory");
  assert.ok(!h.mutations.some(row => row.index === 9));
  assert.ok(!h.mutations.some(row => row.index === 10));
});

test("unknown mutation result is not automatically resubmitted or promoted to success", async () => {
  const h = harness({ requestError: new Error("UNIT_EXTERNAL_RESULT_UNKNOWN") });
  await h.elements.find(element => element.props.children === "继续 / 重试安全步骤").props.onClick();
  assert.equal(h.calls.length, 1);
  assert.ok(h.calls[0].options.signal instanceof AbortSignal);
  assert.deepEqual(h.timeouts, [120000]);
  assert.deepEqual(h.notices, ["UNIT_EXTERNAL_RESULT_UNKNOWN"]);
  assert.ok(!h.calls.some(row => /confirm-submit|batches/.test(row.url)));
});

test("latest failed preflight is visibly unconfirmed, not a successful old observation", () => {
  const h = harness({ preflightError: "UNIT_FAILED_TO_FETCH" });
  const alert = h.elements.find(element => element.type === "p" && element.props.role === "alert");
  assert.ok(alert);
  assert.deepEqual(alert.props.children, ["最新预检未确认：", "UNIT_FAILED_TO_FETCH", "。上次检查结果已清空；请重新检查，未启动或重复提交发布。"]);
  assert.equal(h.calls.length, 0);
});

test("unknown product attachment exposes only an explicit read-only reconciliation action", async () => {
  const h = harness({ checkpoint: { inflight_step: "ATTACH_PRODUCT", external_outcome: "UNKNOWN" } });
  const button = h.elements.find(element => element.props.children === "核实已挂商品（不重挂）");
  assert.ok(button);
  assert.equal(button.props.disabled, false);
  await button.props.onClick();
  assert.equal(h.calls[0].url, "/api/publishing/tasks/UNIT_PUBLISH/reconcile-product");
  assert.equal(h.calls[0].options.method, "POST");
  assert.equal(h.calls[1].url, "/api/publishing/tasks/UNIT_PUBLISH", "refreshes actual persisted task after observation");
  assert.ok(!h.calls.some(row => /confirm-submit|batches|retry|observe-product/.test(row.url)));
  assert.deepEqual(h.timeouts, [120000]);
  assert.ok(h.notices.at(-1).includes("仍需人工"), "WAITING_MANUAL must not produce confirmed success");
});

test("ordinary or unknown upload checkpoints do not expose product recovery", () => {
  for (const checkpoint of [{}, { inflight_step: "UPLOAD_VIDEO", external_outcome: "UNKNOWN" }, { inflight_step: "ATTACH_PRODUCT" }]) {
    const h = harness({ checkpoint });
    assert.ok(!h.elements.some(element => element.props.children === "核实已挂商品（不重挂）"));
    assert.equal(h.calls.length, 0, "render does not perform external observation automatically");
  }
});

test("product observation network failure never reattaches or retries the unknown request", async () => {
  const h = harness({ requestError: new Error("UNIT_READONLY_FAILED"), checkpoint: { inflight_step: "ATTACH_PRODUCT", external_outcome: "UNKNOWN" } });
  await h.elements.find(element => element.props.children === "核实已挂商品（不重挂）").props.onClick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, "/api/publishing/tasks/UNIT_PUBLISH/reconcile-product");
  assert.deepEqual(h.notices, ["UNIT_READONLY_FAILED"]);
  assert.ok(!h.calls.some(row => /confirm-submit|retry|batches/.test(row.url)));
});
