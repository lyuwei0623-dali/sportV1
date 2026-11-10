// No network or real user data: exercise component handshake/storage contract.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
const html = fs.readFileSync(require('node:path').join(__dirname,'../device_component/index.html'),'utf8');
const code = html.match(/<script>([\s\S]*)<\/script>/)[1];
function browser(saved, fail=false) {
  const handlers={}, messages=[];
  const parent={postMessage:(value,origin)=>messages.push({value,origin})};
  const context={crypto, Uint8Array, console,
    localStorage:{getItem:()=>{if(fail) throw Error('denied');return saved;},setItem:(_,v)=>{saved=v;}},
    document:{getElementById:()=>({textContent:''})},
    window:{parent,location:{origin:'https://app.example'},addEventListener:(type,fn)=>{handlers[type]=fn;}}};
  vm.runInNewContext(code,context);
  assert.equal(messages[0].value.type,'streamlit:componentReady');
  handlers.message({source:parent,origin:'https://app.example',data:{type:'streamlit:render'}});
  const result=messages.find(x=>x.value.type==='streamlit:setComponentValue').value.value;
  return {result,saved,messages,handlers,parent};
}
const a=browser(null), b=browser(null), again=browser(a.saved);
assert.match(a.result.token,/^[0-9a-f]{64}$/);
assert.notEqual(a.result.token,b.result.token);
assert.equal(a.result.token,again.result.token);
assert.equal(browser(null,true).result.ok,false);
const before=a.messages.length;
a.handlers.message({source:a.parent,origin:'https://attacker.example',data:{type:'streamlit:render'}});
assert.equal(a.messages.length,before);
console.log('Device component: persistence, isolation, denied storage and origin checks passed.');
