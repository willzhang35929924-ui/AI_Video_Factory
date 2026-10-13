/* eslint-disable @typescript-eslint/no-require-imports */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const component = fs.readFileSync(path.join(__dirname, "../src/components/integration-collector.tsx"), "utf8");
const board = component.slice(component.indexOf("export function IntegrationCollector()"));
const candidates = fs.readFileSync(path.join(__dirname, "../src/components/collector-candidate-review.tsx"), "utf8");

test("Collector board requests bounded backend pages, not a full factory state", () => {
  assert.match(board, /request<CollectorBoard>\("\/api\/collector\/tasks\?/);
  assert.doesNotMatch(board, /useFactory\(/);
  assert.doesNotMatch(candidates, /useFactory\(/);
  assert.match(board, /page_size:10/);
  assert.match(board, /\[10,30,50\]/);
  assert.match(board, /board\.data\.total_pages/);
  assert.match(board, /board\.data\?\.categories\.map/);
});

test("changing a filter clears selection and resets the page", () => {
  assert.match(board, /page:patch\.page \?\? 1/);
  assert.match(board, /setSelection\(\[\]\);setCreatedSelection\(\[\]\)/);
  assert.match(board, /changeBoard\(\{status:category\.status\}\)/);
  assert.match(board, /changeBoard\(\{search:event\.target\.value\}\)/);
  assert.match(board, /changeBoard\(\{page_size:Number\(event\.target\.value\)\}\)/);
});

test("Excel displays actual upload state and invalidates stale worksheet data", () => {
  assert.match(board, /setUploadingFile\(selectedFile\?\{name:selectedFile\.name,size:selectedFile\.size\}/);
  assert.match(board, /signal:AbortSignal\.timeout\(120000\)/);
  assert.match(board, /selectWorksheet\(worksheet:string\).*setRows\(\[\]\);setMappingSheet\(worksheet\)/);
  assert.match(board, /rows=mappingChanged\?\[\]:draftRows/);
  assert.match(board, /字段映射已修改，旧预览已停用/);
  assert.match(board, /sourceError && <p role="alert"/);
  assert.match(board, /setSourceError\(""\);notice\(""\);setImportId\(""\)/);
});
test("queue read errors never expose stale cancelled recovery or production controls", () => {
  assert.match(board, /currentQueue = queue\.isError \? undefined : queue\.data/);
  assert.match(board, /batchId=currentQueue\?\.batch_id/);
  assert.match(board, /batchId=\{currentQueue\?\.batch_id\} status=\{currentQueue\?\.status\}/);
  assert.match(board, /队列 UNKNOWN · 连接失败/);
  assert.match(board, /重新读取队列/);
  assert.doesNotMatch(board, /status=\{queue\.data\?\.status\}/);
});
