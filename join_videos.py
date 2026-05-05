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

# ─── CONFIG ───────────────────────────────────────────────
TRANSITION   = os.environ.get("TRANSITION", "fade")
QUALITY      = os.environ.get("QUALITY", "high")
OUTPUT_NAME  = os.environ.get("OUTPUT_NAME", "merged_output")

MAX_WORKERS = 2 if os.environ.get("CI", "false") == "true" else 4

QUALITY_PRESETS = {
    "high":   {"crf": "18", "preset": "slow"},
    "medium": {"crf": "23", "preset": "medium"},
    "low":    {"crf": "28", "preset": "fast"},
}

SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v"}


# ─── HELPERS ─────────────────────────────────────────────
def log(msg):
    print(msg)


def run_ffprobe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True
    )
    return json.loads(result.stdout)


def get_video_info(path):
    meta = run_ffprobe(path)
    for s in meta["streams"]:
        if s["codec_type"] == "video":
            fps = s.get("r_frame_rate", "30/1").split("/")
            fps = float(fps[0]) / float(fps[1])

            return {
                "width": int(s["width"]),
                "height": int(s["height"]),
                "fps": fps,
                "duration": float(meta["format"]["duration"]),
                "has_audio": any(x["codec_type"] == "audio" for x in meta["streams"])
            }


def find_videos():
    root = Path("videos")
    return sorted([p for p in root.rglob("*") if p.suffix.lower() in SUPPORTED_EXTENSIONS])


# ─── TIMESTAMP GENERATOR ─────────────────────────────────
def write_timestamps(video_list, output="timestamps.txt"):
    current = 0.0

    with open(output, "w") as f:
        f.write("VIDEO TIMESTAMPS\n=================\n\n")

        for i, v in enumerate(video_list, 1):
            info = get_video_info(Path(v))
            start = current
            end = current + info["duration"]

            f.write(f"{i}. {Path(v).name}\n")
            f.write(f"   Start: {start:.2f}s\n")
            f.write(f"   End:   {end:.2f}s\n\n")

            current = end


# ─── NORMALIZE ───────────────────────────────────────────
def normalize_video(src, dst, target, quality, progress_cb=None):
    info = get_video_info(src)

    vf = (
        f"scale={target['width']}:{target['height']}:force_original_aspect_ratio=decrease,"
        f"pad={target['width']}:{target['height']}:(ow-iw)/2:(oh-ih)/2:black"
    )

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", quality["crf"],
        "-preset", quality["preset"],
        "-c:a", "aac",
        "-b:a", "192k",
        "-progress", "pipe:1",
        "-nostats",
        dst
    ]

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    last = 0.0
    for line in process.stdout:
        if line.startswith("out_time_ms"):
            ms = int(line.split("=")[1])
            sec = ms / 1_000_000
            delta = sec - last
            last = sec
            if progress_cb:
                progress_cb(delta)

    process.wait()
    return dst


# ─── MERGE ───────────────────────────────────────────────
def apply_concat(clips, output):
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        for c in clips:
            f.write(f"file '{os.path.abspath(c)}'\n")
        list_file = f.name

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", output
    ], check=True)

    os.unlink(list_file)


# ─── MAIN ────────────────────────────────────────────────
def main():
    videos = find_videos()
    if not videos:
        log("No videos found")
        sys.exit(1)

    target = {"width": 1920, "height": 1080}
    quality = QUALITY_PRESETS[QUALITY]

    video_infos = [get_video_info(v) for v in videos]
    total_seconds = sum(v["duration"] for v in video_infos)

    log(f"Total duration: {total_seconds/60:.2f} min")

    progress = tqdm(total=total_seconds, unit="sec", desc="Processing")
    lock = threading.Lock()

    def update(x):
        with lock:
            progress.update(x)

    with tempfile.TemporaryDirectory() as tmp:
        normalized = [None] * len(videos)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {}

            for i, v in enumerate(videos):
                out = os.path.join(tmp, f"{i}.mp4")
                futures[ex.submit(normalize_video, v, out, target, quality, update)] = i

            for f in as_completed(futures):
                idx = futures[f]
                normalized[idx] = f.result()

        progress.close()

        # ─── TIMESTAMPS ───
        write_timestamps(videos, "timestamps.txt")

        # ─── MERGE ───
        apply_concat(normalized, OUTPUT_NAME + ".mp4")

        log("Done!")


if __name__ == "__main__":
    main()
