import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import {fileURLToPath} from "node:url";
import ts from "typescript";

const directory=path.dirname(fileURLToPath(import.meta.url));
const source=fs.readFileSync(path.join(directory,"../src/lib/api.ts"),"utf8");
const handler=source.match(/export async function submitFactoryAction[\s\S]*?\n\}/)?.[0];
assert.ok(handler,"验收实际动作函数，不能另写恢复演示");
class ApiRequestError extends Error {}
const timeout=Object.assign(new Error("response unknown"),{name:"TimeoutError"});
function setup(post,queue) {
  const calls=[];
  const request=async(path,options)=>{calls.push({path,options});if(options.method==="POST")return post();return queue();};
  const code=ts.transpileModule(handler.replace("export ",""),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  const submit=vm.runInNewContext(code+"\nsubmitFactoryAction;",{request,ApiRequestError,AbortSignal:{timeout:milliseconds=>({milliseconds})}});
  return {submit,calls};
}
const input={action:"production.resume",payload:{batch_id:"visible-original-batch"}};

test("原恢复请求等待120秒，成功回执不额外查询或重交",async()=>{
  const {submit,calls}=setup(()=>({message:"real receipt"}),()=>{throw new Error("must not query");});
  assert.equal((await submit(input)).message,"real receipt");
  assert.equal(calls.length,1);
  assert.equal(calls[0].options.signal.milliseconds,120000);
});
test("响应超时后只读核实同一RUNNING批次，不重发POST",async()=>{
  const {submit,calls}=setup(()=>{throw timeout;},()=>({batch_id:"visible-original-batch",status:"RUNNING"}));
  const result=await submit(input);
  assert.equal(result.accepted,true);
  assert.equal(result.status,"RUNNING");
  assert.equal(calls.filter(row=>row.options.method==="POST").length,1);
  assert.equal(calls[1].path,"/api/production/queue");
});
for(const queue of [{batch_id:"different-batch",status:"RUNNING"},{batch_id:"visible-original-batch",status:"PAUSED"}]) {
  test(`不把${queue.batch_id}/${queue.status}当作恢复成功`,async()=>{
    const {submit,calls}=setup(()=>{throw timeout;},()=>queue);
    const result=await submit(input);
    assert.equal(result.accepted,false);
    assert.equal(result.status,"UNKNOWN");
    assert.match(result.message,/不要重复点击/);
    assert.equal(calls.length,2);
  });
}
test("只读核实再次超时仍UNKNOWN，无自动重试",async()=>{
  const {submit,calls}=setup(()=>{throw timeout;},()=>{throw timeout;});
  assert.equal((await submit(input)).status,"UNKNOWN");
  assert.equal(calls.length,2);
});
test("后端明确拒绝不改写为UNKNOWN、不查询或重交",async()=>{
  const failure=new ApiRequestError("UNKNOWN_EXTERNAL_REQUEST");
  const {submit,calls}=setup(()=>{throw failure;},()=>{throw new Error("must not query");});
  await assert.rejects(submit(input),error=>error===failure);
  assert.equal(calls.length,1);
});
test("Collector与Task实际按钮共用同一个带回执核实的API函数",()=>{
  const collector=fs.readFileSync(path.join(directory,"../src/components/integration-collector.tsx"),"utf8");
  assert.match(collector,/mutationFn:submitFactoryAction/);
  assert.match(source,/: submitFactoryAction\(input\),onSuccess/);
});
