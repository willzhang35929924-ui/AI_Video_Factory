// UNIT_ONLY dispatch/render contracts. No browser or production evidence.
/* eslint-disable @typescript-eslint/no-require-imports -- Node CommonJS harness loads transpiled application modules. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

function loadSource(filename, mocks, fetch) {
  const source = fs.readFileSync(path.join(__dirname, "..", filename), "utf8");
  const code = ts.transpileModule(source, { fileName: filename, compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    esModuleInterop: true,
  } }).outputText;
  const loadedModule = { exports: {} };
  const localRequire = name => Object.hasOwn(mocks, name) ? mocks[name] : require(name);
  new Function("require", "module", "exports", "fetch", code)(localRequire, loadedModule, loadedModule.exports, fetch);
  return loadedModule.exports;
}

function state() {
  const task = { id: "unit-task", execution_mode: "integration", status: "WAITING_REVIEW", keyword: "UNIT_ONLY",
    country: "MX", context: { production_strategy: "manual_compose", production: { current_person_asset_id: "current-person" },
      manual_compose: { auto_resume_required: true, latest_composition_asset_id: "manual-compose" } } };
  const asset = (id, type, extra = {}) => ({ id, task_id: task.id, type, name: id, registry_status: "registered",
    status: "READY", width: 32, height: 48, fps: 6, duration: 1, codec: "UNIT_ONLY", created_at: "2026-10-08T00:00:00Z",
    source_system: "LegacyComposeAdapter", legacy_reference: type === "person" ? {
      alpha_mode: "straight", alpha_path: "/UNIT_ONLY/Alpha.mkv", manifest_path: "/UNIT_ONLY/manifest.json", full_decode_verified: true,
    } : {}, ...extra });
  const legacy = Array.from({ length: 20 }, (_, index) => asset("legacy-compose-" + index, "compose",
    { legacy_reference: { person_asset_id: "historical-person" } }));
  const assets = [asset("source", "source"), asset("historical-person", "person"),
    asset("current-person", "person"), ...legacy,
    asset("manual-compose", "compose", { source_system: "FactoryManualCompose",
      legacy_reference: { manual_compose: true, person_asset_id: "current-person" } }),
    asset("technical-compose", "compose", { source_system: "FactoryManualCompose",
      legacy_reference: { manual_compose: true, execution_scope: "TECHNICAL_VALIDATION" } })];
  const review = item => ({ id: "review-" + item.id, task_id: task.id, asset_id: item.id,
    status: "pending", issues: [], note: "" });
  return { tasks: [task], assets, reviews: legacy.map(review), prompts: [], collector: [], publishing: [],
    runs: [], settings: { production_policy: { auto_advance: false } }, quality_assessments: [] };
}

function harness(data, params = {}) {
  const calls = [], notices = [];
  const fetch = async (url, options) => {
    const frontendPath = new URL(url, "https://factory-browser.example").pathname;
    calls.push({ frontendPath, pathname: frontendPath.replace(/^\/factory-api(?=\/)/, ""), options, body: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ message: "UNIT_ONLY_ACCEPTED" }) };
  };
  const mocks = {
    "@tanstack/react-query": {
      useQuery: () => ({ data, refetch: async () => ({ data }) }),
      useQueryClient: () => ({ invalidateQueries: async () => {} }),
      useMutation: options => ({ isPending: false, mutateAsync: async input => {
        const result = await options.mutationFn(input);
        await options.onSuccess?.(result);
        return result;
      } }),
    },
    "next/navigation": { usePathname: () => "/review", useSearchParams: () => ({ get: key => params[key] || null }) },
    "@/components/providers": { useNotice: () => message => notices.push(message) },
    "./types": { taskSchema: { parse: value => value } },
  };
  const api = loadSource("src/lib/api.ts", mocks, fetch);
  return { api, factory: api.useFactory(), calls, notices, mocks, fetch };
}

function label(children) {
  if (Array.isArray(children)) return children.map(label).join("");
  if (typeof children === "string" || typeof children === "number") return String(children);
  return React.isValidElement(children) ? label(children.props.children) : "";
}

function renderReview(data, params) {
  const h = harness(data, params);
  const buttons = [], transforms = [], repairs = [], previews = [];
  const children = props => React.createElement("div", null, props.children, props.action);
  const empty = () => null;
  const mocks = { ...h.mocks,
    "next/link": props => React.createElement("a", { href: props.href }, props.children),
    "lucide-react": { Check: empty, Keyboard: empty, RefreshCw: empty, SlidersHorizontal: empty, X: empty },
    "@/components/ui/button": { Button: props => { buttons.push(props); return React.createElement("button", { disabled: props.disabled }, props.children); } },
    "@/components/ui/textarea": { Textarea: props => React.createElement("textarea", { defaultValue: props.value }) },
    "@/components/factory-ui": { EmptyState: children, FactoryGuard: children, Field: children,
      PageHeading: empty, Panel: children, StatusBadge: empty },
    "@/components/production-ui": { AssetPreview: props => { previews.push(props); return null; }, RunLogDrawer: empty },
    "@/components/real-workbench": { IssueTimeline: empty },
    "@/components/matte-repair-drawer": { MatteRepairDrawer: props => { repairs.push(props); return null; } },
    "@/components/transform-controls": { PersonTransformReview: props => { transforms.push(props); return null; } },
    "@/lib/api": h.api,
    "@/lib/types": { timeLabel: () => "UNIT_ONLY" },
  };
  const { default: ReviewPage } = loadSource("src/app/review/page.tsx", mocks, h.fetch);
  renderToStaticMarkup(React.createElement(ReviewPage));
  return { ...h, transforms, repairs, previews, button: name => {
    const found = buttons.find(props => label(props.children) === name);
    assert.ok(found, "Missing button: " + name);
    return found;
  } };
}

test("all twenty legacy reviews keep all four legacy actions despite global manual task strategy", async () => {
  const h = harness(state());
  for (const review of h.factory.data.reviews) {
    for (const action of ["review.approve", "review.reject", "review.retry", "review.reprocess"]) {
      await h.factory.act(action, review.id, { note: "UNIT_ONLY" });
      const call = h.calls.at(-1);
      assert.equal(call.pathname, "/api/actions");
      assert.equal(call.body.action, action);
      assert.equal(call.body.entity_id, review.id);
      assert.equal(call.body.manualReview, undefined);
    }
  }
  assert.equal(h.calls.length, 80);
});

test("manual asset decisions use only the dedicated route and retries do not dispatch", async () => {
  const data = state();
  data.reviews = [{ id: "unit/manual review", task_id: "unit-task", asset_id: "manual-compose", status: "pending", issues: [], note: "" }];
  const h = harness(data);
  for (const action of ["approve", "reject"]) {
    await h.factory.act("review." + action, data.reviews[0].id, { note: "UNIT_DECISION" });
    assert.equal(h.calls.at(-1).pathname, "/api/manual-compose/reviews/unit%2Fmanual%20review/" + action);
    assert.deepEqual(h.calls.at(-1).body, { note: "UNIT_DECISION" });
  }
  for (const action of ["review.retry", "review.reprocess"]) assert.equal(await h.factory.act(action, data.reviews[0].id), null);
  assert.equal(h.calls.length, 2);
});

test("technical and non-review upload assets are not manual review assets", async () => {
  const data = state();
  data.reviews = [{ id: "technical-review", task_id: "unit-task", asset_id: "technical-compose", status: "pending", issues: [], note: "" }];
  const h = harness(data);
  assert.equal(h.api.isManualReviewAsset(data.assets.find(asset => asset.id === "technical-compose")), false);
  assert.equal(h.api.isManualReviewAsset({ type: "background", source_system: "FactoryManualUpload", legacy_reference: { manual_upload: true } }), false);
  await h.factory.act("review.approve", "technical-review");
  assert.equal(h.calls[0].pathname, "/api/actions");
});

test("legacy composition keeps review controls and coordinates act on the current person", () => {
  const h = renderReview(state());
  for (const name of ["通过", "拒绝", "从失败处重试", "局部修复", "重新处理整个视频"]) assert.equal(Boolean(h.button(name).disabled), false, name);
  assert.equal(h.transforms.length, 1);
  assert.equal(h.transforms[0].person.id, "current-person");
  assert.equal(h.repairs.length, 1);
  assert.equal(h.repairs[0].currentPersonId, "current-person");
  assert.equal(h.calls.length, 0);
});

test("manual current composition retains mask editing but cannot invoke legacy retry or full cutout", () => {
  const data = state();
  data.reviews = [{ id: "manual-review", task_id: "unit-task", asset_id: "manual-compose", status: "pending", issues: [], note: "" }];
  const h = renderReview(data);
  assert.equal(Boolean(h.button("通过").disabled), false);
  assert.equal(Boolean(h.button("拒绝").disabled), false);
  for (const name of ["从失败处重试", "重新处理整个视频"]) assert.equal(Boolean(h.button(name).disabled), true, name);
  for (const name of ["局部修复", "只处理异常区间"]) assert.equal(Boolean(h.button(name).disabled), false, name);
  assert.equal(h.repairs.length, 1);
  assert.equal(h.repairs[0].currentPersonId, "current-person");
  assert.equal(h.calls.length, 0);
});

test("saving a transform does not disable KEEP REMOVE point box brush propagation preview apply or undo entry", () => {
  const data = state();
  const person = data.assets.find(asset => asset.id === "current-person");
  person.source_system = "FactoryTransform";
  Object.assign(person.legacy_reference, { transform_version_id: "unit-transform", transform: { x: 42, y: -17, scale: .85 } });
  data.reviews = [{ id: "transform-review", task_id: "unit-task", asset_id: person.id, status: "pending", issues: ["MANUAL_TRANSFORM"], note: "" }];
  const h = renderReview(data);
  assert.equal(h.api.isManualReviewAsset(person), true);
  assert.equal(h.api.isMaskEditablePerson(person), true);
  assert.equal(Boolean(h.button("局部修复").disabled), false);
  assert.equal(Boolean(h.button("只处理异常区间").disabled), false);
  assert.equal(h.repairs[0].currentPersonId, person.id);
  assert.equal(h.transforms[0].person.id, person.id);
  assert.equal(h.calls.length, 0);
});

test("missing RGB Alpha evidence never opens a fake editable mask", () => {
  const data = state();
  const person = data.assets.find(asset => asset.id === "current-person");
  person.source_system = "FactoryTransform";
  delete person.legacy_reference.alpha_path;
  const h = renderReview(data);
  assert.equal(h.api.isMaskEditablePerson(person), false);
  assert.equal(Boolean(h.button("局部修复").disabled), true);
  assert.equal(Boolean(h.button("只处理异常区间").disabled), true);
  assert.equal(h.repairs.length, 0);
});

test("precise review links select their asset instead of an unrelated pending review", () => {
  const data = state();
  const target = { id: "exact-composition-review", task_id: "unit-task", asset_id: "manual-compose", status: "approved", issues: [], note: "" };
  data.reviews.push(target);
  const h = renderReview(data, { task: "unit-task", review: target.id });
  assert.equal(h.previews[1].asset.id, target.asset_id);
  assert.equal(Boolean(h.button("通过").disabled), true);
  assert.equal(h.calls.length, 0);
});

test("unknown precise review does not fall back to approving another version", () => {
  const h = renderReview(state(), { task: "unit-task", review: "missing-review" });
  assert.equal(h.previews.length, 0);
  assert.equal(h.transforms.length, 0);
  assert.equal(h.repairs.length, 0);
  assert.equal(h.calls.length, 0);
});

test("mask revision uses real manual render and position comes only from the persisted person version", async () => {
  const h = harness(state());
  const person = h.factory.data.assets.find(asset => asset.id === "current-person");
  person.source_system = "FactoryMatteRepair";
  Object.assign(person.legacy_reference, { transform_version_id: "unit-placement", transform: { x: 31, y: -9, scale: .7 } });
  const background = { id: "unit-background", task_id: "unit-task", type: "background", registry_status: "registered", legacy_reference: {} };
  await h.api.recomposePerson("unit-task", person, background);
  assert.equal(h.calls[0].pathname, "/api/manual-compose/render");
  assert.deepEqual(h.calls[0].body, { task_id: "unit-task", person_asset_id: person.id, background_asset_id: background.id });
});

test("manual recompose rejects absent or technical backgrounds without dispatching", async () => {
  const h = harness(state());
  const person = h.factory.data.assets.find(asset => asset.id === "current-person");
  person.source_system = "FactoryTransform";
  for (const background of [undefined, { id: "technical-bg", task_id: "unit-task", type: "background", registry_status: "registered", legacy_reference: { execution_scope: "TECHNICAL_VALIDATION" } }]) {
    await assert.rejects(h.api.recomposePerson("unit-task", person, background), /真实背景/);
  }
  assert.equal(h.calls.length, 0);
});

test("normal and technical recomposition retain their original routes", async () => {
  const h = harness(state());
  const person = h.factory.data.assets.find(asset => asset.id === "current-person");
  await h.api.recomposePerson("unit-task", person);
  assert.equal(h.calls[0].pathname, "/api/actions");
  assert.equal(h.calls[0].body.action, "production.retry");
  person.legacy_reference.execution_scope = "TECHNICAL_VALIDATION";
  await h.api.recomposePerson("unit-task", person);
  assert.equal(h.calls[1].pathname, "/api/tasks/unit-task/technical-validation/compose");
  assert.deepEqual(h.calls[1].body, {});
});
