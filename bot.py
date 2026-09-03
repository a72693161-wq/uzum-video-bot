import hashlib
import os
import re
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

import requests
from flask import Flask, jsonify, request


TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BASE = f"https://api.telegram.org/bot{TOKEN}"
FILE_BASE = f"https://api.telegram.org/file/bot{TOKEN}"
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:24] if TOKEN else "missing-token"
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "").strip()

app = Flask(__name__)
pending = {}
batch_users = {}
jobs_lock = threading.Lock()


def tg(method, data=None, files=None, timeout=120):
    r = requests.post(f"{BASE}/{method}", data=data, files=files, timeout=timeout)
    r.raise_for_status()
    result = r.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram xatosi"))
    return result.get("result")


def send(chat_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        import json
        data["reply_markup"] = json.dumps(reply_markup)
    return tg("sendMessage", data)


def main_keyboard():
    return {
        "keyboard": [
            [{"text": "🎬 Video tayyorlash"}, {"text": "📦 15 ta video"}],
            [{"text": "✂️ Qirqish"}, {"text": "ℹ️ Imkoniyatlar"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def parse_trim(caption):
    if not caption:
        return 0.0, None
    m = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–]\s*(\d{1,2}:\d{2}(?::\d{2})?)", caption)
    if not m:
        return 0.0, None

    def seconds(value):
        parts = [float(x) for x in value.split(":")]
        return parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]

    start, end = seconds(m.group(1)), seconds(m.group(2))
    return (start, end) if end > start else (0.0, None)


def watermark_filter(position):
    base = "scale=1080:1440:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=1080:1440:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1,fps=30"
    if position == "none":
        return ["-vf", base, "-map", "0:v:0"]

    w, h, margin = 520, 260, 35
    xs = {"l": margin, "c": (1080 - w) // 2, "r": 1080 - w - margin}
    ys = {"t": margin, "m": (1440 - h) // 2, "b": 1440 - h - margin}
    y_key, x_key = position[0], position[1]
    x, y = xs[x_key], ys[y_key]
    graph = (
        f"[0:v]{base}[base];"
        f"[base]split=2[main][area];"
        f"[area]crop={w}:{h}:{x}:{y},gblur=sigma=35:steps=4[blur];"
        f"[main][blur]overlay={x}:{y},scale=1080:1440,setsar=1[outv]"
    )
    return ["-filter_complex", graph, "-map", "[outv]"]


def detect_watermark_position(source, duration):
    """Uchta kadrdagi doimiy, mayda-kontrastli belgi joyini topadi."""
    width, height = 270, 360
    frames = []
    for ratio in (0.15, 0.50, 0.85):
        at = max(0, duration * ratio)
        frame = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(at),
            "-i", str(source), "-vf", f"scale={width}:{height},format=gray",
            "-frames:v", "1", "-f", "rawvideo", "pipe:1",
        ], timeout=45)
        if len(frame) == width * height:
            frames.append(frame)
    if len(frames) < 2:
        return "none"

    rw, rh, margin = 130, 65, 9
    xs = {"l": margin, "c": (width - rw) // 2, "r": width - rw - margin}
    ys = {"t": margin, "m": (height - rh) // 2, "b": height - rh - margin}

    best_position, best_score = "none", 0.0
    for position in ("tl", "tc", "tr", "ml", "mc", "mr", "bl", "bc", "br"):
        x, y = xs[position[1]], ys[position[0]]
        edge_total = diff_total = samples = 0
        for yy in range(y, y + rh):
            row = yy * width
            for xx in range(x, x + rw - 1):
                i = row + xx
                edge_total += sum(abs(frame[i + 1] - frame[i]) for frame in frames)
                diff_total += sum(abs(frames[n][i] - frames[0][i]) for n in range(1, len(frames)))
                samples += len(frames)
        edge = edge_total / max(samples, 1)
        motion = diff_total / max((len(frames) - 1) * rw * rh, 1)
        score = edge - motion * 0.85
        if edge > 11 and motion < 24 and score > best_score:
            best_position, best_score = position, score
    return best_position


def process_job(key):
    job = pending.get(key)
    if not job:
        return
    chat_id = job["chat_id"]
    with jobs_lock:
        try:
            send(chat_id, "⏳ Video tayyorlanmoqda. Biroz kuting...")
            with tempfile.TemporaryDirectory(prefix="uzum_bot_") as temp:
                temp = Path(temp)
                source = temp / "input.mp4"
                output = temp / "UZUM_1080x1440_3MB.mp4"
                file_info = tg("getFile", {"file_id": job["file_id"]})
                with requests.get(f"{FILE_BASE}/{file_info['file_path']}", stream=True, timeout=180) as r:
                    r.raise_for_status()
                    with source.open("wb") as f:
                        for chunk in r.iter_content(1024 * 1024):
                            if chunk:
                                f.write(chunk)

                probe = subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(source)
                ], text=True).strip()
                source_duration = float(probe)
                start = min(job["start"], max(0, source_duration - 0.1))
                end = min(job["end"], source_duration) if job["end"] else source_duration
                duration = end - start
                if duration <= 0:
                    raise RuntimeError("Qirqish vaqti noto‘g‘ri.")

                audio_k = 0 if job["mute"] else 32
                video_k = max(80, int((2_850_000 * 8 / duration) / 1000) - audio_k - 8)

                # Render free serverida 2-pass juda sekin ishlaydi. Bir martalik ABR
                # kodlash va uzun videoda pastroq FPS vaqt tugashining oldini oladi.
                target_fps = 15 if duration > 60 else (20 if duration > 30 else 24)
                watermark = job["watermark"]
                if watermark == "auto":
                    watermark = detect_watermark_position(source, duration)
                filters = watermark_filter(watermark)
                filters[1] = re.sub(r"fps=30", f"fps={target_fps}", filters[1])

                common = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-t", str(duration), "-i", str(source)]
                audio = ["-an"] if job["mute"] else ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "32k"]
                encode = common + filters + audio + [
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-b:v", f"{video_k}k", "-maxrate", f"{video_k}k",
                    "-bufsize", f"{max(video_k * 2, 160)}k",
                    "-profile:v", "high", "-pix_fmt", "yuv420p",
                    "-metadata:s:v:0", "rotate=0", "-aspect", "3:4",
                    "-movflags", "+faststart", str(output),
                ]
                subprocess.run(encode, check=True, timeout=900)

                dimensions = subprocess.check_output([
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(output)
                ], text=True).strip()
                if dimensions != "1080x1440":
                    raise RuntimeError(f"O‘lcham xatosi: {dimensions}")
                if output.stat().st_size > 3_000_000:
                    raise RuntimeError("Fayl 3 MB dan oshib ketdi. Videoni qisqartirib ko‘ring.")

                with output.open("rb") as f:
                    tg("sendVideo", {"chat_id": chat_id, "caption": "✅ Tayyor: 1080×1440 px, 3 MB gacha"}, {"video": (output.name, f, "video/mp4")}, timeout=180)
        except Exception as exc:
            send(chat_id, f"❌ Video tayyorlanmadi: {str(exc)[:500]}")
        finally:
            pending.pop(key, None)


def audio_keyboard(key):
    return {"inline_keyboard": [[
        {"text": "🔊 Ovozli", "callback_data": f"a:{key}:0"},
        {"text": "🔇 Ovozsiz", "callback_data": f"a:{key}:1"},
    ]]}


def batch_audio_keyboard(chat_id):
    return {"inline_keyboard": [[
        {"text": "🔊 Hammasi ovozli", "callback_data": f"b:{chat_id}:0"},
        {"text": "🔇 Hammasi ovozsiz", "callback_data": f"b:{chat_id}:1"},
    ]]}


def position_keyboard(key):
    return {"inline_keyboard": [
        [{"text": "🤖 Avtomatik aniqlash", "callback_data": f"w:{key}:auto"}],
        [{"text": "↖️", "callback_data": f"w:{key}:tl"}, {"text": "⬆️", "callback_data": f"w:{key}:tc"}, {"text": "↗️", "callback_data": f"w:{key}:tr"}],
        [{"text": "⬅️", "callback_data": f"w:{key}:ml"}, {"text": "⏺ Markaz", "callback_data": f"w:{key}:mc"}, {"text": "➡️", "callback_data": f"w:{key}:mr"}],
        [{"text": "↙️", "callback_data": f"w:{key}:bl"}, {"text": "⬇️", "callback_data": f"w:{key}:bc"}, {"text": "↘️", "callback_data": f"w:{key}:br"}],
        [{"text": "✅ Suv belgisi yo‘q", "callback_data": f"w:{key}:none"}],
    ]}


def handle_update(update):
    message = update.get("message")
    if message:
        chat_id = message["chat"]["id"]
        user_id = str(message.get("from", {}).get("id", ""))
        if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
            send(chat_id, "Bu bot shaxsiy foydalanish uchun.")
            return
        text = message.get("text", "")
        if text.startswith("/start") or text == "🏠 Bosh menyu":
            send(
                chat_id,
                "Kerakli bo‘limni tugmalardan tanlang 👇",
                main_keyboard(),
            )
            return
        if text == "🎬 Video tayyorlash":
            batch_users.pop(chat_id, None)
            send(
                chat_id,
                "📤 MP4 videoni yuboring. Keyin ovoz va suv belgisi sozlamalarini tugmalardan tanlaysiz.",
                main_keyboard(),
            )
            return
        if text == "📦 15 ta video":
            send(
                chat_id,
                "15 tagacha video uchun ovoz rejimini bir marta tanlang:",
                batch_audio_keyboard(chat_id),
            )
            return
        if text == "✂️ Qirqish":
            send(
                chat_id,
                "Videoni yuborayotganda izohiga vaqtni yozing. Masalan: 00:05-00:20",
                main_keyboard(),
            )
            return
        if text == "ℹ️ Imkoniyatlar":
            send(
                chat_id,
                "✅ 1080×1440 px\n✅ 3 MB gacha siqish\n✅ 15 ta videoni navbatga olish\n✅ Videoni qirqish\n✅ Ovozsiz qilish\n✅ Suv belgisini avtomatik xiralashtirish",
                main_keyboard(),
            )
            return
        media = message.get("video") or message.get("document")
        if not media:
            send(chat_id, "MP4 videoni yuboring.")
            return
        key = uuid.uuid4().hex[:10]
        start, end = parse_trim(message.get("caption", ""))
        batch = batch_users.get(chat_id)
        if batch:
            if batch["count"] >= 15:
                send(chat_id, "15 ta video qabul qilindi. Yangi guruh uchun 📦 15 ta video tugmasini qayta bosing.")
                return
            batch["count"] += 1
            pending[key] = {"chat_id": chat_id, "file_id": media["file_id"], "start": start, "end": end, "mute": batch["mute"], "watermark": "auto"}
            send(chat_id, f"✅ {batch['count']}/15 video navbatga qo‘shildi.")
            threading.Thread(target=process_job, args=(key,), daemon=True).start()
        else:
            pending[key] = {"chat_id": chat_id, "file_id": media["file_id"], "start": start, "end": end, "mute": False, "watermark": "none"}
            send(chat_id, "Ovoz rejimini tanlang:", audio_keyboard(key))
        return

    callback = update.get("callback_query")
    if callback:
        tg("answerCallbackQuery", {"callback_query_id": callback["id"]})
        parts = callback.get("data", "").split(":")
        if len(parts) != 3:
            return
        kind, key, value = parts
        if kind == "b":
            chat_id = callback["message"]["chat"]["id"]
            batch_users[chat_id] = {"mute": value == "1", "count": 0}
            send(chat_id, "📤 Endi 15 tagacha videoni ketma-ket yuboring. Bot ularni navbat bilan tayyorlaydi.", main_keyboard())
            return
        if key not in pending:
            return
        chat_id = pending[key]["chat_id"]
        if kind == "a":
            pending[key]["mute"] = value == "1"
            send(chat_id, "Suv belgisi qayerda? Joylashuvni tanlang:", position_keyboard(key))
        elif kind == "w":
            pending[key]["watermark"] = value
            threading.Thread(target=process_job, args=(key,), daemon=True).start()


@app.get("/")
def health():
    return jsonify(status="ok", service="Uzum Video Bot")


@app.post(f"/webhook/{SECRET}")
def webhook():
    update = request.get_json(silent=True) or {}
    threading.Thread(target=handle_update, args=(update,), daemon=True).start()
    return "ok"


@app.get("/setup")
def setup():
    if not TOKEN:
        return "BOT_TOKEN kiritilmagan", 500
    url = os.environ.get("RENDER_EXTERNAL_URL", request.url_root.rstrip("/"))
    result = tg("setWebhook", {"url": f"{url}/webhook/{SECRET}"})
    tg("setMyCommands", {"commands": '[{"command":"start","description":"Bosh menyuni ochish"}]'})
    return jsonify(ok=bool(result), webhook=f"{url}/webhook/***")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
