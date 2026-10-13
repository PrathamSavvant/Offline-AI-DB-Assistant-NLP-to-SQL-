(() => {
  const chat = location.pathname === '/'; document.body.classList.toggle('app-chat', chat);
  const toggle = document.getElementById('navToggle');
  function compact(value) {
    document.body.classList.toggle('app-nav-compact', value);
    toggle.textContent = value ? '›' : '‹';
    toggle.setAttribute('aria-expanded', String(!value));
    toggle.setAttribute('aria-label', value ? 'Expand navigation' : 'Collapse navigation');
    try { localStorage.setItem('navigation-compact', String(value)); } catch (_) {}
  }
  try { compact(localStorage.getItem('navigation-compact') === 'true'); } catch (_) {}
  toggle.onclick = () => compact(!document.body.classList.contains('app-nav-compact'));
  const exitButton = document.getElementById('exitApplication');
  if (exitButton) exitButton.addEventListener('click', async () => {
    const bundled = exitButton.dataset.exitMode === 'bundle';
    const detail = bundled ? 'This will stop the application and any local model started by the desktop shortcut.' : 'This will stop the application server. A separately started local model will keep running.';
    if (!window.confirm('Exit AI Database Assistant? ' + detail)) return;
    exitButton.disabled = true;
    const overlay = document.createElement('div');
    overlay.className = 'app-exit-overlay';
    overlay.setAttribute('role', 'status');
    overlay.textContent = 'Stopping the application…';
    document.body.appendChild(overlay);
    try {
      const response = await fetch('/api/offline/exit', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-Exit-Token': exitButton.dataset.exitToken},
        body: '{}'
      });
      if (!response.ok) {
        const result = await response.json();
        throw new Error(result.error || 'The application could not be stopped.');
      }
      overlay.textContent = bundled
        ? 'Shutdown requested. You can close this tab. The local model will stop if this bundle started it.'
        : 'Application shutdown requested. You can close this tab. Any separately started local model remains running.';
    } catch (error) {
      overlay.remove();
      exitButton.disabled = false;
      window.alert(error.message + ' If it is still running, use the Stop desktop shortcut.');
    }
  });
  if (!chat) fetch('/api/database/registry').then(r => { if (!r.ok) throw Error(); return r.json(); })
    .then(d => { document.getElementById('dbBadge').textContent = d.database_name || 'Local workspace'; }).catch(() => {});
  if (chat) {
    document.querySelectorAll('.prompt-card').forEach(card => {
      card.setAttribute('role', 'button'); card.tabIndex = 0;
      card.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); card.click(); } });
    });
    document.querySelector('.app-connect').onclick = event => { event.preventDefault(); openConnModal(); };
    if (new URLSearchParams(location.search).get('connection') === '1') openConnModal();
  }
})();
