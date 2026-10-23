// Crayon City on Cloudflare Workers: the same API as server.py, backed by a Workers KV namespace.
//   GET    /api/config            { pinRequired, classify }
//   GET    /api/cars              list of cars
//   POST   /api/cars              multipart: image, name, type, facing   (X-Pin header)
//   DELETE /api/cars/:id                                                 (X-Pin header)
//   POST   /api/classify          multipart: image -> { type, facing, name } from Claude
//   GET    /images/:file          the cutout PNG/JPEG/WebP
//   everything else               public/index.html via the ASSETS binding
//
// KV layout: cars.json (the list) and images/<id>.<ext> (bytes, content type in the metadata).
// KV is eventually consistent: a new car can take up to a minute to show on a screen far from the phone that added it.
// Secrets: CRAYON_PIN (optional), ANTHROPIC_API_KEY (optional).

const TYPES = ['police', 'fire', 'ambulance', 'taxi', 'race', 'bus', 'truck', 'regular', 'other'];
const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
const EXT = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp' };
const PROMPT =
  "This image is a child's colored drawing of a vehicle, cut out from the paper. " +
  'Reply with only a JSON object like {"type":"police","facing":"right","name":"Blue Flash"}. ' +
  '"type" must be one of: ' + TYPES.join(', ') + ' (use "regular" for an ordinary car, ' +
  '"other" if it is not a road vehicle). "facing" is "left" or "right": the side the front of ' +
  'the vehicle (hood, headlights, windshield) points to. "name" is a fun, kind, two-word name ' +
  'for this car that a child would love, based on its colors.';

class HttpError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' } });

// ---------- helpers ----------
function sniff(b) {
  if (b.length > 8 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47 && b[4] === 0x0d && b[5] === 0x0a && b[6] === 0x1a && b[7] === 0x0a) return 'image/png';
  if (b.length > 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'image/jpeg';
  if (b.length > 12 && b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 && b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50) return 'image/webp';
  return null;
}

function sameString(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(a), y = enc.encode(b);
  if (x.length !== y.length) return false;
  return crypto.subtle.timingSafeEqual(x, y);
}

function checkPin(env, request) {
  const pin = (env.CRAYON_PIN || '').trim();
  if (!pin) return;
  const given = (request.headers.get('x-pin') || '').trim();
  if (!sameString(given, pin)) throw new HttpError(401, 'PIN required');
}

async function readForm(request) {
  try { return await request.formData(); }
  catch (e) { throw new HttpError(400, 'Expected a multipart form with an image'); }
}

async function readImage(form) {
  const file = form.get('image');
  if (!file || typeof file === 'string') throw new HttpError(400, 'Missing image');
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (!bytes.length) throw new HttpError(400, 'Empty image');
  if (bytes.length > MAX_IMAGE_BYTES) throw new HttpError(413, 'Image larger than 4 MB');
  const media = sniff(bytes);
  if (!media) throw new HttpError(415, 'Only PNG, JPEG or WebP images');
  return { bytes, media };
}

async function loadCars(env) {
  const cars = await env.CARS.get('cars.json', { type: 'json', cacheTtl: 60 });
  return Array.isArray(cars) ? cars : [];
}

async function saveCars(env, cars) {
  await env.CARS.put('cars.json', JSON.stringify(cars));
}

function randomId() {
  const b = new Uint8Array(8); crypto.getRandomValues(b);
  return [...b].map(x => x.toString(16).padStart(2, '0')).join('');
}

function toBase64(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// ---------- Claude ----------
async function askClaude(env, bytes, media) {
  const body = {
    model: 'claude-opus-5-5',
    max_tokens: 256,
    output_config: { effort: 'low' },
    messages: [{ role: 'user', content: [
      { type: 'image', source: { type: 'base64', media_type: media, data: toBase64(bytes) } },
      { type: 'text', text: PROMPT },
    ] }],
  };
  const headers = { 'content-type': 'application/json', 'x-api-key': env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01' };
  // Server-side fallback first (a safety classifier that declines re-runs on another model); plain request if the beta is rejected.
  let r = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST', headers: { ...headers, 'anthropic-beta': 'server-side-fallback-2026-07-01' }, body: JSON.stringify({ ...body, fallbacks: 'default' }),
  });
  if (r.status === 400) r = await fetch('https://api.anthropic.com/v1/messages', { method: 'POST', headers, body: JSON.stringify(body) });
  if (!r.ok) throw new Error('Claude HTTP ' + r.status + ': ' + (await r.text()).slice(0, 300));
  const res = await r.json();
  if (res.stop_reason === 'refusal') return {};
  const text = (res.content || []).filter(b => b.type === 'text').map(b => b.text).join('');
  const m = text.match(/\{[\s\S]*\}/);
  if (!m) return {};
  let parsed; try { parsed = JSON.parse(m[0]); } catch (e) { return {}; }
  const out = {};
  if (TYPES.includes(parsed.type)) out.type = parsed.type;
  if (parsed.facing === 'left' || parsed.facing === 'right') out.facing = parsed.facing;
  if (typeof parsed.name === 'string' && parsed.name.trim()) out.name = parsed.name.trim().slice(0, 40);
  return out;
}

// ---------- routes ----------
async function api(request, env, url) {
  const path = url.pathname, method = request.method;
  const classify = !!env.ANTHROPIC_API_KEY;

  if (path === '/api/config' && method === 'GET') return json({ pinRequired: !!(env.CRAYON_PIN || '').trim(), classify });

  if (path === '/api/cars' && method === 'GET') {
    const cars = await loadCars(env);
    cars.sort((a, b) => String(a.addedAt || '').localeCompare(String(b.addedAt || '')));
    return json(cars);
  }

  if (path === '/api/cars' && method === 'POST') {
    checkPin(env, request);
    const form = await readForm(request);
    const { bytes, media } = await readImage(form);
    const id = randomId(), filename = id + '.' + EXT[media];
    await env.CARS.put('images/' + filename, bytes, { metadata: { contentType: media } });
    const type = String(form.get('type') || 'regular');
    const car = {
      id, name: String(form.get('name') || '').trim().slice(0, 40),
      type: TYPES.includes(type) ? type : 'regular',
      facing: form.get('facing') === 'left' ? 'left' : 'right',
      image: filename, addedAt: new Date().toISOString(),
    };
    const cars = await loadCars(env); cars.push(car); await saveCars(env, cars);
    return json(car);
  }

  const del = path.match(/^\/api\/cars\/([^/]+)$/);
  if (del && method === 'DELETE') {
    checkPin(env, request);
    const id = decodeURIComponent(del[1]);
    const cars = await loadCars(env);
    const gone = cars.filter(c => c && c.id === id);
    if (!gone.length) throw new HttpError(404, 'No such car');
    await saveCars(env, cars.filter(c => !(c && c.id === id)));
    for (const c of gone) if (c.image) await env.CARS.delete('images/' + String(c.image).split('/').pop());
    return json({ ok: true });
  }

  if (path === '/api/classify' && method === 'POST') {
    checkPin(env, request); // strangers should not be able to spend your Claude credits
    if (!classify) throw new HttpError(503, 'Classification is off (no ANTHROPIC_API_KEY)');
    const form = await readForm(request);
    const { bytes, media } = await readImage(form);
    try { return json(await askClaude(env, bytes, media)); }
    catch (e) { console.log('classify failed: ' + e.message); throw new HttpError(502, 'Claude did not answer'); }
  }

  throw new HttpError(404, 'Not found');
}

async function image(env, url) {
  const name = decodeURIComponent(url.pathname.slice('/images/'.length)).split('/').pop();
  if (!name) return new Response('Not found', { status: 404 });
  const { value, metadata } = await env.CARS.getWithMetadata('images/' + name, { type: 'stream', cacheTtl: 3600 });
  if (!value) return new Response('Not found', { status: 404 });
  const type = (metadata && metadata.contentType) || 'image/' + (name.endsWith('.png') ? 'png' : name.endsWith('.webp') ? 'webp' : 'jpeg');
  return new Response(value, { headers: { 'content-type': type, 'cache-control': 'public, max-age=31536000, immutable' } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname.startsWith('/api/')) return await api(request, env, url);
      if (url.pathname.startsWith('/images/')) return await image(env, url);
    } catch (e) {
      if (e instanceof HttpError) return json({ detail: e.message }, e.status);
      console.log('error: ' + ((e && e.stack) || e));
      return json({ detail: 'Server error' }, 500);
    }
    if (url.pathname === '/' || url.pathname === '/index.html') {
      // Always revalidate the page itself so a new deploy shows up on the next load.
      const r = await env.ASSETS.fetch(new Request(new URL('/index.html', url), request));
      const h = new Headers(r.headers); h.set('cache-control', 'no-cache');
      return new Response(r.body, { status: r.status, headers: h });
    }
    return env.ASSETS.fetch(request);
  },
};
