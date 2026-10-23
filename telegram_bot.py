"""Crayon City Telegram bot.

Send the bot a photo of a colored car drawing and the car appears in the city.
The bot finds the sheet of paper in the photo, lifts the drawing off it (the same
method as the web page), asks Claude what kind of car it is, and parks it through
the server's API.

Run:   python telegram_bot.py
Env:   TELEGRAM_BOT_TOKEN   from @BotFather (required)
       TELEGRAM_ALLOWED     comma-separated Telegram user ids or @usernames that may
                            add cars; empty means anyone who finds the bot (not advised)
       CRAYON_URL           the server, default http://127.0.0.1:8080
       CRAYON_PIN           the server's PIN, if it has one
       ANTHROPIC_API_KEY    lets Claude guess the type, facing and a name
       REMBG_MODEL          background-removal model (default isnet-general-use; u2net, u2netp
                            and others from the rembg project also work); "off" = paper method only
       CRAYON_KEEP          folder that keeps the last incoming photos, for debugging (optional)

Caption words steer the result: "police", "fire", "ambulance", "taxi", "race", "bus",
"truck", "car"; "left" or "right" for the way the front points; the other words
become the name. Example caption: "police Blue Flash".
"""
import io
import json
import mimetypes
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

from server import CLASSIFY, TYPES, ask_claude

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"
CRAYON_URL = os.environ.get("CRAYON_URL", "http://127.0.0.1:8080").rstrip("/")
PIN = os.environ.get("CRAYON_PIN", "").strip()
ALLOWED = {a.strip().lstrip("@").lower() for a in os.environ.get("TELEGRAM_ALLOWED", "").split(",") if a.strip()}

LABELS = {
    "police": "police car", "fire": "fire truck", "ambulance": "ambulance", "taxi": "taxi",
    "race": "race car", "bus": "bus", "truck": "truck", "regular": "car", "other": "vehicle",
}
TYPE_WORDS = {
    "police": "police", "cop": "police", "polis": "police",
    "fire": "fire", "firetruck": "fire", "firefighter": "fire",
    "ambulance": "ambulance", "taxi": "taxi", "cab": "taxi",
    "race": "race", "racing": "race", "racecar": "race", "f1": "race",
    "bus": "bus", "truck": "truck", "lorry": "truck",
    "car": "regular", "regular": "regular", "other": "other",
}
CROSS = ndimage.generate_binary_structure(2, 1)


# ---------- Image work (a port of the page's cutout) ----------
def load_photo(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    k = min(1.0, 1400 / max(img.size))
    if k < 1:
        img = img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS)
    return img


def find_paper(img: Image.Image) -> Image.Image:
    """Crop to the sheet of paper: the largest bright, unsaturated region."""
    arr = np.asarray(img).astype(np.float32)
    mx, mn = arr.max(2), arr.min(2)
    v = mx / 255.0
    s = (mx - mn) / np.maximum(mx, 1.0)
    bright = (v > 0.55) & (s < 0.35)
    bright = ndimage.binary_opening(bright, structure=CROSS, iterations=3)
    lab, n = ndimage.label(bright, structure=CROSS)
    if n == 0:
        return img
    areas = np.bincount(lab.ravel())
    areas[0] = 0
    big = int(areas.argmax())
    h, w = lab.shape
    if areas[big] < 0.08 * h * w:
        return img  # no obvious sheet; use the whole photo
    ys, xs = np.where(lab == big)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    if (x1 - x0) * (y1 - y0) > 0.93 * h * w:
        return img  # the photo already is the paper
    if areas[big] < 0.72 * (x1 - x0 + 1) * (y1 - y0 + 1):
        return img  # bright but not a rectangle: a pale car on a dark table, not a sheet
    # step a little inside the sheet so its edges and the table never count as drawing
    mx_in, my_in = int((x1 - x0) * 0.03) + 2, int((y1 - y0) * 0.03) + 2
    return img.crop((x0 + mx_in, y0 + my_in, x1 - mx_in + 1, y1 - my_in + 1))


def cutout(img: Image.Image, sens: float = 0.5, biggest: bool = False):
    arr = np.asarray(img).astype(np.float32)
    h, w = arr.shape[:2]
    if h < 4 or w < 4:
        return None
    border = np.concatenate([arr[:2].reshape(-1, 3), arr[-2:].reshape(-1, 3), arr[:, :2].reshape(-1, 3), arr[:, -2:].reshape(-1, 3)])
    paper = np.maximum(np.median(border, axis=0), 40.0)
    bal = np.minimum(arr * (255.0 / paper), 255.0)
    mx, mn = bal.max(2), bal.min(2)
    v = mx / 255.0
    s = (mx - mn) / np.maximum(mx, 1.0)
    sat_t, v_t = 0.15 + 0.27 * sens, 0.88 - 0.4 * sens
    nonpaper = (s > sat_t) | (v < v_t)
    closed = ndimage.binary_dilation(nonpaper, structure=CROSS, iterations=2)
    # paper = whatever the border can reach without crossing the drawing
    lab, _ = ndimage.label(~closed, structure=CROSS)
    edge_labels = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    edge_labels = edge_labels[edge_labels != 0]
    bg = np.isin(lab, edge_labels)
    for _ in range(2):  # give back the rim the closing step ate
        bg |= ndimage.binary_dilation(bg, structure=CROSS) & ~nonpaper
    fg = ~bg
    lab2, n2 = ndimage.label(fg, structure=CROSS)
    if n2 == 0:
        return None
    areas = np.bincount(lab2.ravel())
    areas[0] = 0
    if biggest:
        keep = np.zeros(n2 + 1, bool)
        keep[int(areas.argmax())] = True
    else:
        keep = areas >= max(40, 0.0015 * h * w)
    fg = keep[lab2]
    ys, xs = np.where(fg)
    if xs.size == 0:
        return None
    m = 3
    x0, x1 = max(0, xs.min() - m), min(w - 1, xs.max() + m)
    y0, y1 = max(0, ys.min() - m), min(h - 1, ys.max() + m)
    fgc = fg[y0:y1 + 1, x0:x1 + 1]
    col = bal[y0:y1 + 1, x0:x1 + 1]
    avg = col.mean(2, keepdims=True)
    col = np.clip(avg + (col - avg) * 1.25, 0, 255)
    inner = ndimage.binary_erosion(fgc, structure=CROSS, border_value=0)
    alpha = np.where(fgc, np.where(inner, 255, 150), 0).astype(np.uint8)
    rgba = np.dstack([col.astype(np.uint8), alpha])
    out = Image.fromarray(rgba, "RGBA")
    k = min(1.0, 720 / out.width, 480 / out.height)
    if k < 1:
        out = out.resize((max(1, round(out.width * k)), max(1, round(out.height * k))), Image.LANCZOS)
    return out


# ---------- Background removal with a segmentation model (rembg) ----------
_REMBG = None
REMBG_MODEL = os.environ.get("REMBG_MODEL", "isnet-general-use").strip()


def rembg_session():
    """The model is loaded once; the first call downloads it (~170 MB)."""
    global _REMBG
    if _REMBG is None:
        from rembg import new_session

        _REMBG = new_session(REMBG_MODEL)
    return _REMBG


def finish(col: np.ndarray, alpha: np.ndarray):
    """Crop to the opaque part, boost the colours a little, fit into 720x480."""
    ys, xs = np.where(alpha > 0)
    if xs.size == 0:
        return None
    m = 3
    h, w = alpha.shape
    x0, x1 = max(0, xs.min() - m), min(w - 1, xs.max() + m)
    y0, y1 = max(0, ys.min() - m), min(h - 1, ys.max() + m)
    col = col[y0:y1 + 1, x0:x1 + 1].astype(np.float32)
    avg = col.mean(2, keepdims=True)
    col = np.clip(avg + (col - avg) * 1.15, 0, 255)
    rgba = np.dstack([col.astype(np.uint8), alpha[y0:y1 + 1, x0:x1 + 1]])
    out = Image.fromarray(rgba, "RGBA")
    k = min(1.0, 720 / out.width, 480 / out.height)
    if k < 1:
        out = out.resize((max(1, round(out.width * k)), max(1, round(out.height * k))), Image.LANCZOS)
    return out


def cutout_model(img: Image.Image):
    """Lift the main object off any background. None when the model found nothing usable."""
    from rembg import remove

    k = min(1.0, 1024 / max(img.size))
    small = img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS) if k < 1 else img
    out = remove(small, session=rembg_session(), post_process_mask=True)
    alpha = np.asarray(out)[..., 3].astype(np.uint8)
    mask = alpha > 40
    frac = mask.mean()
    if frac < 0.004 or frac > 0.92:
        return None
    # one object: the biggest blob plus anything at least a tenth of it (a detached wheel, a flag)
    lab, n = ndimage.label(mask, structure=CROSS)
    if n == 0:
        return None
    areas = np.bincount(lab.ravel())
    areas[0] = 0
    keep = areas >= 0.1 * areas.max()
    mask = keep[lab]
    alpha = np.where(mask, alpha, 0).astype(np.uint8)
    return finish(np.asarray(small.convert("RGB")), alpha)


def upright(cut):
    """A car photographed standing on its nose would drive as a tall sliver: turn portrait cutouts
    to landscape, with the side that holds the dark wheels at the bottom."""
    if cut is None or cut.height < 1.3 * cut.width:
        return cut
    a = np.asarray(cut.convert("RGBA")).astype(np.float32)
    opaque = a[..., 3] > 128
    dark = opaque & (a[..., :3].mean(2) < 90)
    half = cut.width // 2
    left = dark[:, :half].sum() / max(1, opaque[:, :half].sum())
    right = dark[:, half:].sum() / max(1, opaque[:, half:].sum())
    # PIL rotates counter-clockwise: +90 brings the left edge to the bottom, -90 the right edge
    return cut.rotate(90 if left >= right else -90, expand=True)


def process_photo(data: bytes):
    img = load_photo(data)
    sheet = find_paper(img)
    if REMBG_MODEL.lower() not in ("", "off", "0", "no"):
        try:
            # the sheet first (a drawing on paper), then the whole photo (a cut-out car on a table)
            for src in ([sheet, img] if sheet.size != img.size else [img]):
                cut = cutout_model(src)
                if cut is not None and cut.width >= 24 and cut.height >= 24:
                    return upright(cut)
            print("[bot] model found no object; using the paper method")
        except Exception as exc:
            print(f"[bot] background model failed ({exc}); using the paper method")
    cut = cutout(sheet)
    if cut is None or cut.width < 24 or cut.height < 24:
        cut = cutout(sheet, sens=0.3)
    return upright(cut)


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def preview_bytes(img: Image.Image) -> bytes:
    """Telegram photos lose transparency, so show the cutout on white."""
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bg.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    bg.save(buf, "JPEG", quality=88)
    return buf.getvalue()


# ---------- Tiny HTTP helpers (no extra dependencies) ----------
def multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----crayon" + secrets.token_hex(12)
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (filename, blob, ctype) in files.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
        out.write(blob)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def http(method: str, url: str, data: bytes = None, headers: dict = None, timeout: int = 60):
    # Cloudflare answers 403 to Python's default user agent, so send a plain one.
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "crayon-city-bot/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def tg(method: str, params: dict = None, files: dict = None, timeout: int = 60):
    if files:
        body, ctype = multipart({k: str(v) for k, v in (params or {}).items()}, files)
        status, raw = http("POST", f"{API}/{method}", body, {"Content-Type": ctype}, timeout)
    else:
        body = urllib.parse.urlencode(params or {}).encode()
        status, raw = http("POST", f"{API}/{method}", body, {"Content-Type": "application/x-www-form-urlencoded"}, timeout)
    return json.loads(raw)


def send(chat_id, text):
    try:
        tg("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as exc:
        print(f"[bot] sendMessage failed: {exc}")


# ---------- Server calls ----------
def server_headers():
    return {"X-Pin": PIN} if PIN else {}


def server_cars() -> list:
    _, raw = http("GET", f"{CRAYON_URL}/api/cars")
    return json.loads(raw)


def server_add(png: bytes, name: str, type_: str, facing: str) -> dict:
    body, ctype = multipart({"name": name, "type": type_, "facing": facing}, {"image": ("car.png", png, "image/png")})
    headers = {"Content-Type": ctype, **server_headers()}
    _, raw = http("POST", f"{CRAYON_URL}/api/cars", body, headers)
    return json.loads(raw)


def server_delete(car_id: str) -> None:
    http("DELETE", f"{CRAYON_URL}/api/cars/{urllib.parse.quote(car_id)}", headers=server_headers())


# ---------- Bot logic ----------
def allowed(user: dict) -> bool:
    if not ALLOWED:
        return True
    return str(user.get("id")) in ALLOWED or (user.get("username") or "").lower() in ALLOWED


def parse_caption(caption: str):
    type_, facing, words = None, None, []
    for raw in (caption or "").split():
        w = raw.strip(".,!").lower()
        if w in TYPE_WORDS:
            type_ = TYPE_WORDS[w]
        elif w in ("left", "right"):
            facing = w
        else:
            words.append(raw.strip(".,!"))
    name = " ".join(words).strip()[:40]
    return type_, facing, name


KEEP_DIR = os.environ.get("CRAYON_KEEP", "").strip()


def keep_photo(data: bytes, limit: int = 40):
    """Keep the last few incoming photos so a bad cutout can be looked at later."""
    if not KEEP_DIR:
        return
    try:
        os.makedirs(KEEP_DIR, exist_ok=True)
        path = os.path.join(KEEP_DIR, time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3) + ".jpg")
        with open(path, "wb") as f:
            f.write(data)
        old = sorted(os.listdir(KEEP_DIR))[:-limit]
        for name in old:
            os.remove(os.path.join(KEEP_DIR, name))
    except OSError as exc:
        print(f"[bot] could not keep the photo: {exc}")


def handle_photo(msg: dict):
    chat_id = msg["chat"]["id"]
    if "photo" in msg:
        file_id = msg["photo"][-1]["file_id"]  # the largest size Telegram kept
    else:
        doc = msg["document"]
        if not (doc.get("mime_type") or "").startswith("image/"):
            send(chat_id, "Send the drawing as a photo or an image file.")
            return
        file_id = doc["file_id"]
    info = tg("getFile", {"file_id": file_id})
    _, data = http("GET", f"{FILE_API}/{info['result']['file_path']}")
    keep_photo(data)
    send(chat_id, "Got it. Cutting the car out of the paper…")
    try:
        cut = process_photo(data)
    except Exception as exc:
        print(f"[bot] cutout failed: {exc}")
        cut = None
    if cut is None:
        send(chat_id, "I could not find a drawing in that photo. Try a brighter photo, straight above the paper, with the whole sheet in view.")
        return
    png = png_bytes(cut)
    c_type, c_facing, c_name = parse_caption(msg.get("caption", ""))
    guess = {}
    if CLASSIFY and (c_type is None or c_facing is None or not c_name):
        try:
            guess = ask_claude(png, "image/png") or {}
        except Exception as exc:
            print(f"[bot] Claude failed: {exc}")
    type_ = c_type or guess.get("type") or "regular"
    facing = c_facing or guess.get("facing") or "right"
    name = c_name or guess.get("name") or LABELS[type_].title()
    try:
        car = server_add(png, name, type_, facing)
    except urllib.error.HTTPError as exc:
        send(chat_id, "The city did not accept the car (wrong PIN?)." if exc.code == 401 else f"The city server answered {exc.code}.")
        return
    except Exception as exc:
        send(chat_id, f"Could not reach the city server: {exc}")
        return
    caption = f"{car['name']} ({LABELS[car['type']]}) is on the road, front pointing {car['facing']}."
    if not c_facing:
        caption += f"\nDriving backwards? Send: /flip {car['name']}"
    if not c_type:
        caption += "\nWrong type? Send the photo again with a caption like: police Blue Flash"
    try:
        tg("sendPhoto", {"chat_id": chat_id, "caption": caption}, {"photo": ("car.jpg", preview_bytes(cut), "image/jpeg")})
    except Exception as exc:
        print(f"[bot] sendPhoto failed: {exc}")
        send(chat_id, caption)


def handle_text(msg: dict):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    if cmd in ("/start", "/help"):
        send(chat_id, "Send me a photo of a colored car drawing and it will drive in Crayon City.\n"
                      "Add a caption to steer it, for example: police Blue Flash, or: taxi left Sunny.\n"
                      "/cars lists the cars, /flip <name> turns one around, /remove <name> takes one off the road.")
    elif cmd == "/cars":
        try:
            cars = server_cars()
        except Exception as exc:
            send(chat_id, f"Could not reach the city server: {exc}")
            return
        send(chat_id, "No cars yet." if not cars else "\n".join(f"• {c['name']} ({LABELS.get(c['type'], c['type'])})" for c in cars))
    elif cmd in ("/remove", "/flip"):
        want = arg.strip().lower()
        if not want:
            send(chat_id, f"Which one? {cmd} <name>")
            return
        try:
            cars = server_cars()
            hit = next((c for c in cars if c["name"].lower() == want), None) or next((c for c in cars if want in c["name"].lower()), None)
            if not hit:
                send(chat_id, f"No car called {arg.strip()}.")
                return
            if cmd == "/flip":
                # the city has no edit call: re-add the same picture with the other facing, then drop the old car
                _, png = http("GET", f"{CRAYON_URL}/images/{hit['image']}")
                facing = "left" if hit.get("facing") == "right" else "right"
                server_add(png, hit["name"], hit["type"], facing)
                server_delete(hit["id"])
                send(chat_id, f"{hit['name']} turned around, front now pointing {facing}.")
                return
            server_delete(hit["id"])
            send(chat_id, f"{hit['name']} left the city.")
        except urllib.error.HTTPError as exc:
            send(chat_id, "The city refused (wrong PIN?)." if exc.code == 401 else f"The city server answered {exc.code}.")
        except Exception as exc:
            send(chat_id, f"Could not reach the city server: {exc}")
    else:
        send(chat_id, "Send a photo of a drawing, or /help.")


def main():
    if not TOKEN:
        sys.exit("Set TELEGRAM_BOT_TOKEN (get one from @BotFather).")
    me = tg("getMe")["result"]
    print(f"[bot] running as @{me.get('username')} -> {CRAYON_URL}; Claude classification {'on' if CLASSIFY else 'off'}; "
          f"allowed: {', '.join(sorted(ALLOWED)) or 'everyone'}")
    offset = None
    while True:
        try:
            res = tg("getUpdates", {"timeout": 50, **({"offset": offset} if offset else {})}, timeout=70)
        except Exception as exc:
            print(f"[bot] getUpdates failed: {exc}")
            time.sleep(5)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("channel_post")
            if not msg:
                continue
            user = msg.get("from") or {}
            if not allowed(user):
                send(msg["chat"]["id"], "This city is private.")
                print(f"[bot] refused user id={user.get('id')} username={user.get('username')}")
                continue
            try:
                if "photo" in msg or "document" in msg:
                    handle_photo(msg)
                else:
                    handle_text(msg)
            except Exception as exc:
                print(f"[bot] error: {exc}")
                send(msg["chat"]["id"], "Something went wrong with that one. Try again.")


if __name__ == "__main__":
    main()
