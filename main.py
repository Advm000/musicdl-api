import os, json, uuid, tempfile, threading, time
import urllib.request, urllib.parse
import shutil
from flask import Flask, Response, request, jsonify, send_file, stream_with_context

app = Flask(__name__)
APP_VERSION = "1.0.0"
jobs = {}
ready_files = {}

# ── CORS manuel ───────────────────────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

@app.route("/<path:path>", methods=["OPTIONS"])
def handle_options(path=""):
    return "", 204

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

def _get_ffmpeg():
    try:
        import imageio_ffmpeg
        src = imageio_ffmpeg.get_ffmpeg_exe()
        tmp = os.path.join(tempfile.gettempdir(), "musicdl_ff")
        os.makedirs(tmp, exist_ok=True)
        dst = os.path.join(tmp, os.path.basename(src))
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
            try: os.chmod(dst, 0o755)
            except: pass
        return tmp
    except Exception:
        return None

# ── Routes ────────────────────────────────────────────────────────────────────
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
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
            info = ydl.extract_info(f"ytmsearch{n}:{q}", download=False)
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
            })
        return jsonify(results)
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500

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
    import yt_dlp
    tmp_dir = tempfile.mkdtemp(prefix="musicdl_")
    ffmpeg_dir = _get_ffmpeg()

    def hook(d):
        j = jobs[job_id]
        if j.get("cancelled"): raise Exception("__cancelled__")
        if d["status"] == "downloading":
            raw = (d.get("_percent_str", "0")
                   .replace("%","").replace("\x1b[0;94m","").replace("\x1b[0m","").strip())
            try: pct = float(raw)
            except: pct = 0.0
            j["progress"] = pct
            j["status"] = d.get("_speed_str","").strip() or "Téléchargement…"
        elif d["status"] == "finished":
            j["progress"] = 99
            j["status"] = "Conversion MP3…"

    try:
        opts = {
            "format":         "bestaudio/best",
            "outtmpl":        os.path.join(tmp_dir, "%(title)s.%(ext)s"),
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
            "writethumbnail": True, "embedthumbnail": True,
            "progress_hooks": [hook], "quiet": True, "no_warnings": True,
        }
        if ffmpeg_dir:
            opts["ffmpeg_location"] = ffmpeg_dir
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        mp3_files = [f for f in os.listdir(tmp_dir) if f.lower().endswith(".mp3")]
        if not mp3_files:
            raise Exception("MP3 introuvable après conversion")
        mp3_path = os.path.join(tmp_dir, mp3_files[0])
        ready_files[job_id] = {"path": mp3_path, "name": mp3_files[0], "dir": tmp_dir}
        jobs[job_id].update({"progress": 100, "status": "Prêt", "done": True})
        threading.Thread(target=lambda: (time.sleep(600), _cleanup_job(job_id)), daemon=True).start()
    except Exception as ex:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if "__cancelled__" in str(ex):
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
    rf = ready_files.get(job_id)
    if not rf or not os.path.exists(rf["path"]):
        return jsonify({"error": "Fichier non disponible"}), 404
    response = send_file(rf["path"], mimetype="audio/mpeg",
                         as_attachment=True, download_name=rf["name"])
    @response.call_on_close
    def cleanup():
        _cleanup_job(job_id)
    return response

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
