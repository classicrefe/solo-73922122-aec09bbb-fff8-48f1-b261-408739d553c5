"""Pydantic models and JSONL upload parsing/validation."""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from . import config

# Accept canonical English field names plus the Chinese aliases used by the
# voice-data team. Stored output is always canonical English.
ALIASES = {
    "record_id": "record_id",
    "recordId": "record_id",
    "记录号": "record_id",
    "respondent_id": "respondent_id",
    "respondentId": "respondent_id",
    "受访者号": "respondent_id",
    "text": "text",
    "transcription": "text",
    "转写文本": "text",
}


class TranscriptionRow(BaseModel):
    record_id: str = Field(min_length=1, max_length=128)
    respondent_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=100_000)

    @field_validator("record_id", "respondent_id")
    @classmethod
    def _no_surrounding_ws(cls, v: str) -> str:
        if v != v.strip():
            raise ValueError("must not have leading/trailing whitespace")
        return v

    @field_validator("text")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be blank")
        return v


class PackageOut(BaseModel):
    package_id: str
    status: Literal["uploading", "rewriting", "ready", "failed"]
    line_count: int
    updated_at: str


class WithdrawalOut(BaseModel):
    respondent_id: str
    reason: str | None = None
    created_at: str


class WithdrawalAck(BaseModel):
    respondent_id: str
    already_withdrawn: bool
    affected_packages: list[str]
    pending_jobs: int


class JobOut(BaseModel):
    id: int
    package_id: str
    status: Literal["pending", "running", "succeeded", "failed"]
    attempts: int
    last_error: str | None = None
    run_after: str
    locked_by: str | None = None
    locked_at: str | None = None
    created_at: str
    updated_at: str


class ErrorBody(BaseModel):
    detail: Any


def parse_jsonl_payload(raw: bytes | str) -> list[TranscriptionRow]:
    """Validate an upload body: <=100 lines, each valid JSON, unique record ids."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"payload is not valid UTF-8: {exc}") from exc
    else:
        text = raw

    if len(text.encode("utf-8")) > config.MAX_UPLOAD_BYTES:
        raise ValueError("upload body exceeds 10 MiB limit")

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("upload contains no data lines")
    if len(lines) > config.MAX_LINES_PER_UPLOAD:
        raise ValueError(
            f"too many lines: {len(lines)} (maximum {config.MAX_LINES_PER_UPLOAD})"
        )

    rows: list[TranscriptionRow] = []
    seen: set[str] = set()
    for idx, line in enumerate(lines, start=1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {idx}: invalid JSON ({exc.msg})") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"line {idx}: expected a JSON object")

        normalised: dict[str, str] = {}
        unknown: list[str] = []
        for key, value in obj.items():
            target = ALIASES.get(key)
            if target is None:
                unknown.append(key)
                continue
            # Reject contradictory aliases in the same row.
            if target in normalised and normalised[target] != value:
                raise ValueError(
                    f"line {idx}: conflicting aliases for field {target!r}"
                )
            normalised[target] = value
        if unknown:
            raise ValueError(f"line {idx}: unknown field(s): {', '.join(unknown)}")

        missing = {"record_id", "respondent_id", "text"} - normalised.keys()
        if missing:
            raise ValueError(f"line {idx}: missing field(s): {', '.join(sorted(missing))}")

        try:
            row = TranscriptionRow(**normalised)
        except Exception as exc:
            raise ValueError(f"line {idx}: {exc}") from exc

        if row.record_id in seen:
            raise ValueError(f"line {idx}: duplicate record_id {row.record_id!r} in package")
        seen.add(row.record_id)
        rows.append(row)

    return rows
