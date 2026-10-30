const test = require('node:test');
const assert = require('node:assert/strict');
const {serveCodexUsageRequests} = require('../scripts/codex_usage_bridge.js');
const request = sequence => JSON.stringify({event:'tool_request', sequence, tool:'usage', arguments:{}})+'\n';

test('each sequential request gets a fresh complete tool result, never a cache', async () => {
  const raw = [{content:[{type:'text',text:'{"private":"first"}'}]},
    {content:[{type:'text',text:'{"private":"second"}'}]}];
  const sent = [];
  let count = 0;
  const result = await serveCodexUsageRequests({
    readUsage: async args => { assert.deepEqual(args, {}); return raw[count++]; },
    write: async args => {
      sent.push(args.chars);
      return sent.length === 1 ? {session_id:1, output:request(2)} :
        {exit_code:0, output:'{"event":"cycle_complete","new_submission":true}\n'};
    },
  }, {session_id:1, output:request(1)});
  assert.equal(result.status, 'complete');
  assert.equal(count, 2);
  assert.deepEqual(sent, raw.map(r=>JSON.stringify(r)+'\n'));
  assert.equal(JSON.stringify(result).includes('private'), false);
});

test('broker requests, arguments and replayed sequences never invoke a tool', async () => {
  for (const change of [{tool:'etoro_place_trade'}, {arguments:{remaining:99}}, {sequence:0}]) {
    const event = {event:'tool_request',sequence:1,tool:'usage',arguments:{},...change};
    const result = await serveCodexUsageRequests({readUsage:async()=>assert.fail(),write:async()=>assert.fail()},
      {session_id:1,output:JSON.stringify(event)+'\n'});
    assert.equal(result.status, 'held');
    assert.equal(result.usageCalls, 0);
  }
});

test('tool errors and uncertain delivery hold without retry or fabricated evidence', async () => {
  let reads=0, writes=0;
  const unavailable = await serveCodexUsageRequests({
    readUsage:async()=>{reads++; throw Error('PRIVATE');},write:async()=>{writes++;},
  }, {session_id:1,output:request(1)});
  assert.equal(unavailable.reason,'bridge_usage_tool_unavailable');
  assert.equal(reads,1); assert.equal(writes,0);
  const unknown = await serveCodexUsageRequests({
    readUsage:async()=>{reads++; return {content:[]};},
    write:async()=>{writes++; throw Error('PRIVATE');},
  }, {session_id:1,output:request(1)});
  assert.equal(unknown.reason,'bridge_response_delivery_unknown_no_retry');
  assert.equal(reads,2); assert.equal(writes,1);
  assert.equal(JSON.stringify([unavailable,unknown]).includes('PRIVATE'),false);
});

test('fragmented process output is assembled without inventing a request', async () => {
  let step=0;
  const line=request(1);
  const result = await serveCodexUsageRequests({
    readUsage:async()=>({content:[]}),
    write:async args=>++step===1 ? {session_id:1,output:line.slice(10)} :
      {exit_code:2,output:'{"event":"stopped","reason":"usage_probe_held"}\n'},
  }, {session_id:1,output:line.slice(0,10)});
  assert.equal(result.status,'held'); assert.equal(result.exitCode,2); assert.equal(result.usageCalls,1);
});

test('unexpected or private raw stdout is not reflected to the caller', async () => {
  const result = await serveCodexUsageRequests({}, {exit_code:1,output:'PRIVATE_RAW_OUTPUT\n'});
  assert.equal(result.reason,'bridge_unexpected_output');
  assert.equal(JSON.stringify(result).includes('PRIVATE_RAW_OUTPUT'),false);
});

test('an exit code without a terminal summary is not claimed complete', async () => {
  const result = await serveCodexUsageRequests({}, {exit_code:0,output:''});
  assert.equal(result.status,'held');
});

test('a silent live process has a bounded observation hold, never a restart', async () => {
  let polls=0;
  const result = await serveCodexUsageRequests({
    write:async args=>{assert.equal(args.chars,''); polls++; return {session_id:1,output:''};},
  }, {session_id:1,output:''});
  assert.equal(result.reason,'bridge_process_observation_limit');
  assert.equal(result.sessionId,1); assert.equal(polls,120);
});

test('JSON null, arrays and scalars hold with the original process handle', async () => {
  for (const value of [null, [], 42, 'private', true]) {
    const result = await serveCodexUsageRequests({readUsage:async()=>assert.fail(),write:async()=>assert.fail()},
      {session_id:7, output:JSON.stringify(value)+'\n'});
    assert.equal(result.reason,'bridge_unexpected_output');
    assert.equal(result.sessionId,7); assert.equal(result.usageCalls,0);
  }
});

test('an array returned by the quota tool is not delivered as evidence', async () => {
  const result = await serveCodexUsageRequests({readUsage:async()=>[],write:async()=>assert.fail()},
    {session_id:7, output:request(1)});
  assert.equal(result.reason,'bridge_usage_response_unavailable');
  assert.equal(result.sessionId,7); assert.equal(result.usageCalls,1);
});
