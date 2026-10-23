"""Crayon City server.

Serves the page, keeps the cars (one PNG per car plus data/cars.json) and asks
Claude what kind of car a drawing is.

Run:   uvicorn server:app --host 0.0.0.0 --port 8080
Env:   CRAYON_PIN        optional PIN that adding or removing a car requires
       CRAYON_DATA       where cars are stored (default: ./data)
       ANTHROPIC_API_KEY lets the server ask Claude what each drawing is
"""
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CRAYON_DATA", ROOT / "data"))
IMAGES = DATA_DIR / "images"
IMAGES.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "cars.json"
PIN = os.environ.get("CRAYON_PIN", "").strip()
TYPES = ["police", "fire", "ambulance", "taxi", "race", "bus", "truck", "regular", "other"]
MAX_IMAGE_BYTES = 4 * 1024 * 1024

# ---------- Claude ----------
_client = None
CLASSIFY = False
if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
    try:
        import anthropic

        _client = anthropic.Anthropic()
        CLASSIFY = True
    except Exception as exc:  # the package is missing or misconfigured
        print(f"[crayon-city] Claude classification is off: {exc}")

PROMPT = (
    "This image is a child's colored drawing of a vehicle, cut out from the paper. "
    'Reply with only a JSON object like {"type":"police","facing":"right","name":"Blue Flash"}. '
    '"type" must be one of: ' + ", ".join(TYPES) + ' (use "regular" for an ordinary car, '
    '"other" if it is not a road vehicle). "facing" is "left" or "right": the side the front of '
    'the vehicle (hood, headlights, windshield) points to. "name" is a fun, kind, two-word name '
    "for this car that a child would love, based on its colors."
)


def ask_claude(image_bytes: bytes, media_type: str) -> dict:
    import base64

    import anthropic

    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(image_bytes).decode("ascii"),
            },
        },
        {"type": "text", "text": PROMPT},
    ]
    kwargs = dict(
        model="claude-opus-5-5",
        max_tokens=256,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": content}],
    )
    try:
        # Server-side fallback: if a safety classifier declines, the API re-runs on another model.
        response = _client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
        )
    except TypeError:
        # An older SDK without the fallbacks parameter.
        response = _client.messages.create(**kwargs)
    except anthropic.BadRequestError:
        response = _client.messages.create(**kwargs)
    if response.stop_reason == "refusal":
        return {}
    text = "".join(block.text for block in response.content if block.type == "text")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    out = {}
    if parsed.get("type") in TYPES:
        out["type"] = parsed["type"]
    if parsed.get("facing") in ("left", "right"):
        out["facing"] = parsed["facing"]
    if isinstance(parsed.get("name"), str) and parsed["name"].strip():
        out["name"] = parsed["name"].strip()[:40]
    return out


# ---------- Storage ----------
def load_cars() -> list:
    if not DB.exists():
        return []
    try:
        cars = json.loads(DB.read_text("utf-8"))
        return cars if isinstance(cars, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_cars(cars: list) -> None:
    tmp = DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(cars, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, DB)


def sniff(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def check_pin(x_pin: Optional[str]) -> None:
    if PIN and not secrets.compare_digest((x_pin or "").strip(), PIN):
        raise HTTPException(status_code=401, detail="PIN required")


async def read_image(upload: UploadFile) -> tuple[bytes, str]:
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty image")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image larger than 4 MB")
    media = sniff(data)
    if not media:
        raise HTTPException(status_code=415, detail="Only PNG, JPEG or WebP images")
    return data, media


# ---------- API ----------
app = FastAPI(title="Crayon City")


@app.get("/api/config")
def config():
    return {"pinRequired": bool(PIN), "classify": CLASSIFY}


@app.get("/api/cars")
def list_cars():
    return sorted(load_cars(), key=lambda c: c.get("addedAt", ""))


@app.post("/api/cars")
async def add_car(
    image: UploadFile = File(...),
    name: str = Form(""),
    type: str = Form("regular"),
    facing: str = Form("right"),
    x_pin: Optional[str] = Header(default=None),
):
    check_pin(x_pin)
    data, media = await read_image(image)
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[media]
    car_id = secrets.token_hex(8)
    filename = f"{car_id}.{ext}"
    (IMAGES / filename).write_bytes(data)
    car = {
        "id": car_id,
        "name": name.strip()[:40],
        "type": type if type in TYPES else "regular",
        "facing": "left" if facing == "left" else "right",
        "image": filename,
        "addedAt": datetime.now(timezone.utc).isoformat(),
    }
    cars = load_cars()
    cars.append(car)
    save_cars(cars)
    return car


@app.delete("/api/cars/{car_id}")
def delete_car(car_id: str, x_pin: Optional[str] = Header(default=None)):
    check_pin(x_pin)
    cars = load_cars()
    keep = [c for c in cars if c.get("id") != car_id]
    if len(keep) == len(cars):
        raise HTTPException(status_code=404, detail="No such car")
    for c in cars:
        if c.get("id") == car_id:
            try:
                (IMAGES / Path(str(c.get("image", ""))).name).unlink(missing_ok=True)
            except OSError:
                pass
    save_cars(keep)
    return {"ok": True}


@app.post("/api/classify")
async def classify(image: UploadFile = File(...), x_pin: Optional[str] = Header(default=None)):
    check_pin(x_pin)  # strangers should not be able to spend your Claude credits
    if not CLASSIFY:
        raise HTTPException(status_code=503, detail="Classification is off (no ANTHROPIC_API_KEY)")
    data, media = await read_image(image)
    try:
        return ask_claude(data, media)
    except Exception as exc:
        print(f"[crayon-city] classify failed: {exc}")
        raise HTTPException(status_code=502, detail="Claude did not answer")


app.mount("/images", StaticFiles(directory=IMAGES), name="images")
app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
