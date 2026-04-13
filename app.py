import os
import uuid
import glob
import json
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import logging

from i18n import detect_lang, get_translator

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Cleanup settings
MAX_DOWNLOAD_AGE_HOURS = 1
MAX_DOWNLOAD_DIR_SIZE_MB = 500

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Download timeout: 30 minutes (large files need more time)
DOWNLOAD_TIMEOUT = 1800

jobs = {}
YOUTUBE_BOT_ERROR_TEXT = "Sign in to confirm you’re not a bot"


def build_ytdlp_flags():
    flags = ["--no-playlist"]
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    cookies_from_browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
    if cookies_file:
        flags += ["--cookies", cookies_file]
    elif cookies_from_browser:
        flags += ["--cookies-from-browser", cookies_from_browser]
    return flags


def build_ytdlp_cmd(*args):
    override = os.environ.get("YT_DLP_BIN", "").strip()
    if override:
        return [override, *args]
    if shutil.which("yt-dlp"):
        return ["yt-dlp", *args]
    return [sys.executable, "-m", "yt_dlp", *args]


def is_youtube_url(url):
    u = (url or "").lower()
    return "youtube.com" in u or "youtu.be" in u


def run_ytdlp(cmd, url=None, timeout=60):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if (
        result.returncode != 0
        and url
        and is_youtube_url(url)
        and YOUTUBE_BOT_ERROR_TEXT in result.stderr
    ):
        retry_cmd = [*cmd, "--extractor-args", "youtube:player_client=android"]
        return subprocess.run(retry_cmd, capture_output=True, text=True, timeout=timeout)
    return result

# Max concurrent downloads in a batch
MAX_BATCH_WORKERS = 3
batch_executor = ThreadPoolExecutor(max_workers=MAX_BATCH_WORKERS)


def cleanup_old_downloads():
    """Remove download files older than MAX_DOWNLOAD_AGE_HOURS."""
    now = time.time()
    cutoff = now - (MAX_DOWNLOAD_AGE_HOURS * 3600)
    removed = 0
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
        try:
            if os.path.isfile(f) and os.path.getmtime(f) < cutoff:
                os.remove(f)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info(f"Cleanup: removed {removed} old download(s)")


def enforce_dir_size_limit():
    """If downloads dir exceeds MAX_DOWNLOAD_DIR_SIZE_MB, remove oldest files first."""
    files = []
    total = 0
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
        if os.path.isfile(f):
            size = os.path.getsize(f)
            total += size
            files.append((os.path.getmtime(f), f, size))

    max_bytes = MAX_DOWNLOAD_DIR_SIZE_MB * 1024 * 1024
    if total <= max_bytes:
        return

    # Sort oldest first, remove until under limit
    files.sort()
    removed = 0
    for _, f, size in files:
        try:
            os.remove(f)
            total -= size
            removed += 1
            if total <= max_bytes:
                break
        except OSError:
            pass
    if removed:
        logger.info(f"Cleanup: removed {removed} file(s) to enforce size limit")


def run_download(job_id, url, format_choice, format_id, t):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]
    if os.path.isfile(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]

    # Enable progress tracking via newline-separated output
    cmd += ["--newline", "--progress"]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        # Use Popen for real-time progress parsing
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        progress_pattern = re.compile(r"\[download\]\s+Destination:|\[download\]\s+(\d+\.?\d*)%")
        last_error_lines = []

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue

            # Track progress percentage
            match = progress_pattern.search(line)
            if match:
                if match.group(1):
                    try:
                        pct = float(match.group(1))
                        job["progress"] = min(pct, 100.0)
                    except ValueError:
                        pass

            # Track error/warning lines for better error reporting
            if line.startswith("ERROR:") or line.startswith("WARNING:"):
                last_error_lines.append(line)
                logger.warning(f"Job {job_id}: {line}")

        process.wait(timeout=DOWNLOAD_TIMEOUT)

        if process.returncode != 0:
            job["status"] = "error"
            # Use the most informative error line
            if last_error_lines:
                job["error"] = last_error_lines[-1].replace("ERROR: ", "")
            else:
                job["error"] = f"yt-dlp exited with code {process.returncode}"
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = t("error.file_not_found")
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        if format_choice == "gif":
            gif_out = os.path.splitext(chosen)[0] + ".gif"
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", chosen,
                "-vf", "fps=15,scale=w='min(480,iw)':h=-2:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                gif_out
            ]
            ff_res = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            if ff_res.returncode != 0:
                job["status"] = "error"
                job["error"] = "Failed to create GIF"
                return
            chosen = gif_out
            files.append(gif_out)

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["progress"] = 100.0
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)

        # Report file size for user awareness
        try:
            file_size = os.path.getsize(chosen)
            job["file_size_mb"] = round(file_size / (1024 * 1024), 2)
        except OSError:
            pass

    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = f"Download timed out ({DOWNLOAD_TIMEOUT // 60} min limit). Try a lower quality format for large files."
        # Clean up partial file
        try:
            process.kill()
        except Exception:
            pass
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        logger.exception(f"Job {job_id} failed with unexpected error")


@app.route("/")
def index():
    lang = detect_lang(request.headers.get("Accept-Language", ""))
    t, strings = get_translator(lang)
    return render_template("index.html", t=t, strings=strings, lang=lang)


@app.route("/api/cleanup", methods=["POST"])
def cleanup_endpoint():
    """Manually trigger cleanup of old downloads."""
    cleanup_old_downloads()
    enforce_dir_size_limit()
    return jsonify({"status": "ok", "message": "Cleanup completed"})


@app.route("/api/downloads/stats", methods=["GET"])
def downloads_stats():
    """Return stats about the downloads folder."""
    files = glob.glob(os.path.join(DOWNLOAD_DIR, "*"))
    file_list = []
    total_size = 0
    for f in files:
        if os.path.isfile(f):
            size = os.path.getsize(f)
            total_size += size
            file_list.append({
                "name": os.path.basename(f),
                "size": size,
                "modified": os.path.getmtime(f),
            })
    file_list.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify({
        "count": len(file_list),
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "files": file_list,
    })


@app.route("/api/info", methods=["POST"])
def get_info():
    lang = detect_lang(request.headers.get("Accept-Language", ""))
    t, _ = get_translator(lang)

    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": t("error.no_url")}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    if os.path.isfile(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = [json.loads(line) for line in result.stdout.strip().split("\n") if line.strip()]

        # Build quality options — keep best format per resolution
        def extract_info(info):
            best_by_height = {}
            for f in info.get("formats", []):
                height = f.get("height")
                if height and f.get("vcodec", "none") != "none":
                    tbr = f.get("tbr") or 0
                    if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                        best_by_height[height] = f

            formats = []
            for height, f in best_by_height.items():
                formats.append({
                    "id": f["format_id"],
                    "label": f"{height}p",
                    "height": height,
                })
            formats.sort(key=lambda x: x["height"], reverse=True)

            return {
                "title": info.get("title", ""),
                "thumbnail": info.get("thumbnail", ""),
                "duration": info.get("duration"),
                "uploader": info.get("uploader", ""),
                "formats": formats,
                "url": info.get("webpage_url", url),
            }
        result_list = [extract_info(v) for v in info]
        
        if len(result_list) ==1:
            return jsonify(result_list[0])
        else:
            return jsonify({
                "is_playlist": True,
                "videos": result_list,
            })
    except subprocess.TimeoutExpired:
        return jsonify({"error": t("error.info_timeout")}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    lang = detect_lang(request.headers.get("Accept-Language", ""))
    t, _ = get_translator(lang)

    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": t("error.no_url")}), 400

    # Run cleanup before starting new downloads
    cleanup_old_downloads()
    enforce_dir_size_limit()

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id, t))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    lang = detect_lang(request.headers.get("Accept-Language", ""))
    t, _ = get_translator(lang)

    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": t("error.job_not_found")}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "progress": job.get("progress"),
        "file_size_mb": job.get("file_size_mb"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    lang = detect_lang(request.headers.get("Accept-Language", ""))
    t, _ = get_translator(lang)

    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": t("error.file_not_ready")}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


@app.route("/api/batch/download", methods=["POST"])
def batch_download():
    """Start multiple downloads in parallel.

    Expects JSON body:
    {
        "urls": ["https://...", "https://..."],
        "format": "video" | "audio",
        "format_id": null | "..."
    }
    Returns a batch_id and individual job_ids.
    """
    data = request.json
    urls = data.get("urls", [])
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")

    if not urls or not isinstance(urls, list):
        return jsonify({"error": "No URLs provided"}), 400

    if len(urls) > 20:
        return jsonify({"error": "Maximum 20 URLs per batch"}), 400

    batch_id = uuid.uuid4().hex[:10]
    job_ids = []

    for url in urls:
        url = url.strip()
        if not url:
            continue
        job_id = uuid.uuid4().hex[:10]
        jobs[job_id] = {"status": "downloading", "url": url, "batch_id": batch_id}
        job_ids.append(job_id)
        batch_executor.submit(run_download, job_id, url, format_choice, format_id)

    jobs[batch_id] = {
        "status": "batch",
        "job_ids": job_ids,
        "total": len(job_ids),
    }

    return jsonify({
        "batch_id": batch_id,
        "job_ids": job_ids,
        "total": len(job_ids),
    })


@app.route("/api/batch/status/<batch_id>")
def batch_status(batch_id):
    """Get status of all jobs in a batch."""
    batch = jobs.get(batch_id)
    if not batch or batch.get("status") != "batch":
        return jsonify({"error": "Batch not found"}), 404

    job_ids = batch.get("job_ids", [])
    results = []
    done_count = 0
    error_count = 0

    for jid in job_ids:
        job = jobs.get(jid)
        if not job:
            results.append({"job_id": jid, "status": "unknown"})
            continue
        results.append({
            "job_id": jid,
            "status": job["status"],
            "error": job.get("error"),
            "filename": job.get("filename"),
        })
        if job["status"] == "done":
            done_count += 1
        elif job["status"] == "error":
            error_count += 1

    all_done = done_count + error_count >= len(job_ids)
    return jsonify({
        "batch_id": batch_id,
        "total": len(job_ids),
        "done": done_count,
        "errors": error_count,
        "pending": len(job_ids) - done_count - error_count,
        "all_done": all_done,
        "jobs": results,
    })


if __name__ == "__main__":
    # Clean up stale downloads on startup
    cleanup_old_downloads()
    enforce_dir_size_limit()

    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
