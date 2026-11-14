"""Pure selected manifests; selection metadata is not apply authorization."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field

from devdiary.note_profile import BoundProfile
from devdiary.note_validation import (
    SETUP_LIMIT,
    NoteError,
    decode_object,
    encode_object,
    freeze_write,
    validate_intent,
)


def _digest(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _identifier(profile: BoundProfile, namespace: str, entry_id: str, kind: str) -> str:
    seed = json.dumps(
        [
            "devdiary-note-import-v1",
            profile.subject_ref,
            profile.workspace_ref,
            namespace,
            entry_id,
            kind,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    prefix = "note-import:" if kind == "note" else "note-op:"
    return prefix + str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


@dataclass(frozen=True)
class _SelectedEntry:
    entry_id: str
    note_ref: str
    create_operation_id: str
    entry_sha256: str
    recorded_at_sha256: str
    _intent: bytes = field(repr=False)

    def create_intent(self) -> dict:
        return decode_object(self._intent, SETUP_LIMIT, "invalid_import")

    def metadata(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "note_ref": self.note_ref,
            "create_operation_id": self.create_operation_id,
            "entry_sha256": self.entry_sha256,
            "recorded_at_sha256": self.recorded_at_sha256,
        }


@dataclass(frozen=True)
class SelectedManifest:
    source_namespace: str
    entries: tuple[_SelectedEntry, ...]
    selection_sha256: str

    def metadata(self) -> dict:
        return {
            "schema_version": 1,
            "source_namespace": self.source_namespace,
            "count": len(self.entries),
            "selection_sha256": self.selection_sha256,
            "entries": [entry.metadata() for entry in self.entries],
        }


def parse_manifest(raw: bytes, bound_profile: BoundProfile) -> SelectedManifest:
    value = decode_object(raw, SETUP_LIMIT, "invalid_import")
    if (
        set(value) != {"schema_version", "source_namespace", "entries"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise NoteError("invalid_import")
    namespace = value["source_namespace"]
    if (
        not isinstance(namespace, str)
        or re.fullmatch(
            r"urn:devdiary:journal:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}",
            namespace,
            re.ASCII,
        )
        is None
    ):
        raise NoteError("invalid_import")
    selected = value["entries"]
    if not isinstance(selected, list) or not 1 <= len(selected) <= 100:
        raise NoteError("invalid_import")
    entries = []
    normalized_entries = []
    seen: set[str] = set()
    for entry in selected:
        if (
            not isinstance(entry, dict)
            or not {"entry_id", "content"} <= entry.keys()
            or entry.keys()
            - {"entry_id", "content", "next_step", "recorded_at", "references"}
        ):
            raise NoteError("invalid_import")
        entry_id = entry["entry_id"]
        if (
            not isinstance(entry_id, str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", entry_id, re.ASCII) is None
            or entry_id in seen
        ):
            raise NoteError("invalid_import")
        seen.add(entry_id)
        if any(
            field in entry and not isinstance(entry[field], str)
            for field in ("content", "next_step", "recorded_at")
        ):
            raise NoteError("invalid_import")
        references = entry.get("references", [])
        if not isinstance(references, list):
            raise NoteError("invalid_import")
        attributes = {
            "content": entry["content"],
            "next_step": entry.get("next_step"),
            "source": "journal-selected-v1",
        }
        if "recorded_at" in entry:
            attributes["recorded_at"] = entry["recorded_at"]
        intent = {"attributes": attributes, "references": references}
        try:
            validate_intent("create", intent)
        except NoteError:
            raise NoteError("invalid_import") from None
        intent["references"] = [
            {**ref, "relation": ref.get("relation", "context")} for ref in references
        ]
        note_ref = _identifier(bound_profile, namespace, entry_id, "note")
        operation_id = _identifier(bound_profile, namespace, entry_id, "create")
        try:
            freeze_write(
                {
                    "action": "create",
                    "note_ref": note_ref,
                    "operation_id": operation_id,
                    **intent,
                }
            )
        except NoteError:
            raise NoteError("invalid_import") from None
        entries.append(
            _SelectedEntry(
                entry_id,
                note_ref,
                operation_id,
                _digest(["devdiary-note-entry-v1", intent]),
                _digest(
                    ["devdiary-note-recorded-at-v1", attributes.get("recorded_at")]
                ),
                encode_object(intent),
            )
        )
        normalized_entries.append({"entry_id": entry_id, **intent})
    normalized = {
        "schema_version": 1,
        "source_namespace": namespace,
        "entries": normalized_entries,
    }
    return SelectedManifest(
        namespace,
        tuple(entries),
        _digest(["devdiary-note-selection-v1", asdict(bound_profile), normalized]),
    )
