/* eslint-disable @typescript-eslint/no-require-imports */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const component = fs.readFileSync(path.join(__dirname,"../src/components/collector-unattended.tsx"),"utf8");
const board = fs.readFileSync(path.join(__dirname,"../src/components/integration-collector.tsx"),"utf8");
const candidate = fs.readFileSync(path.join(__dirname,"../src/components/collector-candidate-review.tsx"),"utf8");
test("triage UI has explicit plan and fingerprint-bound apply, not local success",()=>{
  assert.match(component,/\/api\/collector\/auto-triage\/plan/);
  assert.match(component,/\/api\/collector\/auto-triage\/apply/);
  assert.match(component,/plan_id:plan!\.plan_id,fingerprint:plan!\.fingerprint,confirmed:true/);
  assert.match(component,/invalidateQueries\(\{queryKey:\["factory"\]/);
  assert.doesNotMatch(component,/production\.start|production\.resume|setTimeout/);
});
test("policy is persisted and historical result stays separate from production approval",()=>{
  assert.match(component,/method:"PUT"/);
  assert.match(component,/safe_pre_submit_retry_limit:policy\.data!\.safe_pre_submit_retry_limit/);
  assert.match(component,/不批准质检、不恢复暂停\/取消批次/);
  assert.match(component,/receipt\.paid_requests_submitted/);
  assert.match(board,/unattended\.data\?\.enabled===false/);
  assert.match(candidate,/collector_auto_triage/);
});
