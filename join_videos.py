import os
import sys
import subprocess
import tempfile
import json
import shutil
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# Detect CI (GitHub Actions)
IS_CI = os.environ.get("CI", "false").lower() == "true"

# ─── Config ───────────────────────────────────────────────
TRANSITION   = os.environ.get("TRANSITION", "fade")
QUALITY      = os.environ.get("QUALITY", "high")
OUTPUT_NAME  = os.environ.get("OUTPUT_NAME", "merged_output")

MAX_WORKERS = 2 if IS_CI else min(4, os.cpu_count() or 2)

QUALITY_PRESETS = {
    "high":   {"crf": "18", "preset": "slow"},
    "medium": {"crf": "23", "preset": "medium"},
    "low":    {"crf": "28", "preset": "fast"},
}

SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v"}


# ─── Helpers ──────────────────────────────────────────────
def log(msg):
    print(msg)


def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["ffprobe", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log("❌ FFmpeg not installed")
        sys.exit(1)


def run_ffprobe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True
    )
    return json.loads(result.stdout)


def get_video_info(path):
    meta = run_ffprobe(path)
    for stream in meta.get("streams", []):
        if stream.get("codec_type") == "video":
            fps_raw = stream.get("r_frame_rate", "30/1")
            num, den = fps_raw.split("/")
            fps = round(float(num) / float(den), 3)
            return {
                "width": int(stream.get("width", 1920)),
                "height": int(stream.get("height", 1080)),
                "fps": fps,
                "duration": float(meta["format"].get("duration", 0)),
                "has_audio": any(s["codec_type"] == "audio" for s in meta["streams"]),
            }
    raise RuntimeError(f"No video stream in {path}")


def find_videos():
    return sorted([p for p in Path(".").rglob("*") if p.suffix.lower() in SUPPORTED_EXTENSIONS])


# ─── Live FFmpeg Progress ─────────────────────────────────
def normalize_video(src, dst, target, quality, progress_callback=None):
    info = get_video_info(src)

    vf = f"pad={target['width']}:{target['height']}:(ow-iw)/2:(oh-ih)/2:black,fps={target['fps']}"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", quality["crf"],
        "-preset", quality["preset"],
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        dst
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1
    )

    last_time = 0.0

    for line in process.stdout:
        if line.startswith("out_time_ms"):
            ms = int(line.strip().split("=")[1])
            seconds = ms / 1_000_000

            delta = seconds - last_time
            last_time = seconds

            if progress_callback and delta > 0:
                progress_callback(delta)

    process.wait()

    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg failed on {src}")

    return dst


# ─── Transitions ──────────────────────────────────────────
def apply_none(clips, output):
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        for c in clips:
            f.write(f"file '{os.path.abspath(c)}'\n")
        list_file = f.name

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", output
    ], check=True)

    os.unlink(list_file)


def apply_fade(clips, output, dur=0.5):
    inputs = sum([["-i", c] for c in clips], [])
    durations = [get_video_info(Path(c))["duration"] for c in clips]

    filter_parts, last_v, last_a, offset = [], "[0:v]", "[0:a]", 0.0

    for i in range(1, len(clips)):
        offset += durations[i - 1] - dur
        nv, na = f"[vx{i}]", f"[ax{i}]"
        filter_parts += [
            f"{last_v}[{i}:v]xfade=transition=fade:duration={dur}:offset={offset:.3f}{nv}",
            f"{last_a}[{i}:a]acrossfade=d={dur}{na}"
        ]
        last_v, last_a = nv, na

    subprocess.run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", last_v, "-map", last_a,
        "-c:v", "libx264", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        output
    ], check=True)


# ─── Main ────────────────────────────────────────────────
def main():
    check_ffmpeg()

    videos = find_videos()
    if not videos:
        log("❌ No videos found")
        sys.exit(1)

    target = {"width": 1920, "height": 1080, "fps": 30}
    quality = QUALITY_PRESETS[QUALITY]

    # Pre-calc durations
    video_infos = [get_video_info(v) for v in videos]
    total_seconds = sum(v["duration"] for v in video_infos)

    log(f"🎬 Total duration: {total_seconds/60:.2f} minutes")

    # Global progress bar
    progress = tqdm(
        total=total_seconds,
        unit="sec",
        desc="Total Progress",
        dynamic_ncols=True
    )

    progress_lock = threading.Lock()

    def update_progress(delta):
        with progress_lock:
            progress.update(delta)

    with tempfile.TemporaryDirectory() as tmp:
        normalized = [None] * len(videos)

        log(f"⚙️ Normalizing with {MAX_WORKERS} threads...")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {}

            for i, v in enumerate(videos):
                out_path = os.path.join(tmp, f"{i}.mp4")
                futures[executor.submit(
                    normalize_video, v, out_path, target, quality, update_progress
                )] = i

            for future in as_completed(futures):
                idx = futures[future]
                normalized[idx] = future.result()
                log(f"✓ Finished {idx+1}/{len(videos)}")

        progress.close()

        log("🔗 Joining videos...")

        output = f"{OUTPUT_NAME}.mp4"

        if TRANSITION == "none":
            apply_none(normalized, output)
        else:
            apply_fade(normalized, output)

        log(f"✅ Done: {output}")


if __name__ == "__main__":
    main()
