import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const directory = path.dirname(fileURLToPath(import.meta.url));

test("the existing failed-stage button uses same-generation recovery only for the interrupted real cutout", () => {
  const source = fs.readFileSync(path.join(directory, "../src/components/integration-task-detail.tsx"), "utf8");
  assert.match(source, /const checkpointRetry=task\.status==="FAILED" && latestRun\?\.stage==="cutout"/);
  assert.match(source, /latestRun\.provider==="LegacyComfyUIAdapter"/);
  assert.match(source, /latestRun\.error_code==="STAGE_RESULT_INVALID"/);
  assert.match(source, /latestRun\.error_message==="TimeoutError" && !production\.current_person_asset_id/);
  assert.match(source, /factory\.act\(checkpointRetry\?"production\.resume_cutout":"production\.retry",task\.id\)/);
  assert.match(source, /从失败阶段重试/);
});

test("readonly checkpoint validation has its own timeout rather than losing a recovery receipt after ten seconds", () => {
  const source = fs.readFileSync(path.join(directory, "../src/lib/api.ts"), "utf8");
  assert.match(source, /\["production\.resume","production\.resume_cutout"\]\.includes\(input\.action\)\?\{signal:AbortSignal\.timeout\(120000\)\}/);
});
