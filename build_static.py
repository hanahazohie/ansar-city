"""Builds static/index.html from the artifact page.

The artifact version stores cars through claude.ai. This script wraps the same
page in a full HTML document and teaches it to talk to server.py instead, with
a TV mode (open the page with #main, or the older #tv) that fills the screen.

Usage: python build_static.py <path to crayon-city.html>
"""
import sys
from pathlib import Path

src = Path(sys.argv[1]).read_text("utf-8")
out_path = Path(__file__).resolve().parent / "static" / "index.html"
out_path.parent.mkdir(parents=True, exist_ok=True)


def replace_once(text, old, new):
    assert text.count(old) == 1, f"anchor not unique or missing: {old[:60]!r}"
    return text.replace(old, new)


def replace_between(text, start, end, new):
    i = text.index(start)
    j = text.index(end, i)
    return text[:i] + new + text[j:]


# 1. A full document head (the artifact tool adds one at publish time; here we must).
src = replace_once(
    src,
    '<title>Crayon City</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n',
    '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
    '<title>Crayon City</title>\n<style>[hidden]{display:none!important}img{max-width:100%}</style>\n',
)
src = replace_once(src, '</style>\n\n<div class="app">', '</style>\n</head>\n<body>\n<div class="app">')
src += "\n</body>\n</html>\n"

# 2. TV mode styles.
src = replace_once(
    src,
    "@media (prefers-reduced-motion:reduce)",
    "body.tv .top,body.tv .garage{display:none}\n"
    "body.tv .app{padding:0;gap:0}\n"
    "body.tv .stage{aspect-ratio:auto;height:100vh;max-height:none;min-height:0;border-radius:0}\n"
    ".tvstart{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(10,14,20,.5);z-index:5}\n"
    ".tvstart button{font-size:24px;padding:18px 32px}\n"
    "@media (prefers-reduced-motion:reduce)",
)

# 3. PIN field in the last wizard step.
src = replace_once(
    src,
    '<div class="ctrl field"><label class="lbl" for="nameIn">Name</label><input type="text" id="nameIn" maxlength="40" placeholder="Zoomy Blue"></div>',
    '<div class="ctrl field"><label class="lbl" for="nameIn">Name</label><input type="text" id="nameIn" maxlength="40" placeholder="Zoomy Blue"></div>\n'
    '          <div class="ctrl field" id="pinRow" hidden><label class="lbl" for="pinIn">PIN for this city</label><input type="text" id="pinIn" inputmode="numeric" autocomplete="off" maxlength="32" placeholder="Ask whoever runs the city"></div>',
)

# 4. Server adapter.
src = replace_once(
    src,
    "  localCars: [], examples: [],\n};\n",
    "  localCars: [], examples: [],\n};\n\n"
    "// ---------- Self-hosted server (server.py) ----------\n"
    "const server = { on: false, pinRequired: false, classify: false, pin: '' };\n"
    "const serverSeen = new Set();\n"
    "async function probeServer() {\n"
    "  try {\n"
    "    const r = await fetch('api/config', { cache: 'no-store' }); if (!r.ok) return false;\n"
    "    const c = await r.json(); server.on = true; server.pinRequired = !!c.pinRequired; server.classify = !!c.classify;\n"
    "    try { server.pin = localStorage.getItem('crayoncity.pin') || ''; } catch (e) {}\n"
    "    return true;\n"
    "  } catch (e) { return false; }\n"
    "}\n"
    "function pinHeaders() { return server.pin ? { 'X-Pin': server.pin } : {}; }\n"
    "async function pullServer() {\n"
    "  let cars;\n"
    "  try { const r = await fetch('api/cars', { cache: 'no-store' }); if (!r.ok) return; cars = await r.json(); } catch (e) { return; }\n"
    "  if (!Array.isArray(cars)) return;\n"
    "  const ids = new Set();\n"
    "  for (const c of cars) {\n"
    "    if (!c || typeof c.id !== 'string') continue; ids.add(c.id);\n"
    "    if (!serverSeen.has(c.id)) { serverSeen.add(c.id); showCar({ id: c.id, name: String(c.name || '').slice(0, 40), type: String(c.type || 'regular'), facing: c.facing, src: 'images/' + encodeURIComponent(String(c.image || '')), server: true, addedAt: String(c.addedAt || '') }); }\n"
    "  }\n"
    "  for (const id of [...serverSeen]) if (!ids.has(id)) { serverSeen.delete(id); hideCar(id); }\n"
    "  syncExamples();\n"
    "}\n",
)
src = replace_once(
    src,
    "local: !!rec.local, asset: rec.asset || null",
    "local: !!rec.local, server: !!rec.server, asset: rec.asset || null",
)

# 5. removeCar: server path.
src = replace_between(
    src,
    "async function removeCar(c) {",
    "async function boot() {",
    """async function removeCar(c) {
  if (c.local) {
    state.localCars = state.localCars.filter(r => r.id !== c.id); saveLocal(); hideCar(c.id); syncExamples(); toast('Removed from this device.'); return;
  }
  if (c.server) {
    try {
      const r = await fetch('api/cars/' + encodeURIComponent(c.id), { method: 'DELETE', headers: pinHeaders() });
      if (r.status === 401) { toast('Open "Add a car" and enter the PIN first.'); renderGarage(); return; }
      if (!r.ok) throw new Error('delete failed');
      toast('Removed from the city.'); await pullServer();
    } catch (e) { toast('Could not remove it. Is the server running?'); renderGarage(); }
    return;
  }
  try {
    await state.db.collection('cars').doc(c.id).delete();
    if (c.asset && state.assets) { try { await state.assets.delete(c.asset); } catch (e) { /* leaves an orphan asset; harmless */ } }
    toast('Removed from the city.');
  } catch (e) { toast('Could not remove it. You may not have permission.'); renderGarage(); }
}
""",
)

# 6. boot: try claude.ai first, then the server, then this device.
src = replace_between(
    src,
    "async function boot() {",
    "// ---------- Cut the drawing off the paper ----------",
    """async function boot() {
  resize();
  loadLocal();
  const db = await use('db');
  if (db) {
    state.db = db; state.storeMode = 'db';
    db.collection('cars').orderBy('addedAt').onSnapshot(snap => {
      for (const ch of snap.docChanges()) {
        const d = ch.doc.data() || {};
        if (ch.type === 'removed') hideCar(ch.doc.id);
        else if (ch.type === 'added' && d.asset) showCar({ id: ch.doc.id, name: String(d.name || '').slice(0, 40), type: String(d.type || 'regular'), facing: d.facing, src: '/_blob/' + String(d.asset), asset: String(d.asset), addedAt: String(d.addedAt || '') });
      }
      syncExamples();
    }, err => { toast('The shared garage is not reachable right now.'); });
  } else if (await probeServer()) {
    state.storeMode = 'server';
    $('#garageNote').textContent = server.pinRequired ? 'Everyone who opens this address sees the same city. Adding a car needs the PIN.' : 'Everyone who opens this address sees the same city.';
    await pullServer();
    setInterval(pullServer, 8000);
  } else {
    state.storeMode = 'local';
    $('#storeChip').hidden = false;
    $('#garageNote').textContent = 'No server found, so cars stay on this device.';
  }
  for (const r of state.localCars) showCar({ id: r.id, name: r.name, type: r.type, facing: r.facing, src: r.img, local: true, addedAt: r.addedAt });
  if (state.localCars.length && state.storeMode !== 'local') $('#storeChip').hidden = false;
  syncExamples();
  state.assets = await use('assets');
  state.sample = await use('sample');
  if (state.sample) { try { const lim = await state.sample.limits(); state.sampleImages = !!(lim && lim.images); } catch (e) { state.sampleImages = false; } }
}

""",
)

# 7. startGuess: ask the server (which asks Claude) or the artifact runtime.
src = replace_between(
    src,
    "async function startGuess() {",
    "async function saveCar() {",
    """async function startGuess() {
  const g = $('#guess'); g.textContent = '';
  $('#pinRow').hidden = !(server.on && server.pinRequired && !server.pin);
  if (!wiz.cut) return;
  const viaServer = server.on && server.classify;
  if (!viaServer && !(state.sample && state.sampleImages)) return;
  if (viaServer && server.pinRequired && !server.pin) { g.textContent = 'Enter the PIN and Claude will guess what this is.'; return; }
  if (wiz.guessCtl) wiz.guessCtl.abort();
  const ctl = new AbortController(); wiz.guessCtl = ctl;
  g.innerHTML = '<span class="dot"></span><span>Asking Claude what this is…</span>';
  try {
    const blob = await new Promise(r => wiz.cut.toBlob(r, 'image/png'));
    let ans;
    if (viaServer) {
      const fd = new FormData(); fd.append('image', blob, 'car.png');
      const r = await fetch('api/classify', { method: 'POST', body: fd, headers: pinHeaders(), signal: ctl.signal });
      if (r.status === 401) { $('#pinRow').hidden = false; g.textContent = 'That PIN is not right.'; return; }
      if (!r.ok) throw { code: 'server' };
      ans = await r.json();
    } else {
      const prompt = 'This image is a child\\'s colored drawing of a vehicle, cut out from the paper. ' +
        'Reply with only a JSON object like {"type":"police","facing":"right","name":"Blue Flash"}. ' +
        '"type" must be one of: ' + TYPE_KEYS.join(', ') + ' (use "regular" for an ordinary car, "other" if it is not a road vehicle). ' +
        '"facing" is "left" or "right": the side the front of the vehicle (hood, headlights, windshield) points to. ' +
        '"name" is a fun, kind, two-word name for this car that a child would love, based on its colors.';
      ans = await state.sample.json(prompt, { images: blob, modelTier: 'quick', signal: ctl.signal });
    }
    if (ctl.signal.aborted) return;
    const type = TYPE_KEYS.includes(ans?.type) ? ans.type : null;
    if (type) selectType(type);
    if (ans?.facing === 'left' || ans?.facing === 'right') { wiz.facing = ans.facing; drawFinal(); }
    if (typeof ans?.name === 'string' && ans.name.trim() && !$('#nameIn').value) { wiz.name = ans.name.trim().slice(0, 40); $('#nameIn').value = wiz.name; }
    g.textContent = type ? `Claude thinks it is a ${TYPES[type].label.toLowerCase()}. Change it if that is wrong.` : 'Claude was not sure. Pick the type yourself.';
  } catch (e) {
    if (e && (e.code === 'cancelled' || e.name === 'AbortError')) return;
    g.textContent = e && e.code === 'not_granted' ? '' : 'Claude could not look at it. Pick the type yourself.';
  } finally { if (wiz.guessCtl === ctl) wiz.guessCtl = null; }
}
""",
)

# 8. saveCar: server upload, then the artifact store, then this device. Plus TV mode.
src = replace_between(
    src,
    "async function saveCar() {",
    "boot();\n})();",
    """async function saveCar() {
  if (wiz.busy || !wiz.cut) return;
  wiz.busy = true; const btn = $('#nextBtn'); btn.disabled = true; btn.textContent = 'Parking…';
  const idle = () => { wiz.busy = false; btn.disabled = false; btn.textContent = 'Add to the city'; };
  const name = (wiz.name || $('#nameIn').value || '').trim().slice(0, 40) || TYPES[wiz.type].label;
  const rec = { name, type: wiz.type, facing: wiz.facing, addedAt: new Date().toISOString() };
  let saved = false;
  if (server.on) {
    try {
      const typed = $('#pinIn').value.trim(); if (typed) server.pin = typed;
      const blob = await new Promise(r => wiz.cut.toBlob(r, 'image/png'));
      const fd = new FormData(); fd.append('image', blob, 'car.png'); fd.append('name', name); fd.append('type', wiz.type); fd.append('facing', wiz.facing);
      const r = await fetch('api/cars', { method: 'POST', body: fd, headers: pinHeaders() });
      if (r.status === 401) { $('#pinRow').hidden = false; toast(server.pin ? 'That PIN is not right.' : 'This city needs a PIN to add cars.'); server.pin = ''; idle(); return; }
      if (!r.ok) throw new Error('upload failed');
      try { localStorage.setItem('crayoncity.pin', server.pin); } catch (e) {}
      saved = true; toast(`${name} is on the road!`); pullServer();
    } catch (e) { saved = false; }
  }
  if (!saved && state.db && state.assets) {
    try {
      const blob = await new Promise(r => wiz.cut.toBlob(r, 'image/png'));
      const up = await state.assets.upload(blob, { type: 'image/png' });
      await state.db.collection('cars').add(Object.assign({ asset: up.id, w: wiz.cut.width, h: wiz.cut.height }, rec));
      saved = true; toast(`${name} is on the road!`);
    } catch (e) { saved = false; }
  }
  if (!saved) {
    const small = downscale(wiz.cut, 480, 320);
    let img = small.toDataURL('image/webp', 0.85); if (!img.startsWith('data:image/webp')) img = small.toDataURL('image/png');
    const local = Object.assign({ id: 'local:' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), img }, rec);
    state.localCars.push(local);
    const ok = saveLocal();
    $('#storeChip').hidden = false;
    showCar({ id: local.id, name: local.name, type: local.type, facing: local.facing, src: img, local: true, addedAt: local.addedAt }).then(syncExamples);
    toast(ok ? `${name} is on the road (saved on this device).` : `${name} is driving, but this device could not save it.`);
  }
  idle();
  closeWiz();
}

// ---------- TV mode: open the page with #main (or #tv) ----------
function applyTvMode() {
  const on = location.hash === '#main' || location.hash === '#tv';
  document.body.classList.toggle('tv', on);
  const old = stage.querySelector('.tvstart'); if (old) old.remove();
  if (on) {
    const ov = document.createElement('div'); ov.className = 'tvstart';
    const b = document.createElement('button'); b.className = 'btn primary'; b.textContent = 'Start the city'; ov.append(b); stage.append(ov);
    b.addEventListener('click', () => {
      ov.remove(); cam.tour = true;
      if (!state.sound) $('#soundBtn').click();
      if (!state.night && new Date().getHours() >= 19) $('#nightBtn').click();
      const p = stage.requestFullscreen ? stage.requestFullscreen() : null;
      Promise.resolve(p).catch(() => {});
      setTimeout(resize, 100);
    });
  }
  setTimeout(resize, 50);
}
applyTvMode();
window.addEventListener('hashchange', applyTvMode);

""",
)

out_path.write_text(src, "utf-8")
print(f"wrote {out_path} ({len(src)} bytes)")
