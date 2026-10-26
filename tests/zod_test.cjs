// Exercise the actual upstream validators against the installed runtime package.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {createRequire} = require('node:module');
const root = process.env.ZOD_TEST_ROOT || '/home/frappe/frappe-bench/apps/helpdesk/desk';
const localRequire = createRequire(root + '/package.json');
const zod = localRequire('zod');
const version = localRequire('zod/package.json').version;
assert.match(version, /^4\./);
assert(!fs.existsSync(root + '/node_modules/@vee-validate/zod'));
const utils = fs.readFileSync(root + '/src/utils.ts', 'utf8');
function sourceFunction(name) {
  const match = utils.match(new RegExp('export function ' + name + '\\([^]*?\\n}'));
  assert(match, 'Upstream validator changed: ' + name);
  return match[0].replace('export ', '').replace(/: string/g, '');
}
const validate = new Function('zod', sourceFunction('extractEmail') + '\n' +
  sourceFunction('validateEmailWithZod') + '\nreturn validateEmailWithZod;')(zod);
for (const value of ['person@example.com', 'Person <person@example.com>', 'person+tag@example.co.uk']) {
  assert.equal(validate(value), true, value);
}
for (const value of ['', 'invalid', 'person@', 'a b@example.com', 'a..b@example.com',
  'a@example.c', 'a@localhost', 'ş@example.com', 'a@örnek.com',
  'a@sub_domain.example.com', 'a@-host.com', '.a@example.com', 'a.@example.com',
  'a'.repeat(100000) + '@']) {
  assert.equal(validate(value), false);
}
const phone = fs.readFileSync(root + '/src/components/ticket/SetContactPhoneModal.vue', 'utf8');
const expression = phone.match(/!z\s*\.string\(\)\s*\.regex\([^]*?\.safeParse\(contactDetails.value.phone\).success/);
assert(expression, 'Upstream phone validator changed');
const invalidPhone = new Function('z', 'contactDetails', 'return ' + expression[0]);
for (const value of ['+1234567', '+123456789012345']) {
  assert.equal(invalidPhone(zod, {value: {phone: value}}), false);
}
for (const value of ['', '123456789', '+123', '+1234567890123456', '+abcdefg', '+'.repeat(100000)]) {
  assert.equal(invalidPhone(zod, {value: {phone: value}}), true);
}
// The upstream fix is meaningful only with explicit container bounds.
let visits = 0;
const bounded = zod.array(zod.any().superRefine(() => { visits++; })).max(10);
assert.equal(bounded.safeParse(new Array(100000).fill(null)).success, false);
assert.equal(visits, 0);
console.log('PASS: installed Zod ' + version + ', actual Helpdesk email/phone validators and early collection rejection');
