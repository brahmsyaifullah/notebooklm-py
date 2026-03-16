"""Railway HTTP runtime with health probes and NotebookLM JSON API."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class APIRequestError(Exception):
    """Raised for user-facing API request validation/runtime errors."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


API_PREFIX = "/api/notebooklm"
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}

_NB_IMPORTS: dict[str, Any] | None = None


def _auth_source() -> str:
    auth = os.environ.get("NOTEBOOKLM_AUTH_JSON", "").strip()
    if auth:
        return "env"

    home = os.environ.get("NOTEBOOKLM_HOME", "").strip()
    if home:
        storage = Path(home).expanduser().resolve() / "storage_state.json"
    else:
        storage = Path.home() / ".notebooklm" / "storage_state.json"

    return "file" if storage.exists() else "none"


def _storage_path() -> str:
    home = os.environ.get("NOTEBOOKLM_HOME", "").strip()
    if home:
        return str((Path(home).expanduser().resolve() / "storage_state.json"))
    return str((Path.home() / ".notebooklm" / "storage_state.json").resolve())


def _port() -> int:
    raw = os.environ.get("PORT", "8080").strip()
    try:
        port = int(raw)
    except ValueError:
        return 8080
    return port if 1 <= port <= 65535 else 8080


def _load_notebooklm_symbols() -> dict[str, Any]:
    """Load notebooklm symbols lazily, supporting repo-local execution."""
    global _NB_IMPORTS
    if _NB_IMPORTS is not None:
        return _NB_IMPORTS

    try:
        from notebooklm import (
            ArtifactType,
            AudioFormat,
            AudioLength,
            ChatGoal,
            ChatMode,
            ChatResponseLength,
            ExportType,
            InfographicDetail,
            InfographicOrientation,
            InfographicStyle,
            NotebookLMClient,
            QuizDifficulty,
            QuizQuantity,
            ReportFormat,
            SharePermission,
            ShareViewLevel,
            SlideDeckFormat,
            SlideDeckLength,
            VideoFormat,
            VideoStyle,
        )
    except ModuleNotFoundError:
        src_dir = Path(__file__).resolve().parent / "src"
        if src_dir.exists():
            sys.path.insert(0, str(src_dir))
        from notebooklm import (  # type: ignore[no-redef]
            ArtifactType,
            AudioFormat,
            AudioLength,
            ChatGoal,
            ChatMode,
            ChatResponseLength,
            ExportType,
            InfographicDetail,
            InfographicOrientation,
            InfographicStyle,
            NotebookLMClient,
            QuizDifficulty,
            QuizQuantity,
            ReportFormat,
            SharePermission,
            ShareViewLevel,
            SlideDeckFormat,
            SlideDeckLength,
            VideoFormat,
            VideoStyle,
        )

    _NB_IMPORTS = {
        "NotebookLMClient": NotebookLMClient,
        "ArtifactType": ArtifactType,
        "AudioFormat": AudioFormat,
        "AudioLength": AudioLength,
        "VideoFormat": VideoFormat,
        "VideoStyle": VideoStyle,
        "ReportFormat": ReportFormat,
        "QuizQuantity": QuizQuantity,
        "QuizDifficulty": QuizDifficulty,
        "InfographicOrientation": InfographicOrientation,
        "InfographicDetail": InfographicDetail,
        "InfographicStyle": InfographicStyle,
        "SlideDeckFormat": SlideDeckFormat,
        "SlideDeckLength": SlideDeckLength,
        "ChatMode": ChatMode,
        "ChatGoal": ChatGoal,
        "ChatResponseLength": ChatResponseLength,
        "SharePermission": SharePermission,
        "ShareViewLevel": ShareViewLevel,
        "ExportType": ExportType,
    }
    return _NB_IMPORTS


def _json_ready(value: Any) -> Any:
    """Convert rich Python objects (dataclass/enum/datetime) to JSON-safe values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        data = asdict(value)
        for attr in ("kind", "status_str", "is_ready", "is_completed", "is_failed"):
            if hasattr(value, attr):
                try:
                    data[attr] = getattr(value, attr)
                except Exception:
                    pass
        return _json_ready(data)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_ready(value.to_dict())
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(v) for v in value]
    return str(value)


def _coerce_enum(enum_cls: type[Enum], value: Any, *, field_name: str) -> Enum | None:
    """Convert string/int payload values into enum members."""
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value

    if isinstance(value, str):
        clean = value.strip()
        normalized = clean.upper().replace("-", "_").replace(" ", "_")
        if normalized in enum_cls.__members__:
            return enum_cls[normalized]
        for member in enum_cls:
            if str(member.value).lower() == clean.lower():
                return member
    else:
        for member in enum_cls:
            if member.value == value:
                return member

    raise APIRequestError(f"Invalid value for '{field_name}': {value!r}", status=400)


def _build_raw_transcript(segments: list[dict[str, Any]], speaker_names: dict[str, str]) -> str:
    def fmt_t(sec: float) -> str:
        mm = int(max(0, sec) // 60)
        ss = int(max(0, sec) % 60)
        return f"{mm:02d}:{ss:02d}"

    lines = []
    for seg in segments:
        start = float(seg.get("start", 0))
        speaker_idx = int(seg.get("speakerIdx", 0))
        speaker = speaker_names.get(str(speaker_idx), f"Speaker {speaker_idx + 1}")
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"[{fmt_t(start)}] {speaker}: {text}")
    return "\n".join(lines)


def _meeting_date(payload: dict[str, Any]) -> str:
    date_value = payload.get("date")
    if isinstance(date_value, str) and date_value.strip():
        return date_value.strip()
    return datetime.now(timezone.utc).date().isoformat()


async def _api_meeting_sync(payload: dict[str, Any]) -> dict[str, Any]:
    imports = _load_notebooklm_symbols()
    NotebookLMClient = imports["NotebookLMClient"]

    title = str(payload.get("title") or "Meeting Recording").strip()
    context = str(payload.get("context") or "").strip()
    markdown = str(payload.get("markdown") or "").strip()
    notebook_id = str(payload.get("notebook_id") or "").strip() or None
    wait_ready = bool(payload.get("wait_ready", True))

    segments_in = payload.get("segments") or []
    if not isinstance(segments_in, list):
        raise APIRequestError("'segments' must be a list", status=400)
    segments: list[dict[str, Any]] = [s for s in segments_in if isinstance(s, dict)]

    speaker_names_in = payload.get("speaker_names") or payload.get("speakerNames") or {}
    if not isinstance(speaker_names_in, dict):
        raise APIRequestError("'speaker_names' must be an object", status=400)
    speaker_names = {str(k): str(v) for k, v in speaker_names_in.items()}

    if not markdown and not segments:
        raise APIRequestError("No markdown or transcript segments provided", status=400)

    date_str = _meeting_date(payload)
    raw_transcript = _build_raw_transcript(segments, speaker_names)
    if context:
        context_block = f"Meeting context:\n{context}\n\n"
    else:
        context_block = ""
    if raw_transcript:
        raw_source_body = f"{context_block}{raw_transcript}"
    else:
        raw_source_body = context_block.strip()

    async with await NotebookLMClient.from_storage() as client:
        notebook = None
        if notebook_id:
            try:
                notebook = await client.notebooks.get(notebook_id)
            except Exception:
                notebook = None

        if notebook is None:
            notebook = await client.notebooks.create(f"{title} ({date_str})")
            notebook_id = notebook.id

        source_ids: list[str] = []
        added_sources = []

        if markdown:
            src = await client.sources.add_text(
                notebook_id,
                f"{title} - NotebookLM Optimized",
                markdown,
                wait=wait_ready,
                wait_timeout=240.0,
            )
            source_ids.append(src.id)
            added_sources.append(src)

        if raw_source_body:
            raw_src = await client.sources.add_text(
                notebook_id,
                f"{title} - Full Transcript",
                raw_source_body,
                wait=wait_ready,
                wait_timeout=240.0,
            )
            source_ids.append(raw_src.id)
            added_sources.append(raw_src)

        summary = await client.notebooks.get_summary(notebook_id)
        metadata = await client.notebooks.get_metadata(notebook_id)

        return {
            "notebook": notebook,
            "source_ids": source_ids,
            "sources_added": added_sources,
            "summary": summary,
            "metadata": metadata,
            "meeting_title": title,
            "date": date_str,
        }


async def _api_ask(payload: dict[str, Any]) -> Any:
    imports = _load_notebooklm_symbols()
    NotebookLMClient = imports["NotebookLMClient"]

    notebook_id = str(payload.get("notebook_id") or "").strip()
    question = str(payload.get("question") or "").strip()
    if not notebook_id or not question:
        raise APIRequestError("'notebook_id' and 'question' are required", status=400)

    source_ids = payload.get("source_ids")
    if source_ids is not None and not isinstance(source_ids, list):
        raise APIRequestError("'source_ids' must be a list when provided", status=400)

    conversation_id = payload.get("conversation_id")
    if conversation_id is not None:
        conversation_id = str(conversation_id).strip() or None

    async with await NotebookLMClient.from_storage() as client:
        return await client.chat.ask(
            notebook_id,
            question,
            source_ids=source_ids,
            conversation_id=conversation_id,
        )


async def _api_list_artifacts(payload: dict[str, Any]) -> Any:
    imports = _load_notebooklm_symbols()
    NotebookLMClient = imports["NotebookLMClient"]
    ArtifactType = imports["ArtifactType"]

    notebook_id = str(payload.get("notebook_id") or "").strip()
    if not notebook_id:
        raise APIRequestError("'notebook_id' is required", status=400)

    kind = payload.get("kind")
    artifact_type = None
    if kind not in (None, ""):
        artifact_type = _coerce_enum(ArtifactType, kind, field_name="kind")

    async with await NotebookLMClient.from_storage() as client:
        return await client.artifacts.list(notebook_id, artifact_type=artifact_type)




async def _api_download_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    """Download an artifact as base64. Uses notebooklm-py download_* methods directly."""
    import base64
    import tempfile
    import os as _os

    imports = _load_notebooklm_symbols()
    NotebookLMClient = imports["NotebookLMClient"]

    notebook_id  = str(payload.get("notebook_id")  or "").strip()
    kind         = str(payload.get("kind")         or "").strip().lower().replace("-", "_")
    artifact_id  = str(payload.get("artifact_id")  or "").strip() or None
    output_format = str(payload.get("output_format") or "").strip().lower() or None

    if not notebook_id or not kind:
        raise APIRequestError("'notebook_id' and 'kind' are required", status=400)

    # ── Mime helper ──────────────────────────────────────────────────────────
    MIME = {
        "mp3": "audio/mpeg", "wav": "audio/wav", "mp4": "video/mp4",
        "pdf": "application/pdf", "png": "image/png",
        "csv": "text/csv", "json": "application/json",
        "md": "text/markdown", "html": "text/html",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    async with await NotebookLMClient.from_storage() as client:
        arts = client.artifacts
        with tempfile.TemporaryDirectory() as tmpdir:

            # ── Per-kind download call ────────────────────────────────────────
            if kind == "audio":
                ext = "mp3"
                out = _os.path.join(tmpdir, f"audio.{ext}")
                r = arts.download_audio(notebook_id, out)

            elif kind == "video":
                ext = "mp4"
                out = _os.path.join(tmpdir, f"video.{ext}")
                r = arts.download_video(notebook_id, out)

            elif kind == "cinematic_video":
                ext = "mp4"
                out = _os.path.join(tmpdir, f"cinematic.{ext}")
                r = arts.download_cinematic_video(notebook_id, out)

            elif kind == "slide_deck":
                # Only PDF is publicly downloadable; PPTX requires NotebookLM Plus
                ext = "pdf"
                out = _os.path.join(tmpdir, f"slides.{ext}")
                r = arts.download_slide_deck(notebook_id, out)

            elif kind in ("report", "study_guide"):
                ext = "pdf"
                out = _os.path.join(tmpdir, f"report.{ext}")
                # download_report accepts optional artifact_id
                kw: dict[str, Any] = {}
                if artifact_id:
                    kw["artifact_id"] = artifact_id
                r = arts.download_report(notebook_id, out, **kw)

            elif kind == "quiz":
                fmt = output_format or "pdf"
                ext = fmt
                out = _os.path.join(tmpdir, f"quiz.{ext}")
                r = arts.download_quiz(notebook_id, out, format=fmt)

            elif kind == "flashcards":
                fmt = output_format or "pdf"
                ext = fmt
                out = _os.path.join(tmpdir, f"flashcards.{ext}")
                r = arts.download_flashcards(notebook_id, out, format=fmt)

            elif kind == "infographic":
                ext = "png"
                out = _os.path.join(tmpdir, f"infographic.{ext}")
                kw2: dict[str, Any] = {}
                if artifact_id:
                    kw2["artifact_id"] = artifact_id
                r = arts.download_infographic(notebook_id, out, **kw2)

            elif kind == "data_table":
                ext = "csv"
                out = _os.path.join(tmpdir, f"data_table.{ext}")
                r = arts.download_data_table(notebook_id, out)

            elif kind == "mind_map":
                ext = "json"
                out = _os.path.join(tmpdir, f"mind_map.{ext}")
                r = arts.download_mind_map(notebook_id, out)

            else:
                raise APIRequestError(f"Unsupported artifact kind: {kind}", status=400)

            if inspect.isawaitable(r):
                await r

            if not _os.path.exists(out) or _os.path.getsize(out) == 0:
                raise APIRequestError(
                    f"download_{kind} wrote no output — artifact may still be processing",
                    status=500,
                )

            with open(out, "rb") as fh:
                raw = fh.read()

    return {
        "kind": kind,
        "ext": ext,
        "mime": MIME.get(ext, "application/octet-stream"),
        "size_bytes": len(raw),
        "content": base64.b64encode(raw).decode("ascii"),
    }

async def _api_generate(payload: dict[str, Any]) -> dict[str, Any]:
    imports = _load_notebooklm_symbols()
    NotebookLMClient = imports["NotebookLMClient"]

    notebook_id = str(payload.get("notebook_id") or "").strip()
    artifact_kind = str(payload.get("type") or "").strip().lower()
    if not notebook_id or not artifact_kind:
        raise APIRequestError("'notebook_id' and 'type' are required", status=400)

    options = payload.get("options") or {}
    if not isinstance(options, dict):
        raise APIRequestError("'options' must be an object", status=400)

    wait = bool(payload.get("wait", False))
    timeout = float(payload.get("wait_timeout", 1200.0))
    source_ids = options.get("source_ids")
    if source_ids is not None and not isinstance(source_ids, list):
        raise APIRequestError("'options.source_ids' must be a list", status=400)

    language = str(options.get("language") or "en")
    instructions = options.get("instructions")

    async with await NotebookLMClient.from_storage() as client:
        status: Any = None
        kind = artifact_kind.replace("-", "_")

        if kind == "audio":
            status = await client.artifacts.generate_audio(
                notebook_id,
                source_ids=source_ids,
                language=language,
                instructions=instructions,
                audio_format=_coerce_enum(
                    imports["AudioFormat"], options.get("audio_format"), field_name="audio_format"
                ),
                audio_length=_coerce_enum(
                    imports["AudioLength"], options.get("audio_length"), field_name="audio_length"
                ),
            )
        elif kind == "video":
            status = await client.artifacts.generate_video(
                notebook_id,
                source_ids=source_ids,
                language=language,
                instructions=instructions,
                video_format=_coerce_enum(
                    imports["VideoFormat"], options.get("video_format"), field_name="video_format"
                ),
                video_style=_coerce_enum(
                    imports["VideoStyle"], options.get("video_style"), field_name="video_style"
                ),
            )
        elif kind in ("cinematic_video", "video_cinematic"):
            status = await client.artifacts.generate_cinematic_video(
                notebook_id,
                source_ids=source_ids,
                language=language,
                instructions=instructions,
            )
        elif kind in ("report", "study_guide"):
            report_format = (
                imports["ReportFormat"].STUDY_GUIDE
                if kind == "study_guide"
                else _coerce_enum(
                    imports["ReportFormat"],
                    options.get("report_format") or "briefing_doc",
                    field_name="report_format",
                )
            )
            status = await client.artifacts.generate_report(
                notebook_id,
                report_format=report_format,
                source_ids=source_ids,
                language=language,
                custom_prompt=options.get("custom_prompt"),
                extra_instructions=options.get("extra_instructions") or instructions,
            )
        elif kind == "quiz":
            status = await client.artifacts.generate_quiz(
                notebook_id,
                source_ids=source_ids,
                instructions=instructions,
                quantity=_coerce_enum(
                    imports["QuizQuantity"], options.get("quantity"), field_name="quantity"
                ),
                difficulty=_coerce_enum(
                    imports["QuizDifficulty"], options.get("difficulty"), field_name="difficulty"
                ),
            )
        elif kind == "flashcards":
            status = await client.artifacts.generate_flashcards(
                notebook_id,
                source_ids=source_ids,
                instructions=instructions,
                quantity=_coerce_enum(
                    imports["QuizQuantity"], options.get("quantity"), field_name="quantity"
                ),
                difficulty=_coerce_enum(
                    imports["QuizDifficulty"], options.get("difficulty"), field_name="difficulty"
                ),
            )
        elif kind == "infographic":
            status = await client.artifacts.generate_infographic(
                notebook_id,
                source_ids=source_ids,
                language=language,
                instructions=instructions,
                orientation=_coerce_enum(
                    imports["InfographicOrientation"],
                    options.get("orientation"),
                    field_name="orientation",
                ),
                detail_level=_coerce_enum(
                    imports["InfographicDetail"],
                    options.get("detail_level"),
                    field_name="detail_level",
                ),
                style=_coerce_enum(
                    imports["InfographicStyle"], options.get("style"), field_name="style"
                ),
            )
        elif kind in ("slide_deck", "slidedeck"):
            status = await client.artifacts.generate_slide_deck(
                notebook_id,
                source_ids=source_ids,
                language=language,
                instructions=instructions,
                slide_format=_coerce_enum(
                    imports["SlideDeckFormat"], options.get("slide_format"), field_name="slide_format"
                ),
                slide_length=_coerce_enum(
                    imports["SlideDeckLength"], options.get("slide_length"), field_name="slide_length"
                ),
            )
        elif kind == "data_table":
            status = await client.artifacts.generate_data_table(
                notebook_id,
                source_ids=source_ids,
                language=language,
                instructions=instructions,
            )
        elif kind == "mind_map":
            result = await client.artifacts.generate_mind_map(notebook_id, source_ids=source_ids)
            return {"kind": "mind_map", "result": result}
        elif kind == "revise_slide":
            artifact_id = str(options.get("artifact_id") or "").strip()
            slide_index = int(options.get("slide_index", 0))
            prompt = str(options.get("prompt") or "").strip()
            if not artifact_id or not prompt:
                raise APIRequestError(
                    "'revise_slide' requires options.artifact_id and options.prompt", status=400
                )
            status = await client.artifacts.revise_slide(
                notebook_id,
                artifact_id=artifact_id,
                slide_index=slide_index,
                prompt=prompt,
            )
        else:
            raise APIRequestError(f"Unsupported artifact type: {artifact_kind}", status=400)

        status_state = getattr(status, "status", None)
        status_failed = bool(getattr(status, "is_failed", False)) or status_state == "failed"
        if status_failed:
            err_msg = getattr(status, "error", None) or f"Generation failed for type: {artifact_kind}"
            err_code = str(getattr(status, "error_code", "") or "")
            http_status = 429 if err_code == "USER_DISPLAYABLE_ERROR" else 400
            raise APIRequestError(err_msg, status=http_status)

        waited_status = None
        artifact = None
        if wait and status is not None and getattr(status, "task_id", None):
            waited_status = await client.artifacts.wait_for_completion(
                notebook_id,
                status.task_id,
                timeout=timeout,
            )
            artifact = await client.artifacts.get(notebook_id, status.task_id)

        return {
            "requested_type": artifact_kind,
            "status": status,
            "waited_status": waited_status,
            "artifact": artifact,
        }


def _allowed_invoke_methods() -> dict[str, set[str]]:
    return {
        "notebooks": {
            "list",
            "create",
            "get",
            "delete",
            "rename",
            "get_summary",
            "get_description",
            "get_metadata",
            "share",
            "get_share_url",
            "remove_from_recent",
            "get_raw",
        },
        "sources": {
            "list",
            "get",
            "add_url",
            "add_text",
            "add_file",
            "add_drive",
            "delete",
            "rename",
            "refresh",
            "check_freshness",
            "get_guide",
            "get_fulltext",
            "wait_until_ready",
            "wait_for_sources",
        },
        "chat": {
            "ask",
            "get_conversation_turns",
            "get_conversation_id",
            "get_history",
            "configure",
            "set_mode",
        },
        "artifacts": {
            "list",
            "get",
            "delete",
            "rename",
            "list_audio",
            "list_video",
            "list_reports",
            "list_quizzes",
            "list_flashcards",
            "list_infographics",
            "list_slide_decks",
            "list_data_tables",
            "generate_audio",
            "generate_video",
            "generate_cinematic_video",
            "generate_report",
            "generate_study_guide",
            "generate_quiz",
            "generate_flashcards",
            "generate_infographic",
            "generate_slide_deck",
            "revise_slide",
            "generate_data_table",
            "generate_mind_map",
            "poll_status",
            "wait_for_completion",
            "export",
            "export_report",
            "export_data_table",
            "suggest_reports",
        },
        "research": {"start", "poll", "import_sources"},
        "notes": {"list", "get", "create", "update", "delete", "list_mind_maps", "delete_mind_map"},
        "settings": {"get_output_language", "set_output_language"},
        "sharing": {
            "get_status",
            "set_public",
            "set_view_level",
            "add_user",
            "update_user",
            "remove_user",
        },
    }


def _coerce_invoke_kwargs(
    namespace: str, method: str, kwargs: dict[str, Any], imports: dict[str, Any]
) -> dict[str, Any]:
    enum_rules: dict[tuple[str, str], dict[str, str]] = {
        ("artifacts", "list"): {"artifact_type": "ArtifactType"},
        ("artifacts", "generate_audio"): {
            "audio_format": "AudioFormat",
            "audio_length": "AudioLength",
        },
        ("artifacts", "generate_video"): {
            "video_format": "VideoFormat",
            "video_style": "VideoStyle",
        },
        ("artifacts", "generate_report"): {"report_format": "ReportFormat"},
        ("artifacts", "generate_quiz"): {
            "quantity": "QuizQuantity",
            "difficulty": "QuizDifficulty",
        },
        ("artifacts", "generate_flashcards"): {
            "quantity": "QuizQuantity",
            "difficulty": "QuizDifficulty",
        },
        ("artifacts", "generate_infographic"): {
            "orientation": "InfographicOrientation",
            "detail_level": "InfographicDetail",
            "style": "InfographicStyle",
        },
        ("artifacts", "generate_slide_deck"): {
            "slide_format": "SlideDeckFormat",
            "slide_length": "SlideDeckLength",
        },
        ("artifacts", "export"): {"export_type": "ExportType"},
        ("artifacts", "export_report"): {"export_type": "ExportType"},
        ("chat", "set_mode"): {"mode": "ChatMode"},
        ("chat", "configure"): {
            "goal": "ChatGoal",
            "response_length": "ChatResponseLength",
        },
        ("sharing", "set_view_level"): {"level": "ShareViewLevel"},
        ("sharing", "add_user"): {"permission": "SharePermission"},
        ("sharing", "update_user"): {"permission": "SharePermission"},
    }

    rules = enum_rules.get((namespace, method), {})
    coerced = dict(kwargs)
    for field, enum_name in rules.items():
        if field in coerced:
            coerced[field] = _coerce_enum(imports[enum_name], coerced[field], field_name=field)
    return coerced


async def _api_invoke(payload: dict[str, Any]) -> Any:
    imports = _load_notebooklm_symbols()
    NotebookLMClient = imports["NotebookLMClient"]

    namespace = str(payload.get("namespace") or "").strip()
    method = str(payload.get("method") or "").strip()
    args = payload.get("args") or []
    kwargs = payload.get("kwargs") or {}

    if not namespace or not method:
        raise APIRequestError("'namespace' and 'method' are required", status=400)
    if not isinstance(args, list):
        raise APIRequestError("'args' must be an array", status=400)
    if not isinstance(kwargs, dict):
        raise APIRequestError("'kwargs' must be an object", status=400)

    allowed = _allowed_invoke_methods()
    if namespace not in allowed:
        raise APIRequestError(f"Unsupported namespace: {namespace}", status=400)
    if method not in allowed[namespace]:
        raise APIRequestError(f"Method not allowed: {namespace}.{method}", status=403)

    kwargs = _coerce_invoke_kwargs(namespace, method, kwargs, imports)

    async with await NotebookLMClient.from_storage() as client:
        api_obj = getattr(client, namespace, None)
        if api_obj is None:
            raise APIRequestError(f"Namespace unavailable: {namespace}", status=500)

        fn = getattr(api_obj, method, None)
        if fn is None or method.startswith("_"):
            raise APIRequestError(f"Method unavailable: {namespace}.{method}", status=400)

        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result


def _capabilities_payload() -> dict[str, Any]:
    allowed = _allowed_invoke_methods()
    return {
        "status": "ok",
        "service": "notebooklm-py",
        "api": {
            "prefix": API_PREFIX,
            "routes": [
                f"{API_PREFIX}/capabilities",
                f"{API_PREFIX}/meeting-sync",
                f"{API_PREFIX}/ask",
                f"{API_PREFIX}/generate",
                f"{API_PREFIX}/artifacts",
                f"{API_PREFIX}/download",
                f"{API_PREFIX}/invoke",
            ],
            "invoke_namespaces": {k: sorted(v) for k, v in allowed.items()},
            "artifact_types": [
                "audio",
                "video",
                "cinematic_video",
                "report",
                "study_guide",
                "quiz",
                "flashcards",
                "infographic",
                "slide_deck",
                "data_table",
                "mind_map",
                "revise_slide",
            ],
        },
        "auth_source": _auth_source(),
        "paths": {"storage_state": _storage_path()},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "notebooklm-railway/2.0"

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        for key, value in CORS_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == f"{API_PREFIX}/capabilities":
            self._write_json(_capabilities_payload(), 200)
            return

        if path not in ("/", "/health", "/ready"):
            self._write_json({"status": "not_found"}, 404)
            return

        payload = {
            "status": "ok",
            "service": "notebooklm-py",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "auth_source": _auth_source(),
            "paths": {
                "storage_state": _storage_path(),
            },
            "note": "NotebookLM API available under /api/notebooklm/*",
        }
        self._write_json(payload, 200)

    def _check_api_auth(self) -> None:
        expected = os.environ.get("NOTEBOOKLM_API_KEY", "").strip()
        if not expected:
            return

        auth = self.headers.get("Authorization", "")
        x_key = self.headers.get("X-API-Key", "")

        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        elif x_key:
            token = x_key.strip()

        if token != expected:
            raise APIRequestError("Unauthorized", status=401)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            self._check_api_auth()
            payload = self._read_json()
            data = self._dispatch_post(path, payload)
            self._write_json({"ok": True, "data": _json_ready(data)}, 200)
        except APIRequestError as exc:
            self._write_json({"ok": False, "error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self._write_json({"ok": False, "error": str(exc)}, 500)

    def _dispatch_post(self, path: str, payload: dict[str, Any]) -> Any:
        if path == f"{API_PREFIX}/meeting-sync":
            return asyncio.run(_api_meeting_sync(payload))
        if path == f"{API_PREFIX}/ask":
            return asyncio.run(_api_ask(payload))
        if path == f"{API_PREFIX}/generate":
            return asyncio.run(_api_generate(payload))
        if path == f"{API_PREFIX}/artifacts":
            return asyncio.run(_api_list_artifacts(payload))
        if path == f"{API_PREFIX}/download":
            return asyncio.run(_api_download_artifact(payload))
        if path == f"{API_PREFIX}/invoke":
            return asyncio.run(_api_invoke(payload))
        raise APIRequestError("Not found", status=404)

    def _read_json(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length", "0").strip() or "0"
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise APIRequestError("Invalid Content-Length header", status=400) from exc

        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise APIRequestError("Invalid JSON body", status=400) from exc
        if not isinstance(data, dict):
            raise APIRequestError("JSON body must be an object", status=400)
        return data

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def _write_json(self, payload: dict[str, Any], status: int) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in CORS_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = _port()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[notebooklm-py] listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()