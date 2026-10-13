/* Offline schema explorer. All schema/knowledge text is inserted as text, never HTML. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), ns = 'http://www.w3.org/2000/svg';
  const panel = $('schemaMap'), canvas = $('mapCanvas'), stage = $('mapStage');
  if (!panel) return;
  const W = 282, H = 150, DX = 360, DY = 210;
  let data = null, nodes = new Map(), selected = null, visible = new Set();
  let scale = 1, tx = 0, ty = 0, drag = null, sequence = 0, frame = 0, viewKey = null;
  const svg = (tag, attrs = {}, text) => {
    const el = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (text !== undefined) el.textContent = text;
    return el;
  };
  function html(tag, text, className, parent) {
    const el = document.createElement(tag); el.textContent = text;
    if (className) el.className = className;
    if (parent) parent.append(el);
    return el;
  }
  const short = (text, length) => text.length > length ? text.slice(0, length - 1) + '…' : text;
  function transform() {
    $('mapViewport').setAttribute('transform', `translate(${tx},${ty}) scale(${scale})`);
    $('mapZoom').textContent = Math.round(scale * 100) + '%';
  }
  function zoom(factor, x = stage.clientWidth / 2, y = stage.clientHeight / 2) {
    const next = Math.min(2.5, Math.max(.025, scale * factor));
    tx = x - (x - tx) * next / scale; ty = y - (y - ty) * next / scale;
    scale = next; transform();
  }
  function fit() {
    const list = [...nodes.values()].filter(n => visible.has(n.id));
    if (!list.length || !stage.clientWidth) return;
    const minX = Math.min(...list.map(n => n.x)) - 45, minY = Math.min(...list.map(n => n.y)) - 55;
    const width = Math.max(...list.map(n => n.x + W)) - minX + 70;
    const height = Math.max(...list.map(n => n.y + H)) - minY + 80;
    scale = Math.max(.025, Math.min(1, stage.clientWidth / width, stage.clientHeight / height));
    tx = (stage.clientWidth - width * scale) / 2 - minX * scale;
    ty = (stage.clientHeight - height * scale) / 2 - minY * scale; transform();
  }
  function layout() {
    viewKey = null;
    // Group connected components; breadth-first order keeps related cards nearby.
    // Fixed cells guarantee no overlaps, with no force simulation on the UI thread.
    const adjacency = new Map(data.nodes.map(n => [n.id, new Set()]));
    data.edges.forEach(e => { adjacency.get(e.source).add(e.target); adjacency.get(e.target).add(e.source); });
    const remaining = new Set(adjacency.keys()), components = [];
    const order = [...remaining].sort((a, b) => adjacency.get(b).size - adjacency.get(a).size || a.localeCompare(b));
    for (const root of order) {
      if (!remaining.delete(root)) continue;
      const queue = [root];
      for (let i = 0; i < queue.length; i++) {
        for (const next of [...adjacency.get(queue[i])].sort()) if (remaining.delete(next)) queue.push(next);
      }
      components.push(queue);
    }
    const targetWidth = Math.max(3, Math.ceil(Math.sqrt(data.nodes.length * 1.5))) * DX;
    let left = 0, top = 0, shelf = 0;
    for (const component of components.sort((a, b) => b.length - a.length)) {
      const cols = Math.ceil(Math.sqrt(component.length * 1.5));
      const width = Math.min(cols, component.length) * DX, height = Math.ceil(component.length / cols) * DY;
      if (left && left + width > targetWidth) { left = 0; top += shelf + 55; shelf = 0; }
      component.forEach((id, i) => {
        const n = nodes.get(id); n.x = left + i % cols * DX; n.y = top + Math.floor(i / cols) * DY;
        n.homeX = n.x; n.homeY = n.y;
      });
      left += width + 50; shelf = Math.max(shelf, height);
    }
  }
  function path(e, index) {
    const a = nodes.get(e.source), b = nodes.get(e.target);
    if (a === b) return `M${a.x + W - 45},${a.y} C${a.x + W + 90},${a.y - 100} ${a.x + W + 100},${a.y + 95} ${a.x + W},${a.y + 75}`;
    const ax = a.x + W / 2, ay = a.y + H / 2, bx = b.x + W / 2, by = b.y + H / 2;
    const dx = bx - ax, dy = by - ay, bend = (index % 3 - 1) * 12;
    if (Math.abs(dx) / W >= Math.abs(dy) / H) {
      const sign = dx >= 0 ? 1 : -1, x1 = ax + sign * W / 2, x2 = bx - sign * W / 2;
      const curve = Math.max(60, Math.abs(x2 - x1) * .5);
      return `M${x1},${ay + bend} C${x1 + sign * curve},${ay + bend} ${x2 - sign * curve},${by + bend} ${x2},${by + bend}`;
    }
    const sign = dy >= 0 ? 1 : -1, y1 = ay + sign * H / 2, y2 = by - sign * H / 2;
    const curve = Math.max(45, Math.abs(y2 - y1) * .5);
    return `M${ax + bend},${y1} C${ax + bend},${y1 + sign * curve} ${bx + bend},${y2 - sign * curve} ${bx + bend},${y2}`;
  }
  function moveDrawing() {
    frame = 0;
    for (const n of nodes.values()) if (n.el) n.el.setAttribute('transform', `translate(${n.x},${n.y})`);
    data.edges.forEach((e, i) => { if (e.el) e.el.setAttribute('d', path(e, i)); });
  }
  function relationText(e) {
    return `${e.source} (${e.source_columns.join(', ')}) → ${e.target} (${e.target_columns.join(', ')})`;
  }
  function draw() {
    const nf = document.createDocumentFragment(), ef = document.createDocumentFragment();
    for (const n of nodes.values()) {
      const g = svg('g', {class: 'map-node', tabindex: 0, role: 'button', 'aria-label': `Inspect ${n.name}`, 'data-node': n.id});
      g.append(svg('rect', {width: W, height: H, rx: 11}), svg('title', {}, n.name));
      g.append(svg('text', {x: 16, y: 27, class: 'node-name'}, short(n.name, 29)));
      g.append(svg('text', {x: 16, y: 46, class: 'node-meta'}, `${n.columns.length} columns · ${short(n.domain, 27)}`));
      g.append(svg('line', {x1: 0, y1: 59, x2: W, y2: 59, stroke: '#2b3b50'}));
      n.abbreviations.slice(0, 3).forEach((a, i) => g.append(svg('text', {x: 16, y: 81 + 19 * i, class: 'node-abbr'}, short(`${a.token} · ${a.meaning}`, 37))));
      if (!n.abbreviations.length) g.append(svg('text', {x: 16, y: 88, class: 'node-empty'}, 'No approved abbreviations'));
      g.append(svg('text', {x: 16, y: 139, class: 'node-meta'}, n.abbreviations.length > 3 ? `+${n.abbreviations.length - 3} more · Select to explore` : 'Select to explore connections'));
      g.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(n.id); } });
      n.el = g; nf.append(g);
    }
    data.edges.forEach(e => {
      const p = svg('path', {class: `map-edge ${e.kind}`, 'marker-end': 'url(#mapArrow)', tabindex: 0, role: 'button', 'aria-label': relationText(e)});
      p.append(svg('title', {}, relationText(e)));
      p.addEventListener('click', () => inspectEdge(e));
      p.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); inspectEdge(e); } });
      e.el = p; ef.append(p);
    });
    $('mapNodes').replaceChildren(nf); $('mapEdges').replaceChildren(ef); moveDrawing(); filter();
  }
  function filter() {
    if (!data) return;
    $('mapNeighbors').disabled = !selected;
    if (!selected) $('mapNeighbors').checked = false;
    const query = $('mapSearch').value.trim().toLowerCase(), domain = $('mapDomain').value;
    const adjacent = new Set(selected ? [selected] : []);
    data.edges.forEach(e => { if (e.source === selected) adjacent.add(e.target); if (e.target === selected) adjacent.add(e.source); });
    visible = new Set();
    for (const n of nodes.values()) {
      const match = (!query || n.search.includes(query)) && (!domain || n.domain === domain) && (!$('mapNeighbors').checked || !selected || adjacent.has(n.id));
      n.el.style.display = match ? '' : 'none';
      n.el.classList.toggle('selected', n.id === selected);
      n.el.classList.toggle('neighbor', adjacent.has(n.id) && n.id !== selected);
      n.el.classList.toggle('map-dim', !!selected && !adjacent.has(n.id));
      if (match) visible.add(n.id);
    }
    let count = 0;
    data.edges.forEach(e => {
      const show = visible.has(e.source) && visible.has(e.target); e.el.style.display = show ? '' : 'none'; if (show) count++;
      const active = e.source === selected || e.target === selected;
      e.el.classList.toggle('active', active); e.el.classList.toggle('map-dim', !!selected && !active);
    });
    const nextKey = [...visible].join('\0');
    if (nextKey !== viewKey) {
      viewKey = nextKey;
      const list = [...nodes.values()].filter(n => visible.has(n.id));
      if (visible.size < nodes.size) {
        // A filtered neighborhood needs its own compact layout; retaining the
        // full schema's coordinates can leave four cards thousands of pixels apart.
        const cols = Math.max(1, Math.ceil(Math.sqrt(list.length * stage.clientWidth / Math.max(1, stage.clientHeight) * DY / DX)));
        list.forEach((n, i) => { n.x = i % cols * DX; n.y = Math.floor(i / cols) * DY; });
      } else {
        list.forEach(n => { n.x = n.homeX; n.y = n.homeY; });
      }
      moveDrawing();
    }
    $('mapMessage').hidden = visible.size > 0;
    $('mapMessage').textContent = data.nodes.length ? 'No matching tables. Clear the search or change the filters.' : 'No tables are cached yet. Connect to a database and complete a schema scan.';
    $('mapVisible').textContent = `${visible.size} / ${data.nodes.length} tables · ${count} connections shown`;
  }
  function resetInspector() {
    const root = $('mapInspector'); root.replaceChildren();
    html('h3', 'Explore your database', '', root); html('p', 'Select a table to see its knowledge, columns and connections.', '', root);
  }
  function select(id, center = false) {
    selected = id; filter();
    const n = nodes.get(id), root = $('mapInspector'); root.replaceChildren();
    html('span', n.domain, 'map-badge', root); html('h3', n.name, '', root);
    html('p', `${n.columns.length} columns · ${n.row_count == null ? 'Row count unavailable' : Number(n.row_count).toLocaleString() + ' estimated rows'}`, '', root);
    html('p', n.definition || 'No table context has been approved yet.', '', root);
    const link = html('a', 'Open defined knowledge ↗', '', root); link.href = '/defined-knowledge/' + encodeURIComponent(n.id);
    html('h4', `Approved abbreviations (${n.abbreviations.length})`, '', root);
    if (!n.abbreviations.length) html('p', 'No approved abbreviations.', '', root);
    n.abbreviations.forEach(a => { const row = html('div', '', 'map-detail-row', root); html('strong', a.token, '', row); html('span', a.meaning, '', row); });
    const edges = data.edges.filter(e => e.source === id || e.target === id);
    html('h4', `Connections (${edges.length})`, '', root);
    if (!edges.length) html('p', 'No foreign keys or approved business relationships connect this table.', '', root);
    edges.forEach(e => {
      const other = e.source === id ? e.target : e.source;
      const button = html('button', other === id ? `${other} (self-reference)` : other, 'map-connection', root); button.type = 'button';
      html('small', relationText(e), '', button); html('small', e.kind === 'foreign_key' ? 'Database foreign key' : 'Approved business relationship', '', button);
      button.onclick = () => { $('mapSearch').value = ''; $('mapDomain').value = ''; select(other, true); };
    });
    html('h4', `Columns (${n.columns.length})`, '', root);
    const fks = new Set(data.edges.filter(e => e.source === id && e.kind === 'foreign_key').flatMap(e => e.source_columns));
    n.columns.forEach(c => { const row = html('div', '', 'map-detail-row', root); html('strong', `${c.primary_key ? 'PK · ' : ''}${fks.has(c.name) ? 'FK · ' : ''}${c.name}`, '', row); html('span', `${c.type}${c.nullable === true ? ' · nullable' : ''}`, '', row); });
    if (center) { scale = .9; tx = stage.clientWidth / 2 - (n.x + W / 2) * scale; ty = stage.clientHeight / 2 - (n.y + H / 2) * scale; transform(); }
  }
  function inspectEdge(e) {
    const root = $('mapInspector'); root.replaceChildren();
    html('span', e.kind === 'foreign_key' ? 'Database foreign key' : 'Approved business relationship', 'map-badge', root);
    html('h3', 'Table connection', '', root); html('p', e.constraint, '', root);
    e.source_columns.forEach((c, i) => html('div', `${e.source}.${c} → ${e.target}.${e.target_columns[i]}`, 'map-detail-row', root));
    if (e.description) html('p', e.description, '', root);
    [e.source, e.target].filter((x, i, a) => a.indexOf(x) === i).forEach(id => { const b = html('button', `Explore ${id}`, 'map-connection', root); b.onclick = () => select(id, true); });
    data.edges.forEach(other => other.el.classList.toggle('active', other === e));
  }
  async function load() {
    const current = ++sequence; $('mapMessage').hidden = false; $('mapMessage').textContent = 'Loading schema…';
    $('mapRefresh').disabled = true;
    try {
      const response = await fetch('/api/schema-map', {cache: 'no-store'}), result = await response.json();
      if (current !== sequence) return;
      if (!response.ok) throw new Error(result.error || 'Could not load the schema.');
      data = result; selected = null; nodes = new Map(data.nodes.map(n => [n.id, {...n,
        search: [n.name, n.definition, ...n.columns.map(c => c.name), ...n.abbreviations.flatMap(a => [a.token, a.meaning])].join(' ').toLowerCase()}]));
      $('mapTitle').textContent = data.database || 'Schema map';
      $('mapCounts').textContent = `${data.counts.tables} tables · ${data.counts.relationships} connections · ${data.counts.isolated} isolated`;
      const domain = $('mapDomain'); domain.replaceChildren(new Option('All domains', ''));
      [...new Set(data.nodes.map(n => n.domain))].sort().forEach(d => domain.add(new Option(d, d)));
      $('mapNeighbors').checked = false; resetInspector(); layout(); draw(); fit();
    } catch (error) {
      if (current !== sequence) return;
      data = null; nodes.clear(); $('mapNodes').replaceChildren(); $('mapEdges').replaceChildren(); resetInspector();
      $('mapTitle').textContent = 'Schema map'; $('mapCounts').textContent = ''; $('mapVisible').textContent = '';
      $('mapMessage').hidden = false; $('mapMessage').textContent = error.message + ' Use Refresh to retry.';
    } finally { if (current === sequence) $('mapRefresh').disabled = false; }
  }
  $('mapRefresh').onclick = load;
  let debounce; $('mapSearch').oninput = () => { clearTimeout(debounce); debounce = setTimeout(() => { selected = null; resetInspector(); filter(); fit(); }, 150); };
  $('mapDomain').onchange = () => { filter(); fit(); };
  $('mapNeighbors').onchange = () => {
    if ($('mapNeighbors').checked) { $('mapSearch').value = ''; $('mapDomain').value = ''; }
    filter(); fit();
  };
  $('mapZoomIn').onclick = () => zoom(1.25); $('mapZoomOut').onclick = () => zoom(.8); $('mapFit').onclick = fit;
  $('mapReset').onclick = () => { if (!data) return; selected = null; $('mapSearch').value = ''; $('mapDomain').value = ''; $('mapNeighbors').checked = false; resetInspector(); layout(); moveDrawing(); filter(); fit(); };
  canvas.addEventListener('wheel', e => { e.preventDefault(); const r = canvas.getBoundingClientRect(); zoom(Math.exp(-e.deltaY * .0015), e.clientX - r.left, e.clientY - r.top); }, {passive: false});
  canvas.addEventListener('pointerdown', e => {
    if (e.button !== 0 || !data || e.target.closest('.map-edge')) return;
    const node = e.target.closest('[data-node]'), n = node ? nodes.get(node.dataset.node) : null;
    drag = {id: e.pointerId, startX: e.clientX, startY: e.clientY, x: n ? n.x : tx, y: n ? n.y : ty, node: n, moved: false};
    canvas.setPointerCapture(e.pointerId); canvas.classList.add('dragging');
  });
  canvas.addEventListener('pointermove', e => {
    if (!drag || e.pointerId !== drag.id) return;
    const dx = e.clientX - drag.startX, dy = e.clientY - drag.startY; if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
    if (drag.node) { drag.node.x = drag.x + dx / scale; drag.node.y = drag.y + dy / scale; if (!frame) frame = requestAnimationFrame(moveDrawing); }
    else { tx = drag.x + dx; ty = drag.y + dy; transform(); }
  });
  canvas.addEventListener('pointerup', e => {
    if (!drag || drag.id !== e.pointerId) return;
    const previous = drag; drag = null; canvas.classList.remove('dragging'); canvas.releasePointerCapture(e.pointerId);
    if (!previous.moved) { if (previous.node) select(previous.node.id); else { selected = null; filter(); resetInspector(); } }
  });
  canvas.addEventListener('pointercancel', () => { drag = null; canvas.classList.remove('dragging'); });
  canvas.addEventListener('keydown', e => {
    if (e.target !== canvas) return;
    if (e.key === '+' || e.key === '=') zoom(1.25); else if (e.key === '-') zoom(.8);
    else if (e.key === '0') fit();
    else if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) { e.preventDefault(); tx += e.key === 'ArrowLeft' ? 50 : e.key === 'ArrowRight' ? -50 : 0; ty += e.key === 'ArrowUp' ? 50 : e.key === 'ArrowDown' ? -50 : 0; transform(); }
  });
  new ResizeObserver(() => { if (data && !panel.hidden && !drag) fit(); }).observe(stage);
  window.addEventListener('schema-refreshed', () => { data = null; if (!panel.hidden) load(); });
  load();
})();
