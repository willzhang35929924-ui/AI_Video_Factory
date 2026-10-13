// UNIT_ONLY component/API contracts; no browser, real backend, model, or cloud document.
/* eslint-disable @typescript-eslint/no-require-imports -- CommonJS harness transpiles application TSX. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const identity = { product_id: "UNIT_TARGET", name: "UNIT_PRODUCT", country: "MX", keyword: "UNIT_EXACT_KEYWORD",
  product_image_sha256: "1".repeat(64), input_sha256: "2".repeat(64), pool_sha256: "3".repeat(64), source_context: {} };
const texts = Array.from({ length: 20 }, (_, i) => ({ id: String(i + 1).padStart(2, "0"), text: "UNIT_ONLY " + i, weight: 1, enabled: true }));
const source = { ...identity, product_id: "UNIT_SOURCE", input_sha256: "4".repeat(64), pool_sha256: "5".repeat(64),
  source_context: { source_document: "UNIT_SOURCE_DOC", source_row: 3 }, content_review: "REQUIRED", descriptions: texts };

function harness({ selected = "", confirmed = false, hasDescriptions = false, queryError, postError } = {}) {
  const buttons = [], calls = [], updates = [], mutations = [], invalidations = [], queries = [];
  let state = 0;
  const values = [selected, confirmed, false, "", ""];
  const mocks = {
    react: { ...React, useState: () => { const index = state++; return [values[index], value => mutations.push({ index, value })]; } },
    "@tanstack/react-query": { useQuery: options => { queries.push(options); return { data: queryError ? undefined : { target: identity, sources: [source], target_has_descriptions: hasDescriptions },
      error: queryError, isPending: false, refetch: async () => {} }; }, useQueryClient: () => ({ invalidateQueries: async options => invalidations.push(options) }) },
    "@/lib/api": { request: async (url, options) => { calls.push({ url, options }); if (options?.method === "POST" && postError) throw new Error(postError);
      return options?.method === "POST" ? { product: { descriptions: texts }, reused: false } : { target: identity, sources: [source], target_has_descriptions: false }; } },
    "@/components/ui/button": { Button: props => { buttons.push(props); return React.createElement("button", { disabled: props.disabled }, props.children); } },
    "@/components/factory-ui": { Field: props => React.createElement("label", null, props.label, props.children) },
  };
  const file = "src/components/description-reuse.tsx";
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "..", file), "utf8"), { fileName: file, compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  } }).outputText;
  const loaded = { exports: {} };
  new Function("require", "module", "exports", code)(name => Object.hasOwn(mocks, name) ? mocks[name] : require(name), loaded, loaded.exports);
  const html = renderToStaticMarkup(React.createElement(loaded.exports.DescriptionReuse, { productId: "UNIT_TARGET", onReady: rows => updates.push(rows) }));
  return { html, calls, updates, mutations, invalidations, queries, button: name => buttons.find(row => row.children === name) };
}

test("matching source is never selected automatically and initial render makes no write", async () => {
  const h = harness();
  assert.equal(h.button("复用20条已有文案").disabled, true);
  assert.match(h.html, /请选择来源，不自动匹配/);
  assert.match(h.html, /当前已保存目标/);
  assert.deepEqual(h.calls, []);
  await h.queries[0].queryFn();
  assert.equal(h.calls[0].url, "/api/publishing/products/UNIT_TARGET/descriptions/reuse-options");
  assert.equal(h.calls[0].options, undefined);
});

test("source preview exposes real hashes and never inherits a previous fact approval", () => {
  const h = harness({ selected: "UNIT_SOURCE" });
  assert.equal(h.button("复用20条已有文案").disabled, true);
  for (const value of [source.pool_sha256, source.product_image_sha256, "查看20条已有文案", "不会继承", "确认复用所选文案池"])
    assert.ok(h.html.includes(value), value);
  assert.equal(h.calls.length, 0);
});

test("confirmed source calls real API contract with immutable source and target fingerprints", async () => {
  const h = harness({ selected: "UNIT_SOURCE", confirmed: true });
  assert.equal(h.button("复用20条已有文案").disabled, false);
  await h.button("复用20条已有文案").onClick();
  assert.equal(h.calls[0].url, "/api/publishing/products/UNIT_TARGET/descriptions/reuse");
  assert.equal(h.calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { source_product_id: "UNIT_SOURCE", source_pool_sha256: source.pool_sha256,
    source_input_sha256: source.input_sha256, target_input_sha256: identity.input_sha256, confirmed: true });
  assert.deepEqual(h.updates, [texts]);
  assert.deepEqual(h.invalidations, [{ queryKey: ["publishing"] }]);
  assert.ok(h.mutations.some(row => row.index === 4 && row.value.includes("未调用AI")));
});

test("reuse failure does not change parent editor or claim persisted success", async () => {
  const h = harness({ selected: "UNIT_SOURCE", confirmed: true, postError: "DESCRIPTION_REUSE_TARGET_CHANGED" });
  await h.button("复用20条已有文案").onClick();
  assert.deepEqual(h.updates, []);
  assert.deepEqual(h.invalidations, []);
  assert.ok(h.mutations.some(row => row.index === 3 && row.value === "DESCRIPTION_REUSE_TARGET_CHANGED"));
  assert.ok(!h.mutations.some(row => row.index === 4 && row.value.includes("已保存")));
});

test("existing saved target descriptions cannot be overwritten from this area", () => {
  const h = harness({ hasDescriptions: true });
  assert.equal(h.button("复用20条已有文案"), undefined);
  assert.match(h.html, /本产品已有文案/);
  assert.equal(h.calls.length, 0);
});

test("backend identity error is surfaced without fallback pool", () => {
  const h = harness({ queryError: new Error("DESCRIPTION_REUSE_PRODUCT_IMAGE_REQUIRED") });
  assert.match(h.html, /DESCRIPTION_REUSE_PRODUCT_IMAGE_REQUIRED/);
  assert.equal(h.button("复用20条已有文案"), undefined);
  assert.equal(h.calls.length, 0);
});
