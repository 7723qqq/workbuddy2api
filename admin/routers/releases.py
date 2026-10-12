"""Read-only release information for the browser UI; installing is native-only."""
from functools import lru_cache
import tempfile
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from admin.security import require_admin
from desktop.common import ROOT, VERSION, RELEASES_URL
from desktop.updates import UpdateService

router = APIRouter(prefix="/api/app", tags=["releases"])


@router.get("/version")
def version():
    return {"version": VERSION, "releases_url": RELEASES_URL}


@router.get("/icon")
def icon():
    return FileResponse(ROOT / "desktop/assets/app.svg", media_type="image/svg+xml")


@router.get("/desktop-ready")
def desktop_ready(x_desktop_boot: str = Header(default="")):
    expected = os.getenv("WB_DESKTOP_BOOT", "")
    if not expected or not secrets.compare_digest(expected, x_desktop_boot):
        raise HTTPException(status_code=404)
    return {"ready": True}


@lru_cache(maxsize=1)
def service():
    # The server version never downloads or installs executables.
    base = Path(tempfile.gettempdir()) / "workbuddy2api-releases"
    base.mkdir(exist_ok=True)
    return UpdateService(base)


@router.get("/releases", dependencies=[Depends(require_admin)])
def releases():
    return service().check()
