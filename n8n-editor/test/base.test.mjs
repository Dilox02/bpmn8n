import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { editorBase } from '../live.mjs';

test('the editor base is n8n\'s BASE_PATH, else the hook\'s own folder', () => {
  assert.equal(editorBase('http://localhost:8090/n8n/static/bpmn8n-executions.js?v=7', '/n8n/'), '/n8n/');
  assert.equal(editorBase(new URL('http://localhost:8090/n8n/static/bpmn8n-executions.js?v=7'), undefined), '/n8n/');
  assert.equal(editorBase('http://localhost:5678/static/bpmn8n-executions.js?v=7', undefined), '/');
});

/** bpmn.js run against a minimal DOM: the tokens <link> it adds and the CSS it writes. */
function loadBpmn(basePath, src) {
  const added = [];
  const element = tag => ({ tagName: tag, dataset: {}, style: {} });
  const document = {
    currentScript: { src }, documentElement: {},
    querySelector: () => null, querySelectorAll: () => [], createElement: element,
    head: { prepend: el => added.push(el), appendChild: el => added.push(el) },
  };
  const window = { BASE_PATH: basePath };
  vm.runInNewContext(readFileSync(new URL('../bpmn.js', import.meta.url), 'utf8'), {
    window, document, location: new URL(src), URL, URLSearchParams,
    MutationObserver: class { observe() {} },
  });
  return { link: added.find(el => el.rel === 'stylesheet'), css: added.map(el => el.textContent || '').join('\n') };
}

test('bpmn.js loads the tokens and the BPMN glyphs under n8n\'s path (one origin: /n8n/)', () => {
  for (const [basePath, src] of [['/n8n/', 'http://localhost:8090/n8n/static/bpmn8n-bpmn.js?v=7'],
                                 [undefined, 'http://localhost:8090/n8n/static/bpmn8n-bpmn.js?v=7']]) {
    const { link, css } = loadBpmn(basePath, src);
    assert.equal(link.href, '/n8n/static/bpmn8n-tokens.css?v=1');
    assert.match(css, /url\(\/n8n\/icons\/CUSTOM\/nodes\/icons\/bpmn\/events\/start-event-none\.svg\)/);
    assert.doesNotMatch(css, /url\(\/icons\//);
  }
});

test('bpmn.js keeps the root paths on n8n\'s own origin', () => {
  const { link, css } = loadBpmn('/', 'http://localhost:5678/static/bpmn8n-bpmn.js?v=7');
  assert.equal(link.href, '/static/bpmn8n-tokens.css?v=1');
  assert.match(css, /url\(\/icons\/CUSTOM\/nodes\/icons\/bpmn\/gateways\/gateway-or\.svg\)/);
});
