const {test}=require('node:test');const assert=require('node:assert/strict');
const {assertLocalRequest,assertMutation}=require('../src/local-api-guard.cjs');
test('only local host and origin can reach APIs',()=>{
 for(const headers of [{host:'127.0.0.1:18792'},{host:'localhost:18792',origin:'http://localhost:18792'}])assert.doesNotThrow(()=>assertLocalRequest({headers},18792));
 for(const headers of [{host:'example.invalid:18792'},{host:'127.0.0.1:18792',origin:'https://example.invalid'},{host:'127.0.0.1:18792',origin:'null'},{host:'127.0.0.1:18792','sec-fetch-site':'cross-site'}])assert.throws(()=>assertLocalRequest({headers},18792),{status:403});
});
test('mutations reject navigation and non-JSON bodies but permit native JSON callers',()=>{
 assert.throws(()=>assertMutation({headers:{'sec-fetch-mode':'navigate'}}),{status:403});
 assert.throws(()=>assertMutation({headers:{'content-length':'20','content-type':'text/plain'}}),{status:415});
 assert.doesNotThrow(()=>assertMutation({headers:{'content-length':'20','content-type':'application/json; charset=utf-8'}}));
});
