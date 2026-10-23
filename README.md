# Crayon City

Photograph a child's colored car drawing, lift it off the paper, and it drives in a
shared animated city: crossings, traffic lights, parks, a ferris wheel, day and night.
Made for a little brother's drawings and a living-room TV. Run it on your own server, a
Cloudflare Worker, or just your PC.

```
artifact-page.html   the page itself (canvas animation, garage, crop editor)
build_static.py      turns artifact-page.html into static/index.html (adds the server adapter, PIN field, TV mode)
server.py            FastAPI backend: serves the page, stores cars, optionally asks Claude what a drawing is
telegram_bot.py      Telegram bot: send it a photo of a drawing and the car joins the city
cloudflare/          the same backend as a Cloudflare Worker with Workers KV storage
crayon-city.service  systemd unit for the server
crayon-bot.service   systemd unit for the Telegram bot
data/                created on first run: cars.json plus images/*.png (not in git)
```

## Run it on your PC first

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python build_static.py artifact-page.html
$env:CRAYON_PIN = "1234"                 # optional: PIN needed to add or remove cars
$env:ANTHROPIC_API_KEY = "sk-ant-..."    # optional: lets Claude guess the car type and name
uvicorn server:app --host 0.0.0.0 --port 8080
```

Open http://localhost:8080 on the PC, or http://<your PC's LAN IP>:8080 on a phone
or TV in the same Wi-Fi. Add `#main` to the address for the full-screen city.

## Put it on a Linux VM (Ubuntu)

Copy the files, install, and run it as a systemd service. `vm` below is your ssh host.

```bash
ssh vm mkdir -p crayon-city
scp -r server.py telegram_bot.py requirements.txt static crayon-city.service crayon-bot.service vm:crayon-city/
ssh vm
cd ~/crayon-city
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
printf 'CRAYON_PIN=1234\nANTHROPIC_API_KEY=sk-ant-...\n' > .env
chmod 600 .env
sudo cp crayon-city.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crayon-city
```

Then open the port. On Oracle Cloud that is two steps: an ingress rule in the VCN's
security list (TCP 8080 from 0.0.0.0/0) and, because Oracle's Ubuntu image drops
everything not explicitly allowed, a firewall rule on the VM itself:

```bash
sudo iptables -I INPUT 1 -p tcp -m state --state NEW --dport 8080 -j ACCEPT
sudo netfilter-persistent save
```

Open http://<vm address>:8080 on a phone to add cars and `/#main` on the TV.

Useful commands on the VM:
```bash
sudo systemctl status crayon-city          # is it running
journalctl -u crayon-city -f               # live log
sudo systemctl restart crayon-city         # after editing .env
nano ~/crayon-city/.env                    # CRAYON_PIN, ANTHROPIC_API_KEY, Telegram settings
```

To push a new build of the page from the PC:
```powershell
python build_static.py artifact-page.html
scp static/index.html vm:crayon-city/static/
```
No restart needed for the page; `scp server.py vm:crayon-city/` plus a restart for server changes.

The `.env` file keeps your secrets out of the unit file. Leave `ANTHROPIC_API_KEY` out
and the page still works, you just pick the car type by hand.

## Put it on Cloudflare (no VM needed)

`cloudflare/` is the same server rewritten as a Cloudflare Worker: `src/worker.js`
answers the same `/api/...` routes, keeps cars and images in a Workers KV namespace
(free, no card needed; R2 would need a payment method on file), and serves
`public/index.html` (a copy of `static/index.html`). HTTPS, no firewall rules, free tier
(100k reads and 1k writes per day, plenty for one TV and a few phones).
Needs Node.js and `npm install` once inside `cloudflare/`.

```powershell
cd cloudflare
copy wrangler.example.toml wrangler.toml   # then pick a worker name in it
npx wrangler login                        # once: approve in the browser
npx wrangler kv namespace create CARS     # once: paste the id into wrangler.toml
npx wrangler secret put CRAYON_PIN        # once: type the PIN
npx wrangler secret put ANTHROPIC_API_KEY # optional: Claude guesses type and name
npm run deploy                            # rebuilds the page from artifact-page.html and uploads
npx wrangler tail                         # live logs
```

The city then lives at `https://<worker name>.<your subdomain>.workers.dev` (the subdomain
is set in the dashboard under Workers & Pages > Account details). Phone: add cars; TV: add `#main`.

KV is eventually consistent: a car added from a phone shows on the TV right away when
both are in the same city, and within about a minute otherwise.

To run the Telegram bot against it, set `CRAYON_URL=https://<your worker address>`
(plus the PIN) wherever the bot runs; the bot itself stays a Python script and can run
on a VM or on the PC.

## Adding cars from Telegram

`telegram_bot.py` lets you (or your brother, on his own phone) send a photo of the
drawing to a Telegram bot. The bot finds the sheet of paper in the photo, cuts the car
out, asks Claude what it is, and parks it in the same city. No web page needed on the
phone.

1. In Telegram, talk to @BotFather, send `/newbot`, and copy the token it gives you.
2. Find your Telegram user id (send `/start` to @userinfobot) and your brother's, so
   only you two can add cars.
3. Add to `.env` where the bot runs (`CRAYON_URL` points at the city; leave it out when
   the bot runs next to the FastAPI server):
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_ALLOWED=123456789,987654321
   CRAYON_URL=https://<your worker address>
   ```
   (`TELEGRAM_ALLOWED` also accepts @usernames. Leave it empty and anyone who finds
   the bot can add cars.)
4. Install and start the bot:
   ```bash
   sudo cp crayon-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now crayon-bot
   journalctl -u crayon-bot -f
   ```
5. Send the bot a photo. It replies with the cutout and the car's name. The cutout uses a
   background-removal model (rembg, `isnet-general-use`, about 10 s per photo on a small
   VM), so photos of toy cars and screenshots work too, not only drawings on paper. If the
   model finds nothing it falls back to the plain paper method. `REMBG_MODEL=off` in `.env`
   disables the model. Set `CRAYON_KEEP=/some/dir` to keep the last 40 incoming photos so a
   bad cutout can be looked at later. Optional caption words steer it: `police Blue Flash`
   sets the type and name; `left` or `right` says which way the front points. `/cars` lists
   the cars and `/remove Blue Flash` takes one off the road.

The bot talks to the server over its API with the same PIN, so it needs `CRAYON_PIN`
in `.env` too. To run it on your PC against the local server, set the same variables
and run `python telegram_bot.py`.

## On the smart TV

- Open the TV's browser and type the address with `#main` at the end (`#tv` still works too). Press "Start the
  city" once with the remote: browsers only allow sound after a click.
- TV mode hides the header and garage, fills the screen, turns sound on, and switches
  to night after 19:00 local time.
- Most TV browsers (Samsung Tizen, LG webOS, Android TV Chrome) handle the canvas and
  Web Audio. If sound does not play on an older TV, it is the TV browser; the phone
  version still has sound.
- New cars added from a phone appear on the TV within about 8 seconds, no reload.
- The city is a loop several screens long with crossings, traffic lights, parks and a
  ferris wheel. Left and right on the remote pan the view (on a phone, drag or swipe the
  city; on a PC, arrow keys or a horizontal wheel). When nobody touches the remote for
  20 seconds the TV drifts slowly through the city on its own.
- "Find" on a garage card pans the city to that car and follows it for a few seconds.
- Ordinary cars stop at red lights and people cross at the zebra; police, fire and
  ambulance drive straight through.

## Why a PIN

The address is public once the port is open, so without a PIN anyone who finds it
could fill the city with junk or spend your Claude credits on classification. The PIN
is asked once per phone and remembered in that browser.

## Security notes

- This is a hobby server: no accounts. Do not reuse a real password as the PIN.
- Uploads are capped at 4 MB and must be PNG, JPEG or WebP (checked by file signature).
- The FastAPI version is plain HTTP. For HTTPS, put Caddy in front with a domain name
  (`caddy reverse-proxy --from city.example.com --to localhost:8080`), or use the
  Cloudflare version, which is HTTPS out of the box.
- Keep tokens, PINs and keys in `.env` or Worker secrets, never in the files here.

## Editing the page

Edit `artifact-page.html`, then run `python build_static.py artifact-page.html` to
regenerate `static/index.html`. The build script adds the server adapter, the PIN
field and TV mode on top of the artifact page, so both versions stay in sync.
