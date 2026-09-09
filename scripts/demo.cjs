const {encode, Decoder, inputEvent, MicroEndpoint} = require('../src/micro-protocol.cjs');
const endpoint = new MicroEndpoint(), decoder = new Decoder();
const request = {method: 'v.oai.thstatus', id: 1,
  params: [{id: 0, c: 0x3388ff, b: 1, e: 4, s: 0.4}, {id: 1, c: 0x44dd66, b: 1, e: 1}]};
for (const report of encode(request)) for (const message of decoder.feed(report))
  console.log('Simulated device response:', JSON.stringify(endpoint.handle(message)));
console.table(endpoint.slots);
console.log('Simulated button press:', JSON.stringify(inputEvent('AG00', 1)));
console.log('Simulated button release:', JSON.stringify(inputEvent('AG00', 0)));
console.log('OFFLINE ONLY: no HID device created; no events sent to Codex or N4.');
