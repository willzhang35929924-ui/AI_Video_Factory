// UNIT_ONLY component/API contracts; no browser, real backend or media deletion.
/* eslint-disable @typescript-eslint/no-require-imports -- CommonJS harness transpiles application TSX. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const asset = (id, type) => ({ id, task_id: "UNIT_TASK", type, name: "UNIT_ONLY_" + id, status: "READY",
  registry_status: "registered", storage_path: "/UNIT_ONLY/" + id, country: "MX", keyword: "UNIT_KEYWORD", size: 100 });
const root = asset("UNIT_PERSON", "person"), linked = asset("UNIT_COMPOSE", "compose"), final = asset("UNIT_FINAL", "final");
const plan = { requested_asset_ids: [root.id], expanded_asset_ids: [root.id, linked.id, final.id], assets: [root, linked, final],
  edges: [{ from_asset_id: root.id, to_asset_id: linked.id, reason: "PERSON_INPUT" }],
  references: [{ type: "ProcessingRun", id: "UNIT_RUN", reason: "REFERENCED" }, { type: "Review", id: "UNIT_REVIEW" }],
  records: [{ table: "processing_runs", id: "UNIT_RUN", action: "DELETE", reason: "EXCLUSIVE_FINISHED_OUTPUTS" },
    { table: "reviews", id: "UNIT_REVIEW", action: "DELETE", reason: "EXACT_ASSET_OWNERSHIP" }],
  record_actions: { "processing_runs:DELETE": 1, "reviews:DELETE": 1 }, affected_task_ids: ["UNIT_TASK"],
  remaining_external_media: [{ asset_id: "UNIT_EXTERNAL", path: "/UNIT_ONLY/LEGACY_READ_ONLY.mp4", reason: "READONLY_EXTERNAL_ANCESTOR" }],
  published_anchor_ids: [],
  files: [{ path: root.storage_path, size: 100, allocated_bytes: 4096, exists: true, shared: false, link_count: 1, action: "DELETE" },
    { path: "/UNIT_ONLY/shared.png", size: 20, allocated_bytes: 4096, exists: true, shared: true, link_count: 1, action: "KEEP_SHARED" }],
  file_count: 1, bytes_to_release: 100, allocated_bytes_reclaim_estimate: 4096, retained_shared_bytes: 20,
  can_delete: true, reasons: [], warnings: ["HISTORY_VERSION"], requires_impact_confirmation: true, fingerprint: "a".repeat(64) };

function load(filename, mocks) {
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "..", filename), "utf8"), { fileName: filename,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText;
  const loaded = { exports: {} };
  new Function("require", "module", "exports", code)(name => Object.hasOwn(mocks, name) ? mocks[name] : require(name), loaded, loaded.exports);
  return loaded.exports;
}
const children = props => React.createElement("div", null, props.children);
const empty = () => null;
function elements(value, rows = []) {
  if (Array.isArray(value)) value.forEach(item => elements(item, rows));
  else if (React.isValidElement(value)) { rows.push(value); elements(value.props.children, rows); }
  return rows;
}
function harness({ page = false, selected = [], family = plan, confirmed = "", requestError, requestResponse, queryError, fetching = false } = {}) {
  const calls = [], queries = [], invalidations = [], callbacks = [], mutations = [];
  const values = page ? ["production", "all", "", selected, [], []] : [true, confirmed, false, "", [], [root]];
  let cursor = 0, refetches = 0;
  const mocks = {
    react: { ...React, useState: initial => { const index = cursor++; if (!(index in values)) values[index] = initial;
      return [values[index], update => { values[index] = typeof update === "function" ? update(values[index]) : update; mutations.push({ index, value: values[index] }); }]; } },
    "next/link": children,
    "lucide-react": { Trash2: empty, RefreshCw: empty, Search: empty },
    "@/components/ui/button": { Button: children }, "@/components/ui/input": { Input: children },
    "@/components/ui/dialog": Object.fromEntries(["Dialog", "DialogContent", "DialogDescription", "DialogHeader", "DialogTitle"].map(name => [name, children])),
    "@/components/factory-ui": Object.fromEntries(["PageHeading", "Panel", "StatusBadge", "EmptyState"].map(name => [name, children])),
    "@/lib/types": { durationLabel: () => "UNIT_DURATION" },
    "@/lib/api": { API_URL: "http://UNIT_ONLY.invalid", request: async (url, options) => { calls.push({ url, options });
      if (requestError) throw requestError;
      return url.endsWith("family-delete") ? requestResponse || { status: "DELETED", record_deleted: true, deleted_asset_ids: family.expanded_asset_ids,
        released_bytes: 100, deleted_allocated_bytes: 4096, retained_shared_bytes: 20, cleanup_errors: [],
        original_files_absent: true, shared_files_preserved: true, audit_id: "UNIT_DELETE_AUDIT" } : family; } },
    "@tanstack/react-query": { useQuery: options => { queries.push(options);
      const name = options.queryKey[0];
      const data = name === "asset-family-deletion-plan" ? options.enabled ? family : undefined
        : name === "asset-registry" ? [root, linked, final]
        : name === "asset-media-status" ? [] : name === "asset-deletion-status" ? [] : undefined;
      return { data, isFetching: name === "asset-family-deletion-plan" && fetching, isPending: false,
        isError: name === "asset-family-deletion-plan" && !!queryError, error: queryError, refetch: async () => { refetches++; } }; },
      useQueryClient: () => ({ invalidateQueries: async key => invalidations.push(key) }) },
  };
  const component = load("src/components/asset-deletion.tsx", mocks);
  mocks["@/components/asset-deletion"] = { ...component, AssetDeletionDialog: empty, AssetFamilyDeletionPreview: empty };
  const target = page ? load("src/app/assets/page.tsx", mocks).default : component.AssetDeletionDialog;
  const h = { calls, queries, callbacks, mutations, values, component, invalidations,
    render() { cursor = 0; h.tree = target({ assets: [root], onDeleted: rows => callbacks.push(rows) }); h.elements = elements(h.tree); return h; },
    button(text) { return h.elements.find(row => typeof row.props.onClick === "function" && row.props.children === text); },
    input(label) { return h.elements.find(row => row.props["aria-label"] === label); },
    get refetches() { return refetches; } };
  return h.render();
}

test("selection starts the real family-plan request and automatically checks linked media", async () => {
  const h = harness({ page: true });
  assert.equal(h.queries.find(query => query.queryKey[0] === "asset-family-deletion-plan").enabled, false);
  h.input("选择素材 " + root.id).props.onChange({ target: { checked: true } });
  h.render();
  const query = h.queries.at(-1);
  assert.equal(query.queryKey[0], "asset-family-deletion-plan");
  assert.equal(query.enabled, true);
  await query.queryFn();
  assert.equal(h.calls[0].url, "/api/assets/family-deletion-plan");
  assert.equal(h.calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { asset_ids: [root.id], scope: "production" });
  for (const item of [root, linked, final]) assert.equal(h.input("选择素材 " + item.id).props.checked, true);
  assert.equal(h.input("选择素材 " + linked.id).props.disabled, true);
  assert.equal(h.input("选择素材 " + root.id).props.disabled, false);
  h.input("选择素材 " + root.id).props.onChange({ target: { checked: false } });
  h.render();
  assert.equal(h.input("选择素材 " + linked.id).props.checked, false);
  assert.equal(h.calls.length, 1, "selection never performs deletion");
});

test("family preview exposes every ID, file, count, reference and shared-file protection", () => {
  const h = harness();
  const html = renderToStaticMarkup(React.createElement(h.component.AssetFamilyDeletionPreview, { plan }));
  for (const text of [root.id, linked.id, final.id, root.storage_path, "UNIT_RUN", "UNIT_REVIEW", "保留共享文件", "共 3 项", "待删除文件 1 个", "预计释放文件块", "关联记录删除 / 保留明细", "删除记录", "受影响任务将取消后续自动生产", "/UNIT_ONLY/LEGACY_READ_ONLY.mp4"]) assert.ok(html.includes(text), text);
  assert.ok(html.includes('open=""'), "linked IDs are expanded immediately, not hidden behind another operation");
  assert.equal(h.calls.length, 0);
});

test("family shared-file warning reflects the actual plan while individual protection semantics remain unchanged", () => {
  const h = harness();
  const included = { ...plan, warnings: ["SHARED_FILE"], retained_shared_bytes: 0,
    files: plan.files.map(file => ({ ...file, action: "DELETE", shared: false })) };
  const includedHtml = renderToStaticMarkup(React.createElement(h.component.AssetFamilyDeletionPreview, { plan: included }));
  assert.ok(includedHtml.includes("共享依赖已联动纳入本次删除"));
  assert.ok(!includedHtml.includes("仅删除选中素材记录与专属文件"));
  const retainedHtml = renderToStaticMarkup(React.createElement(h.component.AssetFamilyDeletionPreview, { plan: { ...plan, warnings: ["SHARED_FILE"] } }));
  assert.ok(retainedHtml.includes("共享文件保留 · 仅删除选中素材记录与专属文件"));
  assert.equal(h.component.deletionLabel("SHARED_FILE"), "共享文件保留 · 仅删除选中素材记录与专属文件");
});

test("confirmed external registered media displays its exact path and hash before deletion", () => {
  const h = harness();
  const external = { ...plan, files: [{ ...plan.files[0], external_registered_media: true, sha256: "c".repeat(64) }] };
  const html = renderToStaticMarkup(React.createElement(h.component.AssetFamilyDeletionPreview, { plan: external }));
  assert.ok(html.includes("外部已登记原片 · 将真实删除"));
  assert.ok(html.includes(root.storage_path));
  assert.ok(html.includes("c".repeat(64)));
  assert.equal(h.calls.length, 0, "preview does not delete any file");
});

test("audited orphan previews expose their exact historical lineage without creating asset records", () => {
  const h = harness();
  const orphan = { source_asset_id: root.id, deleted_person_asset_id: "UNIT_DELETED_PERSON", deletion_audit_id: "UNIT_PRIOR_DELETE",
    processing_run_id: "UNIT_ORPHAN_RUN", output_path: "/UNIT_ONLY/orphan/Preview.mp4", output_sha256: "d".repeat(64),
    manifest_path: "/UNIT_ONLY/orphan/manifest.json", directory: "/UNIT_ONLY/orphan", task_id: "UNIT_TASK" };
  const html = renderToStaticMarkup(React.createElement(h.component.AssetFamilyDeletionPreview, { plan: { ...plan, orphan_outputs: [orphan] } }));
  for (const text of ["历史已删版本的残留预览（1）", root.id, orphan.deleted_person_asset_id, orphan.deletion_audit_id,
    orphan.processing_run_id, orphan.output_path, orphan.output_sha256]) assert.ok(html.includes(text), text);
  assert.equal(h.calls.length, 0, "rendering lineage never deletes or re-registers an asset");
});

test("one explicit fingerprint confirmation deletes the family through one real API mutation", async () => {
  const h = harness({ confirmed: plan.fingerprint });
  assert.equal(h.input("确认永久删除媒体").props.checked, true);
  assert.equal(h.elements.filter(row => row.type === "input" && row.props.type === "checkbox").length, 1);
  const button = h.button("确认联动删除 3 项");
  assert.equal(button.props.disabled, false, "ordinary historical references do not disable deletion");
  await button.props.onClick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, "/api/assets/family-delete");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { asset_ids: [root.id], scope: "production", fingerprint: plan.fingerprint,
    confirmed: true, impact_confirmed: true });
  assert.deepEqual(h.callbacks[0][0].deleted_asset_ids, [root.id, linked.id, final.id]);
  assert.equal(h.callbacks[0][0].released_bytes, 100, "uses backend actual release, not card sizes");
  assert.ok(h.invalidations.some(key => key.queryKey[0] === "asset-storage-usage"));
  assert.equal(h.invalidations.find(key => key.queryKey[0] === "asset-family-deletion-plan").refetchType, "none", "deleted-root 404 cannot replace the successful receipt");
  assert.equal(h.values[1], "");
});

test("changed or unconfirmed plan cannot execute with a stale confirmation", async () => {
  for (const confirmed of ["", "b".repeat(64)]) {
    const h = harness({ confirmed });
    const button = h.button("确认联动删除 3 项");
    assert.equal(button.props.disabled, true);
    await button.props.onClick();
    assert.equal(h.calls.length, 0);
  }
});

test("active and protected-path families explain the block and cannot partially delete", () => {
  for (const reason of ["RUNNING", "EXTERNAL_LEGACY_FILE", "SYMLINK_FILE_PROTECTED"]) {
    const h = harness({ family: { ...plan, can_delete: false, reasons: [reason] }, confirmed: plan.fingerprint });
    assert.equal(h.button("确认联动删除 3 项"), undefined);
    const html = renderToStaticMarkup(React.createElement(h.component.AssetFamilyDeletionPreview, { plan: { ...plan, can_delete: false, reasons: [reason] } }));
    assert.ok(html.includes("当前无法删除"));
    assert.ok(html.includes(h.component.deletionLabel(reason)));
    assert.equal(h.calls.length, 0);
  }
});

test("delete failure does not retry a mutation and refreshes the read-only plan", async () => {
  const h = harness({ confirmed: plan.fingerprint, requestError: new Error("UNIT_FILE_IN_USE") });
  await h.button("确认联动删除 3 项").props.onClick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.refetches, 1);
  assert.equal(h.values[1], "");
  assert.equal(h.values[3], "UNIT_FILE_IN_USE");
  assert.deepEqual(h.callbacks, []);
  assert.equal(h.queries[0].retry, false);
});

test("successful family receipt remains visible when the deleted-root plan can no longer load", async () => {
  const h = harness({ confirmed: plan.fingerprint, queryError: new Error("UNIT_DELETED_ROOT_NOT_FOUND") });
  // The pre-delete rendered action was available; a post-delete observer may now return 404.
  const pending = harness({ confirmed: plan.fingerprint });
  await pending.button("确认联动删除 3 项").props.onClick();
  h.values[4] = pending.values[4];
  h.render();
  const html = renderToStaticMarkup(h.tree);
  assert.ok(html.includes("已删除 3 项素材记录"));
  assert.ok(html.includes("UNIT_DELETE_AUDIT"));
  assert.ok(html.includes("已核实不存在"));
  assert.ok(!html.includes("UNIT_DELETED_ROOT_NOT_FOUND"));
  assert.equal(h.calls.length, 0);
});

test("published receipt protection is reported as PARTIAL, not fake full deletion or failure", async () => {
  const family = { ...plan, published_anchor_ids: [final.id], records: [...plan.records,
    { table: "publishing_tasks", id: "UNIT_PUBLIC_POST", action: "KEEP_RECEIPT", reason: "PUBLICATION_RECEIPT_OR_UNKNOWN_EXTERNAL_RESULT" }] };
  const h = harness({ family, confirmed: plan.fingerprint, requestResponse: { status: "PARTIAL", record_deleted: true,
    deleted_asset_ids: family.expanded_asset_ids, released_bytes: 100, deleted_allocated_bytes: 4096, retained_shared_bytes: 20,
    original_files_absent: true, shared_files_preserved: true, published_anchor_ids: [final.id], audit_id: "UNIT_PARTIAL_AUDIT" } });
  const preview = renderToStaticMarkup(React.createElement(h.component.AssetFamilyDeletionPreview, { plan: family }));
  assert.ok(preview.includes("保留真实发布回执"));
  assert.ok(preview.includes("已发布内容不会删除"));
  await h.button("确认联动删除 3 项").props.onClick();
  h.render();
  const html = renderToStaticMarkup(h.tree);
  assert.ok(html.includes("PARTIAL：本地家族已删除"));
  assert.ok(!html.includes("删除失败"));
  assert.equal(h.calls.length, 1);
  assert.equal(h.callbacks[0][0].status, "PARTIAL");
});

test("cancel and reopen reset confirmation without deleting media", () => {
  const h = harness({ confirmed: plan.fingerprint });
  h.button("关闭").props.onClick();
  assert.equal(h.values[0], false);
  const opening = h.elements.find(row => row.props.onClick && Array.isArray(row.props.children) && row.props.children.at(-1) === "删除素材");
  opening.props.onClick();
  assert.equal(h.values[0], true);
  assert.equal(h.values[1], "");
  assert.equal(h.calls.length, 0);
});

test("plan roots are deduplicated and scope changes discard old family selections", async () => {
  const h = harness({ page: true, selected: [root.id] });
  await h.component.assetFamilyPlan([linked.id, root.id, linked.id], "history");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { asset_ids: [linked.id, root.id].sort(), scope: "history" });
  h.input("素材视图").props.onChange({ target: { value: "technical" } });
  assert.deepEqual(h.values[3], []);
  assert.equal(h.values[0], "technical");
});
