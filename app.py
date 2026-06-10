"""
Music DL — Backend serveur pour Android
Flask API avec CORS — télécharge en temp, streame au client, supprime
"""

import os, io, json, uuid, tempfile, threading, time, re
import urllib.request, urllib.parse
from flask import Flask, Response, request, jsonify, send_file, stream_with_context
from flask_cors import CORS
import yt_dlp, imageio_ffmpeg
from mutagen.mp3 import MP3
from mutagen.id3 import ID3
from mutagen.easyid3 import EasyID3
import shutil

# ── FFmpeg ────────────────────────────────────────────────────────────────────
def _setup_ffmpeg():
    src = imageio_ffmpeg.get_ffmpeg_exe()
    tmp = os.path.join(tempfile.gettempdir(), "musicdl_ff")
    os.makedirs(tmp, exist_ok=True)
    dst = os.path.join(tmp, "ffmpeg")
    if not os.path.exists(dst):
        shutil.copy2(src, dst)
        try: os.chmod(dst, 0o755)
        except: pass
    return tmp

FFMPEG_DIR = _setup_ffmpeg()

app = Flask(__name__)
CORS(app)  # autorise toutes les origines (app Android)

APP_VERSION = "3.1.0"

# jobs en mémoire : job_id -> état
jobs = {}
# fichiers temp prêts à être téléchargés : job_id -> path
ready_files = {}

# ── Utilitaires ───────────────────────────────────────────────────────────────

def _fmt_dur(s):
    if not s: return "—"
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def _fmt_views(n):
    if not n: return ""
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M vues"
    if n >= 1_000:     return f"{n/1_000:.0f}K vues"
    return f"{n} vues"

# ── Routes de base ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({"app": "Music DL Server", "version": APP_VERSION, "status": "ok"})

@app.route("/api/version")
def api_version():
    return jsonify({"version": APP_VERSION})

@app.route("/api/ping")
def api_ping():
    try:
        urllib.request.urlopen("https://www.google.com", timeout=4)
        return jsonify({"online": True})
    except Exception:
        return jsonify({"online": False})

# ── Recherche ─────────────────────────────────────────────────────────────────

@app.route("/api/suggest")
def api_suggest():
    q = request.args.get("q", "").strip()
    if not q: return jsonify([])
    try:
        url = ("https://suggestqueries.google.com/complete/search"
               "?client=firefox&ds=yt&q=" + urllib.parse.quote(q))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return jsonify(data[1][:8] if len(data) > 1 else [])
    except Exception:
        return jsonify([])

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    try: n = min(int(request.args.get("n", 20)), 50)
    except: n = 20
    if not q: return jsonify([])
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{q}", download=False)
        results = []
        for e in (info or {}).get("entries", []):
            thumb = e.get("thumbnail", "")
            if not thumb:
                for t in e.get("thumbnails", []):
                    if t.get("url"): thumb = t["url"]; break
            results.append({
                "id":       e.get("id", ""),
                "title":    e.get("title", ""),
                "channel":  e.get("uploader") or e.get("channel", ""),
                "duration": _fmt_dur(e.get("duration")),
                "views":    _fmt_views(e.get("view_count")),
                "thumb":    thumb,
                "url":      e.get("url", ""),
            })
        return jsonify(results)
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500

# ── Téléchargement ────────────────────────────────────────────────────────────

@app.route("/api/download", methods=["POST"])
def api_download():
    data   = request.json or {}
    vid_id = data.get("id", "")
    url    = data.get("url") or f"https://www.youtube.com/watch?v={vid_id}"
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"progress": 0, "status": "Démarrage…", "done": False,
                    "error": "", "cancelled": False}
    threading.Thread(target=_download_server, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})

def _download_server(job_id, url):
    tmp_dir = tempfile.mkdtemp(prefix="musicdl_")
    def hook(d):
        j = jobs[job_id]
        if j.get("cancelled"): raise Exception("__cancelled__")
        if d["status"] == "downloading":
            raw = (d.get("_percent_str", "0")
                   .replace("%", "").replace("\x1b[0;94m", "").replace("\x1b[0m", "").strip())
            try:    pct = float(raw)
            except: pct = 0.0
            spd = d.get("_speed_str", "").strip()
            eta = d.get("_eta_str", "").strip()
            j["progress"] = pct
            j["status"]   = f"{spd} — ETA {eta}" if spd else "Téléchargement…"
        elif d["status"] == "finished":
            j["progress"] = 99
            j["status"]   = "Conversion MP3…"
    try:
        opts = {
            "format":          "bestaudio/best",
            "outtmpl":         os.path.join(tmp_dir, "%(title)s.%(ext)s"),
            "ffmpeg_location": FFMPEG_DIR,
            "postprocessors":  [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
            "writethumbnail": True, "embedthumbnail": True,
            "progress_hooks": [hook], "quiet": True, "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        # Trouver le fichier MP3 généré
        mp3_files = [f for f in os.listdir(tmp_dir) if f.lower().endswith(".mp3")]
        if not mp3_files:
            raise Exception("Fichier MP3 introuvable après conversion")
        mp3_path = os.path.join(tmp_dir, mp3_files[0])
        ready_files[job_id] = {"path": mp3_path, "name": mp3_files[0], "dir": tmp_dir}
        jobs[job_id].update({"progress": 100, "status": "Prêt", "done": True})
        # Nettoyage auto après 10 minutes
        def cleanup():
            time.sleep(600)
            _cleanup_job(job_id)
        threading.Thread(target=cleanup, daemon=True).start()
    except Exception as ex:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if jobs[job_id].get("cancelled") or "__cancelled__" in str(ex):
            jobs[job_id].update({"cancelled": True, "status": "Annulé", "done": True, "error": ""})
        else:
            jobs[job_id].update({"error": str(ex), "status": "Erreur", "done": True})

def _cleanup_job(job_id):
    rf = ready_files.pop(job_id, None)
    if rf: shutil.rmtree(rf["dir"], ignore_errors=True)
    jobs.pop(job_id, None)

@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    def stream():
        while True:
            j = jobs.get(job_id, {})
            yield f"data: {json.dumps(j)}\n\n"
            if j.get("done") or j.get("error"): break
            time.sleep(0.5)
    return Response(stream_with_context(stream()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    j = jobs.get(job_id)
    if j and not j.get("done"):
        j["cancelled"] = True
        j["status"] = "Annulé"
    return jsonify({"ok": True})

@app.route("/api/file/<job_id>")
def api_file(job_id):
    """Télécharge le fichier MP3 prêt, puis le supprime du serveur."""
    rf = ready_files.get(job_id)
    if not rf or not os.path.exists(rf["path"]):
        return jsonify({"error": "Fichier non disponible"}), 404
    path = rf["path"]
    name = rf["name"]
    response = send_file(
        path,
        mimetype="audio/mpeg",
        as_attachment=True,
        download_name=name,
    )
    # Nettoyage après envoi
    @response.call_on_close
    def cleanup():
        _cleanup_job(job_id)
    return response

@app.route("/api/yt/url/<vid>")
def api_yt_url(vid):
    try:
        opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best", "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        url = info.get("url", "")
        if not url:
            for f in reversed(info.get("formats", [])):
                if f.get("acodec", "none") != "none" and f.get("url"):
                    url = f["url"]; break
        return jsonify({"url": url, "title": info.get("title", ""), "duration": info.get("duration", 0)})
    except Exception as e:
        return jsonify({"url": "", "error": str(e)}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
