from __future__ import annotations

import csv
import io
import json
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st
from yt_dlp import YoutubeDL
from yt_dlp.version import __version__ as YT_DLP_VERSION


INSTAGRAM_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,}$")
FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


PRESETS: dict[str, dict[str, Any]] = {
    "Default": {
        "format": "bv*+ba/best",
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    },
    "Conservative": {
        "format": "bv*+ba/best",
        "retries": 8,
        "fragment_retries": 8,
        "socket_timeout": 45,
        "sleep_interval": 2,
        "max_sleep_interval": 8,
    },
}


@dataclass(frozen=True)
class MediaItem:
    observation_id: str
    url: str
    source: str
    author: str = ""
    caption: str = ""


class StreamlitLogger:
    def __init__(self, logs: list[str]) -> None:
        self.logs = logs

    def debug(self, message: str) -> None:
        if message.startswith("[debug] "):
            return
        self.logs.append(message)

    def warning(self, message: str) -> None:
        self.logs.append(f"Warning: {message}")

    def error(self, message: str) -> None:
        self.logs.append(f"Error: {message}")


def first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"nan", "none", "null"}:
            return text
    return ""


def instagram_url_from_code(code: str) -> str:
    return f"https://www.instagram.com/reel/{code.strip('/')}/"


def safe_filename_part(value: str, fallback: str = "observation") -> str:
    safe = FILENAME_SAFE_RE.sub("_", value.strip()).strip("._")
    return safe[:120] or fallback


def normalize_url(value: Any) -> str:
    text = first_text(value)
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    if INSTAGRAM_CODE_RE.match(text):
        return instagram_url_from_code(text)
    return ""


def read_csv_columns(uploaded_file: Any) -> list[str]:
    content = uploaded_file.getvalue().decode("utf-8-sig")
    reader = csv.reader(io.StringIO(content))
    try:
        return [name for name in next(reader) if name]
    except StopIteration:
        return []


def parse_csv_file(uploaded_file: Any, url_column: str) -> list[MediaItem]:
    content = uploaded_file.getvalue().decode("utf-8-sig")
    rows = csv.DictReader(io.StringIO(content))
    items: list[MediaItem] = []

    for row_number, row in enumerate(rows, start=2):
        observation_id = first_text(row.get("id"), row.get("thread_id"), row.get("parent_id"), row_number)
        text = first_text(row.get(url_column))
        if not text.startswith(("http://", "https://")):
            continue

        items.append(
            MediaItem(
                observation_id=observation_id,
                url=text,
                source="csv",
                author=first_text(row.get("author"), row.get("author_fullname"), row.get("author_id")),
                caption=first_text(row.get("body"))[:240],
            )
        )

    return items


def parse_ndjson_file(uploaded_file: Any) -> list[MediaItem]:
    content = uploaded_file.getvalue().decode("utf-8-sig")
    items: list[MediaItem] = []

    for row_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue

        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        raw_data = record.get("data")
        if isinstance(raw_data, dict) and raw_data:
            data = raw_data
            meta = record
        elif isinstance(record.get("__import_meta"), dict):
            # 4CAT-converted NDJSON: payload hoisted to the top level, envelope under __import_meta
            data = record
            meta = record["__import_meta"]
        else:
            data = {}
            meta = record

        platform = first_text(meta.get("source_platform"))

        if platform == "tiktok.com":
            author = data.get("author") if isinstance(data.get("author"), dict) else {}
            unique_id = first_text(author.get("uniqueId"))
            video_id = first_text(data.get("id"), meta.get("item_id"))
            if not unique_id or not video_id:
                continue
            items.append(
                MediaItem(
                    observation_id=first_text(meta.get("item_id"), video_id, row_number),
                    url=f"https://www.tiktok.com/@{unique_id}/video/{video_id}",
                    source="ndjson",
                    author=first_text(author.get("uniqueId"), author.get("nickname")),
                    caption=first_text(data.get("desc"))[:240],
                )
            )
            continue

        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        caption = data.get("caption") if isinstance(data.get("caption"), dict) else {}

        code = first_text(data.get("code"))
        url = normalize_url(
            first_text(
                code,
                data.get("url"),
                meta.get("source_platform_url"),
            )
        )
        if not url or url.rstrip("/") in {"https://www.instagram.com/reels", "https://www.instagram.com/reel"}:
            url = instagram_url_from_code(code) if code else ""
        if not url:
            continue

        items.append(
            MediaItem(
                observation_id=first_text(meta.get("item_id"), data.get("id"), code, row_number),
                url=url,
                source="ndjson",
                author=first_text(user.get("username"), user.get("full_name")),
                caption=first_text(caption.get("text"))[:240],
            )
        )

    return items


def dedupe_items(items: list[MediaItem]) -> list[MediaItem]:
    seen: set[str] = set()
    deduped: list[MediaItem] = []
    for item in items:
        key = item.url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def build_output_template(output_dir: Path) -> str:
    return str(output_dir / "%(extractor_key)s_%(id)s.%(ext)s")


def build_item_output_template(output_dir: Path, item: MediaItem) -> str:
    observation_id = safe_filename_part(item.observation_id)
    return str(output_dir / f"{observation_id}.%(ext)s")


def build_command_log(item: MediaItem, output_template: str, cookie_file: Path | None, preset: str) -> str:
    command = [
        "yt-dlp",
        "--format",
        PRESETS[preset]["format"],
        "--write-info-json",
        "--merge-output-format",
        "mp4",
        "--no-playlist",
        "--no-overwrites",
        "--output",
        output_template,
    ]
    if cookie_file:
        command.extend(["--cookies", str(cookie_file)])
    command.append(item.url)
    return " ".join(shlex.quote(part) for part in command)


def append_row_log(output_dir: Path, row: dict[str, Any]) -> None:
    row["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    row["yt_dlp_version"] = YT_DLP_VERSION
    with (output_dir / "sour-stingray-download-log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def item_files(output_dir: Path, item: MediaItem) -> list[str]:
    observation_id = safe_filename_part(item.observation_id)
    return sorted(path.name for path in output_dir.glob(f"{observation_id}.*") if path.is_file())


def download_items(
    items: list[MediaItem],
    output_dir: Path,
    *,
    preset: str,
    inter_request_delay: float,
    cookie_file: Path | None,
) -> tuple[int, int, list[str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []
    status_box = st.empty()
    progress_bar = st.progress(0)

    def progress_hook(data: dict[str, Any]) -> None:
        if data.get("status") == "downloading":
            filename = Path(first_text(data.get("filename"))).name
            percent = first_text(data.get("_percent_str")).strip()
            speed = first_text(data.get("_speed_str")).strip()
            status_box.write(f"Downloading `{filename}` {percent} {speed}".strip())
        elif data.get("status") == "finished":
            filename = Path(first_text(data.get("filename"))).name
            logs.append(f"Finished: {filename}")

    base_ydl_opts = {
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "ignoreerrors": False,
        "continuedl": True,
        "nooverwrites": True,
        "noplaylist": True,
        "writeinfojson": True,
        "quiet": True,
        "logger": StreamlitLogger(logs),
        "progress_hooks": [progress_hook],
    }
    base_ydl_opts.update(PRESETS[preset])
    if cookie_file:
        base_ydl_opts["cookiefile"] = str(cookie_file)

    successes = 0
    failures = 0

    for index, item in enumerate(items, start=1):
        progress_bar.progress((index - 1) / max(len(items), 1))
        status_box.write(f"Starting `{item.url}`")
        output_template = build_item_output_template(output_dir, item)
        ydl_opts = dict(base_ydl_opts)
        ydl_opts["outtmpl"] = output_template
        command = build_command_log(item, output_template, cookie_file, preset)
        started_at = time.monotonic()

        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([item.url])
            successes += 1
            logs.append(f"OK: {item.observation_id} {item.url}")
            append_row_log(
                output_dir,
                {
                    "status": "ok",
                    "observation_id": item.observation_id,
                    "source": item.source,
                    "url": item.url,
                    "command": command,
                    "preset": preset,
                    "output_template": output_template,
                    "files": item_files(output_dir, item),
                    "elapsed_seconds": round(time.monotonic() - started_at, 3),
                },
            )
        except Exception as exc:
            failures += 1
            logs.append(f"Failed: {item.observation_id} {item.url} ({exc})")
            append_row_log(
                output_dir,
                {
                    "status": "failed",
                    "observation_id": item.observation_id,
                    "source": item.source,
                    "url": item.url,
                    "command": command,
                    "preset": preset,
                    "error": str(exc),
                    "files": item_files(output_dir, item),
                    "elapsed_seconds": round(time.monotonic() - started_at, 3),
                },
            )

        if inter_request_delay > 0 and index < len(items):
            status_box.write(f"Waiting {inter_request_delay:g}s before the next request")
            time.sleep(inter_request_delay)

    progress_bar.progress(1.0)
    status_box.write("Done")
    return successes, failures, logs


def render_app() -> None:
    st.set_page_config(page_title="Sour Stingray", page_icon=":material/download:", layout="wide")
    st.title("Sour Stingray")
    st.caption("Collect social media videos from CSV or Zeeschuimer NDJSON exports with yt-dlp.")

    uploaded_file = st.file_uploader("Input file", type=["csv", "ndjson", "jsonl"])
    output_dir_text = st.text_input("Download directory", value=str(Path.cwd() / "downloads"))
    preset = st.selectbox(
        "Download profile",
        options=list(PRESETS),
        index=0,
        help=(
            "Controls retry and throttling behavior; the extractor itself is auto-selected by yt-dlp from the URL.\n\n"
            "- **Default**: 3 retries, 30s socket timeout, no inter-request sleep. Use for normal runs.\n"
            "- **Conservative**: 8 retries, 45s socket timeout, random 2–8s sleep between requests. "
            "Use when a platform is rate-limiting you or fragments keep failing."
        ),
    )
    inter_request_delay = st.number_input(
        "Inter-request delay in seconds",
        min_value=0.0,
        max_value=300.0,
        value=3.0,
        step=1.0,
    )
    with st.expander("How to export a cookies.txt file"):
        st.markdown(
            "Some platforms (Instagram, TikTok, private YouTube, Facebook, X/Twitter) "
            "require a logged-in session to fetch videos. Provide one as a "
            "Netscape-format `cookies.txt`:\n\n"
            "1. Install a browser extension that exports cookies, e.g. "
            "`Get cookies.txt LOCALLY` for Chromium browsers or `cookies.txt` for Firefox.\n"
            "2. Log into the target platform in your browser (e.g. `instagram.com`, `tiktok.com`).\n"
            "3. Open the extension on that tab and export cookies for the current site in Netscape format.\n"
            "4. Upload the resulting `.txt` file below.\n\n"
            "The file is copied into your download directory as "
            "`.sour-stingray-cookies.txt`; delete it when you no longer need it."
        )

    cookie_upload = st.file_uploader(
        "Cookie file",
        type=["txt"],
        help="Optional Netscape-format cookies.txt for authenticated platforms.",
    )

    if not uploaded_file:
        st.info("Upload a CSV or Zeeschuimer NDJSON export to begin.")
        return

    suffix = Path(uploaded_file.name).suffix.lower()
    if suffix == ".csv":
        columns = read_csv_columns(uploaded_file)
        if not columns:
            st.error("Could not read column headers from the CSV.")
            return
        default_index = next(
            (i for i, name in enumerate(columns) if "url" in name.lower()),
            0,
        )
        url_column = st.selectbox(
            "URL column",
            options=columns,
            index=default_index,
            help=(
                "Pick the CSV column whose values are full http(s) URLs to the videos. "
                "Rows where this column is empty or not a URL will be skipped."
            ),
        )
        parsed_items = parse_csv_file(uploaded_file, url_column)
    elif suffix in {".ndjson", ".jsonl"}:
        parsed_items = parse_ndjson_file(uploaded_file)
    else:
        st.error("Upload a .csv, .ndjson, or .jsonl file.")
        return

    items = dedupe_items(parsed_items)
    skipped_duplicates = len(parsed_items) - len(items)

    left, right = st.columns(2)
    left.metric("Media URLs", len(items))
    right.metric("Duplicates skipped", skipped_duplicates)

    if not items:
        st.warning("No downloadable URLs were found in the uploaded file.")
        return

    with st.expander("Preview", expanded=True):
        st.dataframe(
            [
                {
                    "observation_id": item.observation_id,
                    "url": item.url,
                    "author": item.author,
                    "caption": item.caption,
                }
                for item in items
            ],
            width="stretch",
            hide_index=True,
        )

    limit = st.number_input(
        "Maximum downloads",
        min_value=1,
        max_value=len(items),
        value=len(items),
        help="Use a smaller number for a test run.",
    )

    if st.button("Download videos", type="primary"):
        output_dir = Path(output_dir_text).expanduser()
        selected_items = items[: int(limit)]
        cookie_file = None
        if cookie_upload is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            cookie_file = output_dir / ".sour-stingray-cookies.txt"
            cookie_file.write_bytes(cookie_upload.getvalue())
        with st.status(f"Downloading {len(selected_items)} item(s) to {output_dir}", expanded=True):
            successes, failures, logs = download_items(
                selected_items,
                output_dir,
                preset=preset,
                inter_request_delay=float(inter_request_delay),
                cookie_file=cookie_file,
            )

        st.success(f"Completed: {successes} succeeded, {failures} failed.")
        st.caption(f"Per-row log: {output_dir / 'sour-stingray-download-log.jsonl'}")
        st.code("\n".join(logs) if logs else "No yt-dlp log output.", language="text")


if __name__ == "__main__":
    render_app()
