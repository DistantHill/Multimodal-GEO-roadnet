if (location.hash.startsWith('#experiments')) {
  await import('./experiments.js');
} else {
  await import('./main.js');
}
window.addEventListener('hashchange', () => location.reload());
