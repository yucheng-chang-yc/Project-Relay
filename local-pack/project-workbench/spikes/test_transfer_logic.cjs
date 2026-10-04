// Actual widget JavaScript with an in-memory mock. No browser/CSP/ChatGPT proof.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const {File} = require('node:buffer');
const html = fs.readFileSync(path.join(__dirname, '../ui/transfer-spike.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const sha = async bytes => Buffer.from(await webcrypto.subtle.digest('SHA-256', bytes)).toString('hex');

function boot(openai, fetch) {
  const elements = Object.fromEntries(['project', 'task', 'artifact', 'stage', 'transfer', 'next', 'status']
    .map(id => [id, {value: id === 'project' ? 'sandbox' : id === 'artifact' ? '0' : 'fixture-task', textContent: ''}]));
  vm.runInNewContext(script, {window: {openai}, document: {getElementById: id => elements[id]},
    crypto: webcrypto, URL, File, Blob, Uint8Array, fetch, atob: value => Buffer.from(value, 'base64').toString('binary')});
  return elements;
}

(async () => {
  const results = [];
  const missing = boot(undefined, undefined);
  await missing.stage.onclick();
  assert.equal(JSON.parse(missing.status.textContent).host_verified, false);
  results.push({case: 'missing_host_fail_closed', status: 'PASS'});

  for (const mib of [1, 20]) {
    const data = Uint8Array.from({length: mib * 1024 * 1024}, (_, i) => i % 256);
    const hash = await sha(data);
    const files = new Map([['input', data]]);
    let followup = null;
    const openai = {
      toolOutput: {data: {projects: [{id: 'computetrial'}]}},
      selectFiles: async () => [{fileId: 'input', fileName: 'fixture.bin', mimeType: 'application/octet-stream'}],
      getFileDownloadUrl: async ({fileId}) => ({downloadUrl: 'https://files.example/' + fileId + '?secret=DO_NOT_LOG'}),
      uploadFile: async file => {files.set('uploaded', new Uint8Array(await file.arrayBuffer())); return {fileId: 'uploaded'};},
      setWidgetState: () => {},
      sendFollowUpMessage: async message => {followup = message;},
      callTool: async (name, args) => {
        if (name === 'stage_binary_input') {
          assert.equal(args.project_id, 'computetrial');
          assert.equal(args.expected_sha256, hash);
          assert.equal(args.expected_size_bytes, data.length);
          return {structuredContent: {data: {input_id: 'input-fixture'}}};
        }
        if (name === 'get_task_result') return {structuredContent: {data: {result: {artifacts: [
          {name: 'fixture.bin', size_bytes: data.length, sha256: hash, mime_type: 'application/octet-stream'}]}}}};
        if (name === 'read_artifact_chunk') {
          const chunk = data.slice(args.offset, args.offset + args.limit);
          return {structuredContent: {data: {offset: args.offset, next_offset: args.offset + chunk.length,
            chunk_sha256: await sha(chunk)}}, _meta: {bytes_base64: Buffer.from(chunk).toString('base64')}};
        }
        throw Error('Unknown fixture tool');
      }
    };
    const fetch = async url => new Response(files.get(new URL(url).pathname.slice(1)));
    const elements = boot(openai, fetch);
    assert.equal(elements.project.value, 'computetrial');
    await elements.stage.onclick();
    assert.equal(JSON.parse(elements.status.textContent).kind, 'input_staged');
    await elements.transfer.onclick();
    const receipt = JSON.parse(elements.status.textContent);
    assert.equal(receipt.widget_roundtrip, 'PASS');
    assert.equal(receipt.next_turn_model_access, 'PENDING');
    assert.equal(receipt.sha256, hash);
    await elements.next.onclick();
    assert.ok(followup.prompt.includes('actual') || followup.prompt.includes('實際讀取'));
    openai.getFileDownloadUrl = async () => ({downloadUrl: 'https://files.example/input?secret=DO_NOT_LOG'});
    const blocked = boot(openai, async () => {throw Error('Fetch blocked https://files.example/input?secret=DO_NOT_LOG');});
    await blocked.stage.onclick();
    const diagnostic = JSON.parse(blocked.status.textContent);
    assert.equal(diagnostic.download_host, 'files.example');
    assert.equal(diagnostic.host_verified, false);
    assert.ok(!blocked.status.textContent.includes('DO_NOT_LOG'));
    results.push({case: mib + 'MiB_mock_stage_egress_diagnostic', status: 'PASS', model_access: 'PENDING'});
  }
  process.stdout.write(JSON.stringify({scope: 'JavaScript logic mock; no render/CSP/actual host', results}) + '\n');
})().catch(error => {process.stderr.write(error.stack + '\n'); process.exitCode = 1;});
