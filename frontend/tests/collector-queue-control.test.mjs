import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import {fileURLToPath} from "node:url";
import ts from "typescript";

const __dirname=path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(__dirname,"../src/components/integration-collector.tsx"),"utf8");
const handler = source.match(/  async function controlQueue\(action:"pause"\|"resume"\|"cancel"\) \{[\s\S]*?\n  \}/)?.[0];
const currentQueueBinding = source.match(/  const currentQueue = queue\.isError \? undefined : queue\.data;/)?.[0];
assert.ok(handler,"必须测试实际 Collector 处理函数，不能另写一个示例函数");
assert.ok(currentQueueBinding,"必须复用真实队列读取失败保护");

function setup(batchId="actual-visible-batch",confirm=true,isError=false) {
  const calls=[], notices=[], confirmations=[];
  const queue={data:batchId?{batch_id:batchId}:undefined,isError};
  const factory={act:async (...args)=>{calls.push(args);}};
  const window={confirm:message=>{confirmations.push(message);return typeof confirm==="function"?confirm(queue):confirm;}};
  const code=ts.transpileModule(currentQueueBinding+"\n"+handler,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  const control=vm.runInNewContext(code+"\ncontrolQueue;",{queue,factory,window,notice:message=>notices.push(message)});
  return {control,calls,notices,confirmations,queue};
}

for(const action of ["pause","resume"])test(`${action} 携带当前批次，无取消确认`,async()=>{
  const {control,calls,confirmations}=setup();
  await control(action);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)),[["production."+action,null,{batch_id:"actual-visible-batch"}]]);
  assert.equal(confirmations.length,0);
});

test("取消二次确认被关闭时不发送后台动作",async()=>{
  const {control,calls,confirmations}=setup("actual-visible-batch",false);
  await control("cancel");
  assert.equal(calls.length,0);
  assert.equal(confirmations.length,1);
  assert.match(confirmations[0],/actual-visible-batch/);
  assert.match(confirmations[0],/这不是暂停/);
});

test("确认取消仅操作确认时批次，变更后由后台 stale guard 拒绝",async()=>{
  const {control,calls}=setup("old-batch",queue=>{queue.data.batch_id="new-batch";return true;});
  await control("cancel");
  assert.deepEqual(JSON.parse(JSON.stringify(calls)),[["production.cancel",null,{batch_id:"old-batch"}]]);
});

test("无批次时明确提示且不执行",async()=>{
  const {control,calls,notices}=setup("");
  await control("pause");
  assert.equal(calls.length,0);
  assert.match(notices[0],/当前没有可操作批次/);
});

for(const action of ["pause","resume","cancel"])test(`队列读取失败时 ${action} 不使用旧批次`,async()=>{
  const {control,calls,notices,confirmations}=setup("stale-batch",true,true);
  await control(action);
  assert.equal(calls.length,0);
  assert.equal(confirmations.length,0);
  assert.match(notices[0],/队列读取失败/);
  assert.match(notices[0],/旧状态未用于调度/);
});
