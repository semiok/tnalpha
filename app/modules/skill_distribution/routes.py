"""Versioned, deterministic distribution endpoints for TN-Alpha Skills."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response


router = APIRouter(prefix="/skills", tags=["skill-distribution"])

ROOT_DIR = Path(__file__).resolve().parents[3]
SKILLS_DIR = ROOT_DIR / "skills"
EXCLUDED_PARTS = {"__pycache__", ".DS_Store", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail=f"Invalid Skill metadata: {error}") from error


def _skill_dir(skill_id: str) -> Path:
    if not skill_id or "/" in skill_id or "\\" in skill_id or skill_id in {".", ".."}:
        raise HTTPException(status_code=404, detail="Skill not found")
    path = SKILLS_DIR / skill_id
    if not path.is_dir() or not (path / "skill.json").is_file():
        raise HTTPException(status_code=404, detail="Skill not found")
    return path


def _package_bytes(skill_id: str) -> bytes:
    skill_dir = _skill_dir(skill_id)
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(skill_dir)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            archive_name = f"{skill_id}/{relative.as_posix()}"
            info = ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
            mode = 0o755 if relative.parts[0] == "scripts" else 0o644
            info.external_attr = mode << 16
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()


@router.get("/registry.json")
def registry(request: Request):
    data = _read_json(SKILLS_DIR / "registry.json")
    for skill in data.get("skills", []):
        skill_id = skill.get("id", "")
        skill["manifestUrl"] = str(request.url_for("skill_manifest", skill_id=skill_id))
    return data


@router.get("/{skill_id}/manifest.json", name="skill_manifest")
def skill_manifest(skill_id: str, request: Request):
    skill_dir = _skill_dir(skill_id)
    manifest = _read_json(skill_dir / "skill.json")
    package = _package_bytes(skill_id)
    version = manifest["version"]
    manifest["manifestUrl"] = str(request.url_for("skill_manifest", skill_id=skill_id))
    manifest["package"] = {
        "url": str(request.url_for("skill_package", skill_id=skill_id, version=version)),
        "sha256": sha256(package).hexdigest(),
        "sizeBytes": len(package),
        "format": "zip",
    }
    return manifest


@router.get("/{skill_id}/{version}.zip", name="skill_package")
def skill_package(skill_id: str, version: str):
    skill_dir = _skill_dir(skill_id)
    manifest = _read_json(skill_dir / "skill.json")
    if version != manifest.get("version"):
        raise HTTPException(status_code=404, detail="Skill version not found")
    package = _package_bytes(skill_id)
    return Response(
        content=package,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{skill_id}-{version}.zip"',
            "ETag": f'"{sha256(package).hexdigest()}"',
            "Cache-Control": "public, max-age=300",
        },
    )
