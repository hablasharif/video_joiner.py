"""
Advanced Video Joiner — Zero Configuration
Just place this script anywhere in your repo and run it.
It automatically finds videos, detects resolution, and saves output.
No folder paths needed.
"""

import os
import sys
import subprocess
import tempfile
import json
import shutil
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    console = Console()
    USE_RICH = True
except ImportError:
    USE_RICH = False

# ─── Config (change these if you want, but defaults just work) ────────────────
TRANSITION   = os.environ.get("TRANSITION", "fade")   # fade | crossfade | wipe | none
QUALITY      = os.environ.get("QUALITY",    "high")   # high | medium | low
OUTPUT_NAME  = os.environ.get("OUTPUT_NAME","merged_output")

QUALITY_PRESETS = {
    "high":   {"crf": "18", "preset": "slow"},
    "medium": {"crf": "23", "preset": "medium"},
    "low":    {"crf": "28", "preset": "fast"},
}

SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v"}


# ─── Auto-locate everything ───────────────────────────────────────────────────
def find_repo_root() -> Path:
    """Walk up from this script's location until we find .git or hit filesystem root."""
    current = Path(__file__).resolve().parent
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    # No .git found — just use the script's own directory
    return current


def find_videos(root: Path) -> list[Path]:
    """
    Search the entire repo for video files.
    Skips: .git, node_modules, __pycache__, and the merged output folder.
    Respects video_list.txt if one exists anywhere in the repo.
    """
    # Check for video_list.txt anywhere in repo
    lists = list(root.rglob("video_list.txt"))
    if lists:
        list_file = lists[0]
        log(f"📋 Found order file: {list_file.relative_to(root)}")
        lines = [l.strip() for l in list_file.read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
        resolved = []
        for line in lines:
            # Search repo-wide for the filename
            matches = list(root.rglob(line))
            matches = [m for m in matches if m.suffix.lower() in SUPPORTED_EXTENSIONS]
            if matches:
                resolved.append(matches[0])
            else:
                log(f"⚠️  '{line}' listed in video_list.txt but not found — skipping")
        return resolved

    # No list file — collect all videos from repo, skip common non-video dirs
    skip_dirs = {".git", "node_modules", "__pycache__", "merged", "output", "outputs"}
    videos = []
    for p in sorted(root.rglob("*")):
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.suffix.lower() in SUPPORTED_EXTENSIONS:
            videos.append(p)
    return videos


def make_output_path(root: Path) -> Path:
    """Create merged/ folder at repo root and return output file path."""
    out_dir = root / "merged"
    out_dir.mkdir(exist_ok=True)
    return out_dir / f"{OUTPUT_NAME}.mp4"


# ─── Helpers ─────────────────────────────────────────────────────────────────
def log(msg: str):
    if USE_RICH:
        console.print(msg)
    else:
        print(msg)


def run_ffprobe(path: Path) -> dict:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_streams", "-show_format", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {result.stderr}")
    return json.loads(result.stdout)


def get_video_info(path: Path) -> dict:
    meta = run_ffprobe(path)
    for stream in meta.get("streams", []):
        if stream.get("codec_type") == "video":
            fps_raw = stream.get("r_frame_rate", "30/1")
            num, den = fps_raw.split("/")
            fps = round(float(num) / float(den), 3)
            return {
                "width":     int(stream.get("width", 1920)),
                "height":    int(stream.get("height", 1080)),
                "fps":       fps,
                "duration":  float(meta["format"].get("duration", 0)),
                "has_audio": any(s["codec_type"] == "audio" for s in meta["streams"]),
            }
    raise RuntimeError(f"No video stream in {path}")


def get_highest_resolution(videos: list[Path]) -> tuple[int, int]:
    max_w, max_h = 0, 0
    for v in videos:
        info = get_video_info(v)
        if info["width"] * info["height"] > max_w * max_h:
            max_w, max_h = info["width"], info["height"]
    return max_w, max_h


# ─── Normalization ────────────────────────────────────────────────────────────
def normalize_video(src: Path, dst: str, target: dict, quality: dict):
    """Pad to target canvas — never upscales, preserves original pixels."""
    info = get_video_info(src)

    vf_chain = (
        f"pad={target['width']}:{target['height']}:"
        f"(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={target['fps']}"
    )

    audio_args = (
        ["-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "192k"]
        if info["has_audio"]
        else ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
              "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "192k", "-shortest"]
    )

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        *(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"] if not info["has_audio"] else []),
        "-vf", vf_chain,
        "-c:v", "libx264",
        "-crf", quality["crf"],
        "-preset", quality["preset"],
        *audio_args,
        "-movflags", "+faststart",
        dst
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ─── Transitions ─────────────────────────────────────────────────────────────
def apply_fade(clips: list[str], output: str, dur: float = 0.5):
    if len(clips) == 1:
        shutil.copy(clips[0], output); return
    inputs = []
    for c in clips:
        inputs += ["-i", c]
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
    cmd = ["ffmpeg", "-y", *inputs,
           "-filter_complex", ";".join(filter_parts),
           "-map", last_v, "-map", last_a,
           "-c:v", "libx264", "-crf", "18", "-preset", "medium",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", output]
    subprocess.run(cmd, check=True, capture_output=True)


def apply_wipe(clips: list[str], output: str):
    if len(clips) == 1:
        shutil.copy(clips[0], output); return
    inputs = []
    for c in clips:
        inputs += ["-i", c]
    durations = [get_video_info(Path(c))["duration"] for c in clips]
    filter_parts, last_v, last_a, offset, dur = [], "[0:v]", "[0:a]", 0.0, 0.7
    for i in range(1, len(clips)):
        offset += durations[i - 1] - dur
        nv, na = f"[vx{i}]", f"[ax{i}]"
        filter_parts += [
            f"{last_v}[{i}:v]xfade=transition=wipeleft:duration={dur}:offset={offset:.3f}{nv}",
            f"{last_a}[{i}:a]acrossfade=d={dur}{na}"
        ]
        last_v, last_a = nv, na
    cmd = ["ffmpeg", "-y", *inputs,
           "-filter_complex", ";".join(filter_parts),
           "-map", last_v, "-map", last_a,
           "-c:v", "libx264", "-crf", "18",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", output]
    subprocess.run(cmd, check=True, capture_output=True)


def apply_none(clips: list[str], output: str):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for c in clips:
            f.write(f"file '{os.path.abspath(c)}'\n")
        list_file = f.name
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", list_file, "-c", "copy", output],
                   check=True, capture_output=True)
    os.unlink(list_file)


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    root    = find_repo_root()
    videos  = find_videos(root)
    out     = make_output_path(root)
    quality = QUALITY_PRESETS[QUALITY]

    if not videos:
        log("❌ No video files found anywhere in the repo.")
        sys.exit(1)

    tw, th = get_highest_resolution(videos)

    log(Panel(
        f"[bold cyan]Repo root :[/bold cyan] {root}\n"
        f"[bold cyan]Videos    :[/bold cyan] {len(videos)} found\n"
        f"[bold cyan]Canvas    :[/bold cyan] {tw}x{th} (padding only — no upscale)\n"
        f"[bold cyan]Transition:[/bold cyan] {TRANSITION}  |  Quality: {QUALITY}\n"
        f"[bold cyan]Output    :[/bold cyan] {out}"
    ))

    if USE_RICH:
        table = Table(title="Videos to Join (in order)")
        table.add_column("#", style="dim")
        table.add_column("File")
        table.add_column("Resolution")
        table.add_column("FPS")
        table.add_column("Duration")
        for i, v in enumerate(videos, 1):
            info = get_video_info(v)
            table.add_row(str(i), v.name,
                          f"{info['width']}x{info['height']}",
                          str(info["fps"]), f"{info['duration']:.1f}s")
        console.print(table)

    target = {"width": tw, "height": th, "fps": 30.0}

    with tempfile.TemporaryDirectory() as tmp:
        log("\n[bold]Step 1/2:[/bold] Normalizing…")
        normalized = []
        for i, v in enumerate(videos):
            norm = os.path.join(tmp, f"norm_{i:04d}.mp4")
            log(f"  → [{i+1}/{len(videos)}] {v.name}")
            normalize_video(v, norm, target, quality)
            normalized.append(norm)

        log("\n[bold]Step 2/2:[/bold] Joining…")
        out_str = str(out)
        if TRANSITION in ("fade", "crossfade"):
            dur = 0.5 if TRANSITION == "fade" else 1.2
            apply_fade(normalized, out_str, dur)
        elif TRANSITION == "wipe":
            apply_wipe(normalized, out_str)
        else:
            apply_none(normalized, out_str)

    info = get_video_info(out)
    size = out.stat().st_size / (1024 * 1024)
    log(Panel(
        f"[bold green]✓ Done![/bold green]\n"
        f"Saved  : {out}\n"
        f"Size   : {size:.1f} MB\n"
        f"Duration: {info['duration']:.1f}s  |  {info['width']}x{info['height']} @ {info['fps']}fps"
    ))


if __name__ == "__main__":
    main()
