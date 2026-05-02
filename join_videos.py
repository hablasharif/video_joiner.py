"""
Advanced Video Joiner Script
Supports: fade, crossfade, wipe transitions | quality presets | re-encoding control
Usage: python scripts/join_videos.py --input-dir videos/ --output merged/output.mp4
"""

import os
import sys
import argparse
import subprocess
import tempfile
import json
import shutil
from pathlib import Path
from typing import Optional

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
    from rich.table import Table
    from rich.panel import Panel
    console = Console()
    USE_RICH = True
except ImportError:
    USE_RICH = False


# ─── Quality Presets ──────────────────────────────────────────────────────────
QUALITY_PRESETS = {
    "high":   {"crf": "18", "preset": "slow",   "bitrate": "8000k"},
    "medium": {"crf": "23", "preset": "medium",  "bitrate": "4000k"},
    "low":    {"crf": "28", "preset": "fast",    "bitrate": "2000k"},
}

SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v"}


# ─── Helpers ──────────────────────────────────────────────────────────────────
def log(msg: str, style: str = ""):
    if USE_RICH:
        console.print(msg, style=style)
    else:
        print(msg)


def run_ffprobe(video_path: str) -> dict:
    """Return video metadata using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(video_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {video_path}: {result.stderr}")
    return json.loads(result.stdout)


def get_video_info(path: str) -> dict:
    """Extract width, height, fps, duration from a video."""
    meta = run_ffprobe(path)
    for stream in meta.get("streams", []):
        if stream.get("codec_type") == "video":
            fps_raw = stream.get("r_frame_rate", "30/1")
            num, den = fps_raw.split("/")
            fps = round(float(num) / float(den), 3)
            return {
                "width":    int(stream.get("width", 1920)),
                "height":   int(stream.get("height", 1080)),
                "fps":      fps,
                "duration": float(meta["format"].get("duration", 0)),
                "has_audio": any(
                    s["codec_type"] == "audio" for s in meta["streams"]
                ),
            }
    raise RuntimeError(f"No video stream found in {path}")


def collect_videos(input_dir: str, video_list: Optional[str] = None) -> list[str]:
    """Collect and sort video files from directory or a text list."""
    if video_list and Path(video_list).exists():
        with open(video_list) as f:
            paths = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        return [str(Path(input_dir) / p) if not Path(p).is_absolute() else p for p in paths]

    videos = sorted(
        p for p in Path(input_dir).iterdir()
        if p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    return [str(v) for v in videos]


# ─── Normalization ────────────────────────────────────────────────────────────
def get_highest_resolution(videos: list[str]) -> tuple[int, int]:
    """Find the highest resolution (width x height) among all input videos."""
    max_w, max_h = 0, 0
    for v in videos:
        info = get_video_info(v)
        if info["width"] * info["height"] > max_w * max_h:
            max_w, max_h = info["width"], info["height"]
    return max_w, max_h


def normalize_video(input_path: str, output_path: str, target: dict, quality: dict) -> None:
    """
    Re-encode a video to a common spec using PADDING only — never upscales pixels.
    - Videos smaller than target get black bars (letterbox/pillarbox)
    - Videos matching target are re-encoded with no resize
    - FPS: normalized to target fps
    - Codec: H.264 + AAC
    - Audio: 44100 Hz stereo (add silent track if missing)
    """
    info = get_video_info(input_path)

    # Pad to target size WITHOUT scaling up — original pixels preserved
    vf_chain = (
        f"pad={target['width']}:{target['height']}:"
        f"(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={target['fps']}"
    )

    audio_args = (
        ["-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "192k"]
        if info["has_audio"]
        else ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
              "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "192k",
              "-shortest"]
    )

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        *(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"] if not info["has_audio"] else []),
        "-vf", vf_chain,
        "-c:v", "libx264",
        "-crf", quality["crf"],
        "-preset", quality["preset"],
        *audio_args,
        "-movflags", "+faststart",
        output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ─── Transitions ──────────────────────────────────────────────────────────────
def apply_fade_transition(clips: list[str], output: str, duration: float = 0.5) -> None:
    """Concatenate with fade-out/fade-in between clips using xfade filter."""
    if len(clips) == 1:
        shutil.copy(clips[0], output)
        return

    inputs = []
    for c in clips:
        inputs += ["-i", c]

    # Build xfade filter chain
    n = len(clips)
    filter_parts = []
    last_v = "[0:v]"
    last_a = "[0:a]"

    # Compute cumulative offset per clip
    durations = [get_video_info(c)["duration"] for c in clips]

    offset = 0.0
    for i in range(1, n):
        offset += durations[i - 1] - duration
        new_v = f"[vx{i}]"
        new_a = f"[ax{i}]"
        filter_parts.append(
            f"{last_v}[{i}:v]xfade=transition=fade:duration={duration}:offset={offset:.3f}{new_v}"
        )
        filter_parts.append(
            f"{last_a}[{i}:a]acrossfade=d={duration}{new_a}"
        )
        last_v = new_v
        last_a = new_a

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", last_v, "-map", last_a,
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def apply_crossfade_transition(clips: list[str], output: str, duration: float = 1.0) -> None:
    """Dissolve crossfade (longer overlap than fade)."""
    apply_fade_transition(clips, output, duration=duration)


def apply_wipe_transition(clips: list[str], output: str, direction: str = "wipeleft") -> None:
    """Wipe transition between clips."""
    if len(clips) == 1:
        shutil.copy(clips[0], output)
        return

    inputs = []
    for c in clips:
        inputs += ["-i", c]

    n = len(clips)
    filter_parts = []
    last_v = "[0:v]"
    last_a = "[0:a]"
    durations = [get_video_info(c)["duration"] for c in clips]
    offset = 0.0
    dur = 0.7

    for i in range(1, n):
        offset += durations[i - 1] - dur
        new_v = f"[vx{i}]"
        new_a = f"[ax{i}]"
        filter_parts.append(
            f"{last_v}[{i}:v]xfade=transition={direction}:duration={dur}:offset={offset:.3f}{new_v}"
        )
        filter_parts.append(
            f"{last_a}[{i}:a]acrossfade=d={dur}{new_a}"
        )
        last_v = new_v
        last_a = new_a

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", last_v, "-map", last_a,
        "-c:v", "libx264", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def apply_no_transition(clips: list[str], output: str) -> None:
    """Simple concat demuxer — fastest, no re-encode."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for c in clips:
            f.write(f"file '{os.path.abspath(c)}'\n")
        list_file = f.name

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        output
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    os.unlink(list_file)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Advanced Video Joiner")
    parser.add_argument("--input-dir",   default="videos/",   help="Folder containing videos")
    parser.add_argument("--video-list",  default=None,        help="Optional ordered text file of video names")
    parser.add_argument("--output",      default="merged/merged_output.mp4", help="Output path")
    parser.add_argument("--transition",  choices=["fade", "crossfade", "wipe", "none"], default="fade")
    parser.add_argument("--quality",     choices=["high", "medium", "low"], default="high")
    parser.add_argument("--target-res",  default=None, help="Target canvas WxH (default: auto = highest res among inputs)")
    parser.add_argument("--target-fps",  default=30.0, type=float)
    args = parser.parse_args()

    quality = QUALITY_PRESETS[args.quality]

    # ── Collect ───────────────────────────────────────────────────────────────
    videos = collect_videos(args.input_dir, args.video_list)
    if not videos:
        log("[bold red]No video files found![/bold red]")
        sys.exit(1)

    # ── Auto-detect highest resolution among all inputs ───────────────────────
    if args.target_res:
        tw, th = map(int, args.target_res.split("x"))
    else:
        tw, th = get_highest_resolution(videos)
        log(f"[cyan]Auto-detected highest resolution: {tw}x{th} (used for padding)[/cyan]")

    target = {"width": tw, "height": th, "fps": args.target_fps}

    log(Panel(
        f"[bold cyan]Found {len(videos)} video(s)[/bold cyan]\n"
        f"Canvas  : [magenta]{tw}x{th}[/magenta] (padding only — no upscale)\n"
        f"Transition: [yellow]{args.transition}[/yellow]  |  Quality: [green]{args.quality}[/green]"
    ))

    # ── Display info table ────────────────────────────────────────────────────
    if USE_RICH:
        table = Table(title="Input Videos")
        table.add_column("#", style="dim")
        table.add_column("File")
        table.add_column("Resolution")
        table.add_column("FPS")
        table.add_column("Duration")
        for i, v in enumerate(videos, 1):
            info = get_video_info(v)
            table.add_row(
                str(i), Path(v).name,
                f"{info['width']}x{info['height']}",
                str(info["fps"]),
                f"{info['duration']:.1f}s"
            )
        console.print(table)

    # ── Normalize ─────────────────────────────────────────────────────────────
    out_dir = Path(args.output).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    normalized = []
    with tempfile.TemporaryDirectory() as tmp:
        log("\n[bold]Step 1/2:[/bold] Normalizing videos…")
        for i, v in enumerate(videos):
            norm_path = os.path.join(tmp, f"norm_{i:04d}.mp4")
            log(f"  → [{i+1}/{len(videos)}] {Path(v).name}")
            normalize_video(v, norm_path, target, quality)
            normalized.append(norm_path)

        # ── Join ──────────────────────────────────────────────────────────────
        log("\n[bold]Step 2/2:[/bold] Joining with transition…")
        out = str(args.output)

        if args.transition == "fade":
            apply_fade_transition(normalized, out)
        elif args.transition == "crossfade":
            apply_crossfade_transition(normalized, out, duration=1.2)
        elif args.transition == "wipe":
            apply_wipe_transition(normalized, out)
        else:
            apply_no_transition(normalized, out)

    # ── Summary ───────────────────────────────────────────────────────────────
    final_info = get_video_info(out)
    size_mb = Path(out).stat().st_size / (1024 * 1024)
    log(Panel(
        f"[bold green]✓ Done![/bold green]\n"
        f"Output : [cyan]{out}[/cyan]\n"
        f"Size   : [yellow]{size_mb:.1f} MB[/yellow]\n"
        f"Duration: {final_info['duration']:.1f}s  |  "
        f"{final_info['width']}x{final_info['height']} @ {final_info['fps']}fps"
    ))


if __name__ == "__main__":
    main()
