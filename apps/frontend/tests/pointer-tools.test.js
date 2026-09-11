import assert from 'node:assert/strict';
import { test } from 'node:test';
import { isMiddleButton, isPrimaryButton, installToolbarTooltips } from '../src/pointer-tools.js';

test('middle pan and primary selection are mutually exclusive, regardless of modifiers', () => {
  for (const tool of ['pan', 'point', 'box']) {
    for (const shiftKey of [false, true]) {
      for (const button of [0, 1, 2]) {
        const event = { tool, originalEvent: { button, shiftKey } };
        assert.equal(isMiddleButton(event), button === 1);
        assert.equal(isPrimaryButton(event), button === 0);
      }
    }
  }
});

test('clear selection gets an accessible tooltip with its original title', () => {
  const attributes = {};
  const tip = { setAttribute: (key, value) => { attributes[key] = value; } };
  const button = {
    title: '清除选择 (Esc)',
    ownerDocument: { createElement: () => tip },
    removeAttribute: (key) => { delete button[key]; },
    setAttribute: (key, value) => { attributes[key] = value; },
    append: (child) => { button.child = child; },
  };
  installToolbarTooltips({ querySelectorAll: () => [button] });
  assert.equal(button.child.textContent, '清除选择 (Esc)');
  assert.equal(attributes.role, 'tooltip');
  assert.equal(attributes['aria-describedby'], tip.id);
  assert.equal(button.title, undefined);
});
