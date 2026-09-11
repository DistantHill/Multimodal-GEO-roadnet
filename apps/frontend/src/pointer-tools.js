export function isMiddleButton(event) {
  return event.originalEvent.button === 1;
}

export function isPrimaryButton(event) {
  return event.originalEvent.button === 0;
}

export function installToolbarTooltips(root) {
  root.querySelectorAll('.tool-button[title]').forEach((button, index) => {
    const tooltip = button.ownerDocument.createElement('span');
    tooltip.className = 'tool-tooltip';
    tooltip.id = `tool-tooltip-${index}`;
    tooltip.setAttribute('role', 'tooltip');
    tooltip.textContent = button.title;
    button.removeAttribute('title');
    button.setAttribute('aria-describedby', tooltip.id);
    button.append(tooltip);
  });
}
