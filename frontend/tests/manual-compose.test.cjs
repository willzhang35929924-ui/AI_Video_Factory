// UNIT_ONLY component/API contracts; no browser, network, or production acceptance.
/* eslint-disable @typescript-eslint/no-require-imports -- Existing CommonJS harness transpiles application TSX. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

function loadSource(filename, mocks) {
  const source = fs.readFileSync(path.join(__dirname, "..", filename), "utf8");
  const code = ts.transpileModule(source, { fileName: filename, compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  } }).outputText;
  const loaded = { exports: {} };
  new Function("require", "module", "exports", code)(name => Object.hasOwn(mocks, name) ? mocks[name] : require(name), loaded, loaded.exports);
  return loaded.exports;
}

const children = props => React.createElement("div", null, props.children, props.action);
const empty = () => null;
function label(value) {
  if (Array.isArray(value)) return value.map(label).join("");
  if (typeof value === "string" || typeof value === "number") return String(value);
  return React.isValidElement(value) ? label(value.props.children) : "";
}
const asset = (id, type = "person", extra = {}) => ({ id, task_id: "unit-task", type, name: "UNIT_ONLY_" + id,
  registry_status: "registered", status: "READY", width: 32, height: 48, duration: 1, country: "MX", keyword: "unit-keyword",
  created_at: "2026-10-08T00:00:00Z",
  source_system: "UNIT_SOURCE", legacy_reference: { alpha_path: "/UNIT_ONLY/alpha.mkv", alpha_sha256: "unit-sha",
    manifest_path: "/UNIT_ONLY/manifest.json", alpha_mode: "straight" }, ...extra });
const current = asset("current-person"), background = asset("background", "background"), old = asset("historical-person");
const mediaRow = item => ({ asset_id: item.id, available: true, preview_available: true, status: "FILE_PRESENT" });

function mocks(buttons = [], calls = []) {
  return {
    "next/link": props => React.createElement("a", { href: props.href }, props.children),
    "lucide-react": { RefreshCw: empty, Search: empty, Film: empty, Plus: empty, Upload: empty },
    "@/components/ui/button": { Button: props => { buttons.push(props); return React.createElement("button", { disabled: props.disabled }, props.children); } },
    "@/components/ui/input": { Input: props => React.createElement("input", { "aria-label": props["aria-label"], defaultValue: props.value }) },
    "@/components/ui/dialog": { Dialog: children, DialogContent: children, DialogDescription: children, DialogHeader: children, DialogTitle: children },
    "@/components/factory-ui": { Field: children, Panel: children, StatusBadge: props => React.createElement("span", null, props.status) },
    "@/lib/api": { API_URL: "http://UNIT_ONLY.invalid", request: async (url, options) => { calls.push({ url, options }); return { run_id: "unit-run", message: "UNIT_ONLY" }; } },
    "@/lib/types": { durationLabel: value => value + "s", timeLabel: value => value },
    "@tanstack/react-query": { useQuery: options => ({ data: options.queryKey[0] === "manual-compose-library"
      ? { current_person_asset_ids: { "unit-task": current.id }, blocked_task_ids: [] }
      : [current, background, old].map(mediaRow), refetch: async () => {} }), useQueryClient: () => ({ invalidateQueries: async () => {} }) },
  };
}
const picker = loadSource("src/components/asset-picker.tsx", mocks());
const reason = (item, kind = "person", media = mediaRow(item)) => picker.composeAssetReason(item, kind, "unit-task", current.id, media);

test("current RGBAlpha and backgrounds are reusable across task owners without accepting historical versions", () => {
  assert.equal(reason(current), "");
  assert.equal(reason(background, "background"), "");
  assert.match(reason(old), /Review.*恢复/);
  const other = asset("other", "person", { task_id: "other-task" });
  assert.match(reason(other), /正在核对来源/);
  assert.equal(picker.composeAssetReason(other, "person", "unit-task", current.id, mediaRow(other),
    { current_person_asset_ids: { "other-task": other.id }, blocked_task_ids: [] }), "");
  assert.equal(reason(asset("other-background", "background", { task_id: "other-task" }), "background"), "");
  assert.match(picker.composeAssetReason(other, "person", "unit-task", current.id, mediaRow(other),
    { current_person_asset_ids: { "other-task": other.id }, blocked_task_ids: ["other-task"] }), /人工拒绝/);
});

test("Final and ordinary RGB cannot masquerade as alpha persons", () => {
  assert.match(reason(asset("final", "final")), /不能冒充/);
  assert.match(reason(asset("rgb", "source")), /RGB.*Alpha/);
  assert.match(reason(asset("current-person", "person", { legacy_reference: {} })), /配对证据/);
  assert.match(reason(asset("video-background", "background", { codec: "h264" }), "background"), /不是可用图片/);
});

test("technical, rejected, mock and unavailable media remain disabled", () => {
  assert.match(reason(asset("current-person", "person", { legacy_reference: { ...current.legacy_reference, execution_scope: "TECHNICAL_VALIDATION" } })), /技术验证/);
  assert.match(reason(asset("current-person", "person", { status: "REJECTED" })), /拒绝/);
  assert.match(reason(asset("current-person", "person", { registry_status: "mock" })), /尚未登记/);
  assert.match(picker.composeAssetReason(current, "person", "unit-task", current.id), /正在核对/);
  assert.match(reason(current, "person", { ...mediaRow(current), available: false, status: "SIZE_MISMATCH" }), /SIZE_MISMATCH/);
});

test("picker filters actual IDs, names, sources, type and status", () => {
  const rows = [current, background, asset("failed", "person", { status: "failed", source_system: "SOURCE_SEARCH" })];
  assert.deepEqual(picker.filterPickerAssets(rows, "person", "READY", "CURRENT-PERSON"), [current]);
  assert.deepEqual(picker.filterPickerAssets(rows, "all", "all", "source_search"), [rows[2]]);
  assert.deepEqual(picker.filterPickerAssets(rows, "background", "READY", "UNIT_ONLY"), [background]);
});

test("picker checks the real cross-task library and defers unplayed media loading", async () => {
  const calls = [], queries = [], mock = mocks([], calls);
  mock["@tanstack/react-query"].useQuery = options => {
    queries.push(options);
    return { data: options.queryKey[0] === "manual-compose-library" ? { current_person_asset_ids: { "unit-task": current.id }, blocked_task_ids: [] }
      : [current, background].map(mediaRow), refetch: async () => {} };
  };
  const component = loadSource("src/components/asset-picker.tsx", mock);
  const properties = { open: true, onOpenChange: () => {}, taskId: "unit/task?", currentPersonId: current.id,
    assets: [current, background], onSelect: () => {} };
  const personHtml = renderToStaticMarkup(React.createElement(component.AssetPicker, { ...properties, kind: "person" }));
  const backgroundHtml = renderToStaticMarkup(React.createElement(component.AssetPicker, { ...properties, kind: "background" }));
  await queries[0].queryFn();
  assert.deepEqual(queries[0].queryKey, ["asset-media-status", "production", "compose-library"]);
  assert.equal(calls[0].url, "/api/assets/media-status?scope=production");
  await queries[1].queryFn();
  assert.equal(calls[1].url, "/api/manual-compose/library");
  assert.ok(personHtml.includes('preload="none"'));
  assert.ok(backgroundHtml.includes('loading="lazy"'));
});

test("real picker renders search, status, preview and metadata; closes only after selection completes", async () => {
  const buttons = [], calls = [], selected = [], opened = [];
  const component = loadSource("src/components/asset-picker.tsx", mocks(buttons, calls));
  const html = renderToStaticMarkup(React.createElement(component.AssetPicker, { open: true, onOpenChange: value => opened.push(value),
    kind: "person", taskId: "unit-task", currentPersonId: current.id, selectedId: current.id, assets: [current, old, background], onSelect: value => selected.push(value) }));
  for (const label of ["搜索合成素材", "合成素材类型", "合成素材状态", "实际素材预览", "32 × 48", "1s", "UNIT_SOURCE", "Review 显式恢复"]) assert.ok(html.includes(label), label);
  assert.ok(html.includes("创建 2026-10-08T00:00:00Z · 任务 unit-task"));
  const selectedCard = buttons.find(button => button.children === "已选中素材");
  assert.equal(Boolean(selectedCard.disabled), false);
  selectedCard.onClick();
  assert.deepEqual(selected, []);
  assert.deepEqual(opened, []);
  const confirm = buttons.find(button => button.children === "确认选择");
  assert.equal(Boolean(confirm.disabled), false);
  confirm.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(selected, [current]);
  assert.deepEqual(opened, [false]);
  assert.equal(calls.length, 0);
});

function renderManual(saved = false, liveJob, savedSelection, extraAssets = [], versionsExtra = {}, reviewStatus = "pending", runOverride = [], fixture = {}) {
  const buttons = [], calls = [], pickerProps = [];
  const task = { id: "unit-task", context: { production: { current_person_asset_id: current.id } }, ...fixture.task };
  const review = { id: "review/unit composition", asset_id: "composition", status: reviewStatus };
  const composition = asset("composition", "compose", { legacy_reference: { background_asset_id: background.id, person_asset_id: current.id, processing_run_id: "unit-run" } });
  const manifest = { mode: "COMPOSITION", processing_run_id: "unit-run", person_asset_id: current.id, background_asset_id: background.id,
    output_sha256: "UNIT_SHA", output_media: { width: 64, height: 96, fps: 6, frame_count: 6 }, cutout_inference_executions: 0 };
  const mock = mocks(buttons, calls);
  mock["@tanstack/react-query"].useQuery = options => ({ data: options.queryKey[0] === "manual-compose-config" ? { manual_import_max_bytes: 12345 }
    : options.queryKey[0] === "manual-transforms" ? { current_person_asset_id: current.id, selection: savedSelection, latest_composition_asset_id: saved ? composition.id : undefined,
      job: liveJob ? { run_id: liveJob.run_id, status: liveJob.status, phase: liveJob.phase, stage: "compose" }
        : saved ? { run_id: "unit-run", status: "READY", manifest } : undefined, ...versionsExtra } : liveJob, refetch: async () => {} });
  mock["@/components/asset-picker"] = { AssetPicker: props => { pickerProps.push(props); return null; } };
  const component = loadSource("src/components/manual-compose.tsx", mock);
  const html = renderToStaticMarkup(React.createElement(component.ManualCompose, { task, assets: [fixture.person || current, background, composition, ...extraAssets], reviews: [...(saved ? [review] : []), ...(fixture.reviews || [])],
    runs: runOverride, pickerOpen: true, onPickerOpenChange: () => {} }));
  return { html, calls, pickerProps, button: name => buttons.find(button => label(button.children) === name) };
}

test("Manual Compose selection persists asset identity without transform editing or restore mutation", async () => {
  const h = renderManual();
  for (const name of ["添加素材库视频", "添加素材库背景", "生成短片预览", "生成完整合成"]) assert.ok(h.button(name), name);
  for (const oldControl of ["X · 像素", "Y · 像素", "Scale", "Apply", "Undo", "人物缩放"]) assert.equal(h.html.includes(oldControl), false, oldControl);
  assert.equal(h.pickerProps[0].open, true);
  await h.pickerProps[0].onSelect(current);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, "/api/manual-compose/tasks/unit-task/selection");
  assert.equal(h.calls[0].options.method, "PUT");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { kind: "person", asset_id: current.id });
});

test("saved background selection is used after reloading instead of silently selecting a newer upload", async () => {
  const h = renderManual(false, undefined, { person_asset_id: current.id, background_asset_id: background.id }, [asset("newer-background", "background")]);
  assert.equal(h.pickerProps[0].selectedId, current.id);
  assert.equal(Boolean(h.button("生成完整合成").disabled), false);
  assert.ok(h.html.includes("尚未开始合成背景") || h.html.includes("尚未合成背景"));
  h.button("生成完整合成").onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(JSON.parse(h.calls[0].options.body).background_asset_id, background.id);
});

test("upload buttons can open file selection before any file exists", () => {
  const h = renderManual();
  assert.equal(Boolean(h.button("上传并验证人物").disabled), false);
  assert.equal(Boolean(h.button("上传并验证背景").disabled), false);
  assert.ok(h.html.includes("人物 RGB 或透明视频文件"));
  assert.ok(h.html.includes("人物 Alpha 遮罩文件"));
  assert.ok(h.html.includes("背景图片文件"));
  assert.equal(h.calls.length, 0);
});

test("failed selection keeps picker open and does not claim successful selection", async () => {
  const buttons = [], opened = [], mock = mocks(buttons);
  const component = loadSource("src/components/asset-picker.tsx", mock);
  renderToStaticMarkup(React.createElement(component.AssetPicker, { open: true, onOpenChange: value => opened.push(value),
    kind: "person", taskId: "unit-task", currentPersonId: current.id, selectedId: current.id, assets: [current], onSelect: async () => { throw new Error("selection media changed"); } }));
  buttons.find(button => button.children === "确认选择").onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(opened, []);
});

test("cancelled staged choice does not save or replace the persisted selection", () => {
  const buttons = [], selected = [], opened = [];
  const component = loadSource("src/components/asset-picker.tsx", mocks(buttons));
  renderToStaticMarkup(React.createElement(component.AssetPicker, { open: true, onOpenChange: value => opened.push(value),
    kind: "background", taskId: "unit-task", currentPersonId: current.id, selectedId: background.id, assets: [background], onSelect: value => selected.push(value) }));
  buttons.find(button => button.children === "已选中素材").onClick();
  buttons.find(button => button.children === "取消选择").onClick();
  assert.deepEqual(selected, []);
  assert.deepEqual(opened, [false]);
});

test("cross-task persisted selections reload and render their exact IDs", async () => {
  const person = asset("donor-person", "person", { task_id: "donor-task" });
  const bg = asset("donor-background", "background", { task_id: "background-donor" });
  const h = renderManual(false, undefined, { person_asset_id: person.id, background_asset_id: bg.id }, [person, bg]);
  assert.ok(h.html.includes("复用来源任务 donor-task"));
  assert.equal(h.pickerProps[0].selectedId, person.id);
  assert.equal(Boolean(h.button("生成完整合成").disabled), false);
  h.button("生成完整合成").onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { task_id: "unit-task", person_asset_id: person.id, background_asset_id: bg.id, preview: false });
});

test("actual full-render button submits only selected asset IDs and preview flag", async () => {
  const h = renderManual();
  assert.equal(Boolean(h.button("生成完整合成").disabled), false);
  h.button("生成完整合成").onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, "/api/manual-compose/render");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { task_id: "unit-task", person_asset_id: current.id, background_asset_id: background.id, preview: false });
});

test("persisted true render links the precise new Quality Review rather than automatically approving", () => {
  const h = renderManual(true);
  assert.ok(h.html.includes("打开质量审核"));
  assert.ok(h.html.includes("review=review%2Funit%20composition"));
  assert.equal(h.html.includes("批准成片"), false);
  assert.equal(h.calls.length, 0);
});

test("saved background continuation displays persisted item identities without triggering work on load", () => {
  const h = renderManual(true, undefined, undefined, [], { saved_backgrounds: {
    id: "reuse-id", status: "WAITING_FIRST_REVIEW", person_asset_id: current.id, first_review_id: "review/unit composition", items: [
      { ordinal: 1, step_key: "item-1", status: "READY", asset_id: "real-composition", run_id: "real-run", output_sha256: "real-sha" },
      { ordinal: 2, step_key: "item-2", status: "WAITING" },
    ],
  } });
  assert.ok(h.html.includes("1 / 2"));
  assert.ok(h.html.includes("real-run"));
  assert.ok(h.html.includes("/assets/real-composition"));
  assert.ok(h.html.includes("review=review%2Funit%20composition"));
  assert.equal(h.calls.length, 0);
});

test("legacy completed render prepares bounded reuse only when backend advertises the real capability", async () => {
  const backgrounds = Array.from({ length: 19 }, (_, index) => asset("background-" + index, "background"));
  const unsupported = renderManual(true, undefined, undefined, backgrounds);
  assert.equal(unsupported.button("核验已有背景复用"), undefined);
  const supported = renderManual(true, undefined, undefined, backgrounds, { saved_backgrounds_supported: true });
  assert.equal(Boolean(supported.button("核验已有背景复用").disabled), false);
  supported.button("核验已有背景复用").onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(supported.calls.length, 1);
  assert.equal(supported.calls[0].url, "/api/manual-compose/tasks/unit-task/saved-backgrounds/prepare");
  assert.equal(supported.calls[0].options.method, "POST");
});

test("human-approved completed render is not labelled waiting for human review", () => {
  const h = renderManual(true, undefined, undefined, [], {}, "approved");
  assert.ok(h.html.includes("当前版本已通过人工审核"));
  assert.equal(h.html.includes("完整合成 · 待人工审核"), false);
  assert.equal(h.calls.length, 0);
});

test("quality telemetry cannot replace the verified composition preview with a person black preview", () => {
  const manifest = { mode: "COMPOSITION", processing_run_id: "unit-run", person_asset_id: current.id, background_asset_id: background.id,
    output_sha256: "UNIT_SHA", output_media: { width: 64, height: 96, fps: 6, frame_count: 6 }, cutout_inference_executions: 0 };
  const h = renderManual(true, undefined, undefined, [], { job: { stage: "quality", status: "READY", manifest: { mode: "REAL", input_sha256: "alpha" } } }, "approved", [
    { id: "unit-run", task_id: "unit-task", provider: "FactoryManualCompose", status: "success", resource_info: { manual_compose: manifest } },
  ]);
  assert.ok(h.html.includes("真实合成视频"));
  assert.ok(h.html.includes("/api/manual-compose/runs/unit-run/preview"));
  assert.ok(h.html.includes("当前版本已通过人工审核"));
  assert.equal(h.html.includes("黑底 · 尚未合成背景"), false);
  assert.equal(h.calls.length, 0);
});

test("failed saved background retries only its actual ordinal and never starts completed items", async () => {
  const h = renderManual(true, undefined, undefined, [], { saved_backgrounds: { id: "reuse", status: "COMPLETED_WITH_ERRORS", person_asset_id: current.id, items: [
    { ordinal: 1, step_key: "01", status: "READY", background_asset_id: background.id },
    { ordinal: 2, step_key: "02", status: "FAILED", background_asset_id: "background-02", error: "local render error" },
  ] } });
  assert.equal(h.button("仅重试 01"), undefined);
  assert.equal(Boolean(h.button("仅重试 02").disabled), false);
  h.button("仅重试 02").onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, "/api/manual-compose/tasks/unit-task/saved-backgrounds/retry");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { ordinal: 2 });
});

const savedReuse = { id: "reuse", status: "AUTHORIZED", person_asset_id: current.id, items: [
  { ordinal: 1, step_key: "01", status: "READY", background_asset_id: background.id },
  { ordinal: 2, step_key: "02", status: "FAILED", background_asset_id: "background-02", error: "local render error" },
] };

test("existing saved intent can explicitly recheck the same backend preparation without approving or rendering", async () => {
  const h = renderManual(true, undefined, undefined, [], { saved_backgrounds: savedReuse }, "approved");
  assert.equal(Boolean(h.button("重新核验已有背景复用").disabled), false);
  assert.equal(h.calls.length, 0);
  h.button("重新核验已有背景复用").onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, "/api/manual-compose/tasks/unit-task/saved-backgrounds/prepare");
  assert.equal(h.calls[0].options.method, "POST");
  assert.ok(h.calls[0].options.signal instanceof AbortSignal);
  assert.equal(h.calls[0].options.body, undefined);
});

test("rejected current person keeps old authorization visible but cannot prepare or retry", async () => {
  for (const status of ["rejected", "REJECTED"]) {
    const h = renderManual(true, undefined, undefined, [], { saved_backgrounds: savedReuse }, "approved", [], {
      person: { ...current, status }, reviews: [{ id: "review/rejected person", asset_id: current.id, status: "rejected", note: "手提袋缺失" }],
    });
    assert.ok(h.html.includes("WAITING_MANUAL"));
    assert.ok(h.html.includes("AUTHORIZED"));
    assert.ok(h.html.includes("手提袋缺失"));
    assert.ok(h.html.includes("review=review%2Frejected%20person"));
    assert.equal(Boolean(h.button("重新核验已有背景复用").disabled), true);
    assert.equal(Boolean(h.button("仅重试 02").disabled), true);
    h.button("重新核验已有背景复用").onClick();
    h.button("仅重试 02").onClick();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.calls.length, 0);
  }
});

test("rejected task cannot use a non-rejected person to bypass human refusal", async () => {
  const h = renderManual(true, undefined, undefined, [], { saved_backgrounds: savedReuse }, "approved", [], {
    task: { status: "REJECTED" },
  });
  assert.ok(h.html.includes("当前任务已被人工拒绝"));
  assert.ok(h.html.includes("/review?task=unit-task"));
  assert.equal(Boolean(h.button("重新核验已有背景复用").disabled), true);
  assert.equal(Boolean(h.button("仅重试 02").disabled), true);
  h.button("重新核验已有背景复用").onClick();
  h.button("仅重试 02").onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.calls.length, 0);
});

test("saved intent cannot recheck while its real run is active or current person is unavailable", async () => {
  const active = renderManual(true, undefined, undefined, [], { saved_backgrounds: savedReuse }, "approved", [
    { id: "active-run", task_id: "unit-task", status: "running" },
  ]);
  const unavailable = renderManual(true, undefined, undefined, [], { saved_backgrounds: savedReuse, current_person_asset_id: "missing-person" }, "approved");
  for (const h of [active, unavailable]) {
    assert.equal(Boolean(h.button("重新核验已有背景复用").disabled), true);
    assert.equal(Boolean(h.button("仅重试 02").disabled), true);
    h.button("重新核验已有背景复用").onClick();
    h.button("仅重试 02").onClick();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.calls.length, 0);
  }
});

test("first preparation also respects a rejected task", async () => {
  const backgrounds = Array.from({ length: 19 }, (_, index) => asset("background-" + index, "background"));
  const h = renderManual(true, undefined, undefined, backgrounds, { saved_backgrounds_supported: true }, "approved", [], {
    task: { status: "REJECTED" },
  });
  assert.equal(Boolean(h.button("核验已有背景复用").disabled), true);
  h.button("核验已有背景复用").onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.calls.length, 0);
});

test("queued and running jobs show the persisted Run ID and only an observed PID", () => {
  for (const [status, pid, shown] of [["QUEUED", null, "待启动"], ["RUNNING", 4321, "4321"]]) {
    const h = renderManual(false, { run_id: "unit-live-run", status, phase: "VALIDATING", run: { pid } });
    assert.ok(h.html.includes("Run unit-live-run · PID " + shown));
    assert.ok(h.html.includes("VALIDATING"));
    assert.equal(Boolean(h.button("生成完整合成").disabled), true);
    assert.equal(h.calls.length, 0);
  }
});

test("manual mode button opens the real picker in the same click", () => {
  const buttons = [], updates = [], mock = mocks(buttons);
  let index = 0;
  mock.react = { ...React, useState: initial => { const slot = index++; return [initial, value => updates.push({ slot, value })]; } };
  const routes = [];
  mock["next/navigation"] = { useSearchParams: () => ({ get: () => null }), useRouter: () => ({ replace: (...args) => routes.push(args) }) };
  mock["@/lib/api"].useFactory = () => ({ data: { tasks: [{ id: "unit-task", execution_mode: "integration", current_stage: "compose", context: {} }] } });
  mock["@/components/factory-ui"] = { EmptyState: children, FactoryGuard: children, PageHeading: children, TaskPicker: empty };
  mock["@/components/manual-compose"] = { ManualCompose: empty };
  mock["@/components/real-workbench"] = { RealWorkbench: empty };
  const component = loadSource("src/app/compose/page.tsx", mock);
  renderToStaticMarkup(React.createElement(component.default));
  buttons.find(button => button.children === "手动合成").onClick();
  assert.deepEqual(updates, [{ slot: 1, value: "manual" }, { slot: 2, value: true }]);
  assert.deepEqual(routes, [["/compose?task=unit-task&mode=manual", { scroll: false }]]);
});
