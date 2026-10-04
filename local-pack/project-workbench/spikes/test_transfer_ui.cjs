// Controlled widget bridge test. No ChatGPT account/host/file access is involved.
const fs=require('node:fs');const path=require('node:path');
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch({headless:true});const page=await browser.newPage();const result={bridge:'MOCK',host_verified:false,checks:[]};
try{await page.goto('file://'+path.resolve(__dirname,'../ui/transfer-spike.html'));
await page.click('#transfer');const blocked=JSON.parse(await page.textContent('#status'));if(blocked.status!=='BLOCKED_OR_FAILED')throw Error('Missing host not blocked');result.checks.push({missing_host:'PASS'});
for(const size of [1048576,20971520]){await page.addInitScript(({size})=>{const data=Uint8Array.from({length:size},(_,i)=>i%256);let file,hash;
const sha=async x=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',x))).map(c=>c.toString(16).padStart(2,'0')).join('');
window.openai={uploadFile:async f=>{file=f;return{fileId:'MOCK_HOST_FILE'}},getFileDownloadUrl:async()=>({downloadUrl:URL.createObjectURL(file)}),setWidgetState:s=>{window.lastMockState=s},
callTool:async(name,args)=>{hash??=await sha(data);if(name==='get_task_result')return{structuredContent:{data:{result:{artifacts:[{name:'MOCK_BINARY_FIXTURE.bin',size_bytes:data.length,sha256:hash,mime_type:'application/octet-stream',artifact_identity:'mock:0'}]}}}};
if(name==='read_artifact_chunk'){const chunk=data.slice(args.offset,args.offset+args.limit);let s='';for(const b of chunk)s+=String.fromCharCode(b);return{structuredContent:{data:{offset:args.offset,next_offset:args.offset+chunk.length,chunk_sha256:await sha(chunk)}},_meta:{bytes_base64:btoa(s)}}}throw Error('Unexpected mock tool');}};
},{size});await page.reload();await page.fill('#task','MOCK_TASK');await page.click('#transfer');await page.waitForFunction(()=>{try{return['artifact_host_roundtrip','BLOCKED_OR_FAILED'].includes(JSON.parse(document.querySelector('#status').textContent).kind||JSON.parse(document.querySelector('#status').textContent).status)}catch{return false}},{},{timeout:30000});const receipt=JSON.parse(await page.textContent('#status'));if(receipt.widget_roundtrip!=='PASS'||receipt.next_turn_model_access!=='PENDING')throw Error(JSON.stringify(receipt));result.checks.push({size_bytes:size,widget_mock_roundtrip:'PASS',model_access:'NOT_TESTED'});}
}finally{await browser.close();fs.writeFileSync(process.argv[2],JSON.stringify(result,null,2));}
})().catch(e=>{process.stderr.write(e.stack+'\n');process.exitCode=1});
