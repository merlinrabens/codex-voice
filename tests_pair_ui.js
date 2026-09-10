#!/usr/bin/env node
'use strict';
// Isolated pairing behavior tests. No browser, server, tunnel, or real codes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const script = fs.readFileSync(path.join(__dirname, 'static/pair.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'static/pair.html'), 'utf8');
const checkbox = html.match(/<input\b[^>]*\bid="pair-remember"[^>]*>/)[0];
const appBootstrap = fs.readFileSync(path.join(__dirname, 'static/app.js'), 'utf8').split('\n(() => {')[0];

function fixture({hash = '', signedIn = false, pairResponse, stateFailure = false} = {}) {
  const requests = [];
  const navigation = [];
  const elements = Object.fromEntries([
    'pair-code', 'pair-remember', 'pair-status', 'pair-help', 'pair-submit', 'pair-form'
  ].map(id => [id, {value: '', textContent: '', disabled: false, open: false}]));
  elements['pair-remember'].checked = /\schecked(?:\s|=|>)/.test(checkbox);
  let submit;
  elements['pair-form'].addEventListener = (event, callback) => {
    assert.equal(event, 'submit');
    submit = callback;
  };
  const location = {hash, pathname: '/', replace: target => navigation.push(target)};
  const sandbox = {
    URLSearchParams,
    document: {getElementById: id => elements[id]},
    location,
    history: {replaceState: (_state, _unused, target) => {
      navigation.push(`strip:${target}`);
      location.hash = '';
    }},
    fetch: async (url, options) => {
      requests.push({url, options, hashAtRequest: location.hash});
      if (url === '/api/state') {
        if (stateFailure) throw new Error('Offline');
        return {ok: signedIn};
      }
      assert.equal(url, '/api/pair');
      return pairResponse || {ok: true, json: async () => ({ok: true})};
    }
  };
  vm.runInNewContext(script, sandbox, {filename: 'pair.js'});
  return {elements, requests, navigation, submit: () => submit({preventDefault() {}})};
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test('a URL code stays private and is not consumed until the user connects', async () => {
  const state = fixture({hash: '#pair=offline-fixture-code'});
  await settle();
  assert.equal(state.elements['pair-code'].value, 'offline-fixture-code');
  assert.equal(state.requests.length, 1);
  assert.equal(state.requests[0].url, '/api/state');
  assert.equal(state.requests[0].hashAtRequest, '');
  assert.deepEqual(state.navigation, ['strip:/']);
  assert.equal(state.elements['pair-remember'].checked, false);
  await state.submit();
  assert.deepEqual(JSON.parse(state.requests[1].options.body), {
    code: 'offline-fixture-code', remember: false
  });
  assert.equal(state.elements['pair-code'].value, '');
  assert.equal(state.navigation.at(-1), '/');
});

test('remembering a device is an explicit pairing choice', async () => {
  const state = fixture({hash: '#pair=offline-fixture-code'});
  state.elements['pair-remember'].checked = true;
  await state.submit();
  assert.deepEqual(JSON.parse(state.requests.at(-1).options.body), {
    code: 'offline-fixture-code', remember: true
  });
});

test('returning without a code explains recovery and exposes the Mac command', async () => {
  const state = fixture();
  await settle();
  assert.match(state.elements['pair-status'].textContent, /new link from your Mac/);
  assert.equal(state.elements['pair-help'].open, true);
  assert.equal(state.requests.length, 1);
  assert.deepEqual(state.navigation, []);
  assert.match(html, /python3 remote\.py --pair --copy/);
});

test('a valid cookie returns to voice even when the connection page opened without a code', async () => {
  const state = fixture({signedIn: true});
  await settle();
  assert.deepEqual(state.navigation, ['/']);
  assert.equal(state.requests.length, 1);
});

test('a consumed or expired code leaves recovery available without navigating', async () => {
  const state = fixture({
    hash: '#pair=offline-fixture-code',
    pairResponse: {ok: false, json: async () => ({error: 'Pairing code expired or already used.'})}
  });
  await state.submit();
  assert.equal(state.elements['pair-submit'].disabled, false);
  assert.equal(state.elements['pair-help'].open, true);
  assert.match(state.elements['pair-status'].textContent, /expired or already used/);
  assert.deepEqual(state.navigation, ['strip:/']);
});

test('a failed status request still leaves the pairing form usable', async () => {
  const state = fixture({stateFailure: true});
  await settle();
  assert.equal(state.elements['pair-submit'].disabled, false);
  state.elements['pair-code'].value = ' manually-entered-fixture ';
  await state.submit();
  assert.equal(JSON.parse(state.requests.at(-1).options.body).code, 'manually-entered-fixture');
  assert.equal(state.navigation.at(-1), '/');
});

test('the authenticated app removes an invitation fragment without consuming its code', () => {
  const replacements = [];
  vm.runInNewContext(appBootstrap, {
    URLSearchParams,
    location: {hash: '#pair=offline-fixture-code', pathname: '/', search: '?view=voice'},
    history: {replaceState: (...args) => replacements.push(args)}
  });
  assert.deepEqual(replacements, [[null, '', '/?view=voice']]);
});

test('the authenticated app preserves unrelated URL fragments', () => {
  const replacements = [];
  vm.runInNewContext(appBootstrap, {
    URLSearchParams,
    location: {hash: '#conversation', pathname: '/', search: ''},
    history: {replaceState: (...args) => replacements.push(args)}
  });
  assert.deepEqual(replacements, []);
});
