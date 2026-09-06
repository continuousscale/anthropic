"""Storage adapters.

The pipeline talks to this interface, not to Google. That keeps the whole
classify/name/route path testable against a local folder, and it means the
destructive operations - rename and move - exist in exactly two small methods
that can be read in full before anyone points this at a real Drive.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
import logging
import shutil
from pathlib import Path
from typing import Iterator, Protocol

from .models import FileRef

log = logging.getLogger(__name__)

# Google's own formats have no bytes to download; they are exported instead.
GOOGLE_EXPORT_MIMES = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    "application/vnd.google-apps.presentation": "application/pdf",
}

# Never touched: folders, shortcuts, and Google's non-document types.
SKIP_MIMES = {
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
}


class Storage(Protocol):
    """What the pipeline needs from a file store.

    `rename` and `move` return the file's id *after* the operation. Drive ids
    are stable across both, so it returns the same id; a local path changes,
    so it returns the new path. Callers must use the returned id for any
    subsequent operation on that file.

    `rename_and_move` does both in one operation. It exists because doing them
    in sequence can fail on a name that is only transiently taken: renaming a
    file in its source folder collides with an already-correctly-named file
    sitting there, even though the destination folder is free.
    """

    def list_files(self, folder: str, recursive: bool = True) -> Iterator[FileRef]: ...
    def download(self, ref: FileRef) -> tuple[bytes, str]: ...
    def ensure_folder(self, path: str) -> str: ...
    def peek_folder(self, path: str) -> str | None: ...
    def names_in(self, folder_id: str) -> set[str]: ...
    def rename(self, file_id: str, new_name: str) -> str: ...
    def move(self, file_id: str, new_parent_id: str, old_parent_id: str | None) -> str: ...
    def rename_and_move(
        self, file_id: str, new_name: str, new_parent_id: str, old_parent_id: str | None
    ) -> str: ...


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Local filesystem
# ---------------------------------------------------------------------------


class LocalStorage:
    """A local directory tree, used for testing and for rehearsing a run.

    `root` is the equivalent of the BizBox root folder: destination paths from
    the routing rules resolve underneath it.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _rel(self, path: Path) -> str:
        return str(path.parent.relative_to(self.root)) if path.parent != self.root else ""

    def list_files(self, folder: str, recursive: bool = True) -> Iterator[FileRef]:
        base = (self.root / folder).resolve() if folder else self.root
        if not base.exists():
            raise FileNotFoundError(f"No such folder: {base}")
        paths = base.rglob("*") if recursive else base.glob("*")
        for path in sorted(paths):
            if not path.is_file() or path.name.startswith("."):
                continue
            stat = path.stat()
            yield FileRef(
                id=str(path),
                name=path.name,
                mime_type=_guess_mime(path),
                size=stat.st_size,
                parent_id=str(path.parent),
                parent_path=self._rel(path),
                modified_time=_dt.datetime.fromtimestamp(stat.st_mtime, _dt.timezone.utc),
            )

    def download(self, ref: FileRef) -> tuple[bytes, str]:
        data = Path(ref.id).read_bytes()
        return data, sha256(data)

    def ensure_folder(self, path: str) -> str:
        target = self.root / path
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def peek_folder(self, path: str) -> str | None:
        target = self.root / path
        return str(target) if target.is_dir() else None

    def names_in(self, folder_id: str) -> set[str]:
        folder = Path(folder_id)
        if not folder.exists():
            return set()
        return {p.name for p in folder.iterdir() if p.is_file()}

    def rename(self, file_id: str, new_name: str) -> str:
        src = Path(file_id)
        dst = src.parent / new_name
        if dst.exists():
            raise FileExistsError(f"Refusing to overwrite {dst}")
        src.rename(dst)
        return str(dst)

    def move(self, file_id: str, new_parent_id: str, old_parent_id: str | None) -> str:
        src = Path(file_id)
        dst = Path(new_parent_id) / src.name
        if dst.exists():
            raise FileExistsError(f"Refusing to overwrite {dst}")
        Path(new_parent_id).mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return str(dst)

    def rename_and_move(
        self, file_id: str, new_name: str, new_parent_id: str, old_parent_id: str | None
    ) -> str:
        src = Path(file_id)
        dst = Path(new_parent_id) / new_name
        if dst.exists():
            raise FileExistsError(f"Refusing to overwrite {dst}")
        Path(new_parent_id).mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return str(dst)


def _guess_mime(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/drive"]


class GoogleDriveStorage:
    """Google Drive, including shared drives.

    Authentication uses an installed-app OAuth flow by default; a service
    account is supported for unattended runs. The token is cached so a
    scheduled run does not prompt.
    """

    def __init__(
        self,
        root_folder_name: str,
        credentials_path: str | Path = "credentials.json",
        token_path: str | Path = "token.json",
        service_account_path: str | Path | None = None,
    ) -> None:
        self.service = self._build_service(
            Path(credentials_path), Path(token_path), service_account_path
        )
        self.root_folder_name = root_folder_name
        self._root_id: str | None = None
        self._folder_cache: dict[str, str] = {}
        self._path_cache: dict[str, str] = {}

    # -- auth ------------------------------------------------------------

    @staticmethod
    def _build_service(
        credentials_path: Path, token_path: Path, service_account_path: str | Path | None
    ):
        from googleapiclient.discovery import build

        if service_account_path:
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                str(service_account_path), scopes=SCOPES
            )
            return build("drive", "v3", credentials=creds, cache_discovery=False)

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not credentials_path.exists():
                    raise FileNotFoundError(
                        f"Google OAuth client secrets not found at {credentials_path}. "
                        f"Create an OAuth client ID (Desktop app) in Google Cloud "
                        f"Console, enable the Drive API, and save it there."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_path), SCOPES
                )
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json(), encoding="utf-8")
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    # -- helpers ---------------------------------------------------------

    def _q(self, **kwargs):
        """Drive list calls, with shared-drive support always on."""
        kwargs.setdefault("supportsAllDrives", True)
        kwargs.setdefault("includeItemsFromAllDrives", True)
        return self.service.files().list(**kwargs)

    def root_id(self) -> str:
        if self._root_id:
            return self._root_id
        escaped = self.root_folder_name.replace("'", "\\'")
        resp = self._q(
            q=(
                f"name='{escaped}' and "
                f"mimeType='application/vnd.google-apps.folder' and trashed=false"
            ),
            fields="files(id,name)",
            pageSize=10,
        ).execute()
        files = resp.get("files", [])
        if not files:
            raise FileNotFoundError(
                f"Could not find a folder named {self.root_folder_name!r} in Drive."
            )
        if len(files) > 1:
            log.warning(
                "Multiple folders named %r; using the first.", self.root_folder_name
            )
        self._root_id = files[0]["id"]
        return self._root_id

    def resolve_folder_id(self, path: str) -> str | None:
        """Find an existing folder by path relative to the root, without creating it."""
        if path in self._folder_cache:
            return self._folder_cache[path]
        parent = self.root_id()
        for segment in [s for s in path.split("/") if s]:
            escaped = segment.replace("'", "\\'")
            resp = self._q(
                q=(
                    f"name='{escaped}' and '{parent}' in parents and "
                    f"mimeType='application/vnd.google-apps.folder' and trashed=false"
                ),
                fields="files(id,name)",
                pageSize=10,
            ).execute()
            files = resp.get("files", [])
            if not files:
                return None
            parent = files[0]["id"]
        self._folder_cache[path] = parent
        return parent

    def ensure_folder(self, path: str) -> str:
        """Resolve a path, creating any missing folders along the way."""
        if path in self._folder_cache:
            return self._folder_cache[path]
        parent = self.root_id()
        for segment in [s for s in path.split("/") if s]:
            escaped = segment.replace("'", "\\'")
            resp = self._q(
                q=(
                    f"name='{escaped}' and '{parent}' in parents and "
                    f"mimeType='application/vnd.google-apps.folder' and trashed=false"
                ),
                fields="files(id,name)",
                pageSize=10,
            ).execute()
            files = resp.get("files", [])
            if files:
                parent = files[0]["id"]
            else:
                created = (
                    self.service.files()
                    .create(
                        body={
                            "name": segment,
                            "mimeType": "application/vnd.google-apps.folder",
                            "parents": [parent],
                        },
                        fields="id",
                        supportsAllDrives=True,
                    )
                    .execute()
                )
                parent = created["id"]
        self._folder_cache[path] = parent
        return parent

    def _folder_path(self, folder_id: str) -> str:
        """Walk parents up to the root to build a readable path."""
        if folder_id in self._path_cache:
            return self._path_cache[folder_id]
        segments: list[str] = []
        current = folder_id
        root = self.root_id()
        for _ in range(20):  # guard against a cycle or a folder outside the root
            if current == root:
                break
            meta = (
                self.service.files()
                .get(fileId=current, fields="id,name,parents", supportsAllDrives=True)
                .execute()
            )
            segments.append(meta["name"])
            parents = meta.get("parents")
            if not parents:
                break
            current = parents[0]
        path = "/".join(reversed(segments))
        self._path_cache[folder_id] = path
        return path

    # -- Storage protocol -------------------------------------------------

    def list_files(self, folder: str, recursive: bool = True) -> Iterator[FileRef]:
        start = self.resolve_folder_id(folder) if folder else self.root_id()
        if not start:
            raise FileNotFoundError(f"No such Drive folder: {folder!r}")

        queue = [start]
        while queue:
            current = queue.pop(0)
            page_token = None
            while True:
                resp = self._q(
                    q=f"'{current}' in parents and trashed=false",
                    fields=(
                        "nextPageToken, files(id,name,mimeType,size,parents,"
                        "modifiedTime,md5Checksum,webViewLink)"
                    ),
                    pageSize=200,
                    pageToken=page_token,
                ).execute()

                for item in resp.get("files", []):
                    mime = item["mimeType"]
                    if mime == "application/vnd.google-apps.folder":
                        if recursive:
                            queue.append(item["id"])
                        continue
                    if mime in SKIP_MIMES:
                        continue
                    yield FileRef(
                        id=item["id"],
                        name=item["name"],
                        mime_type=mime,
                        size=int(item.get("size", 0) or 0),
                        parent_id=current,
                        parent_path=self._folder_path(current),
                        modified_time=_parse_time(item.get("modifiedTime")),
                        content_hash=item.get("md5Checksum"),
                        web_link=item.get("webViewLink"),
                    )

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

    def download(self, ref: FileRef) -> tuple[bytes, str]:
        from googleapiclient.http import MediaIoBaseDownload

        if ref.mime_type in GOOGLE_EXPORT_MIMES:
            request = self.service.files().export_media(
                fileId=ref.id, mimeType=GOOGLE_EXPORT_MIMES[ref.mime_type]
            )
        else:
            request = self.service.files().get_media(
                fileId=ref.id, supportsAllDrives=True
            )

        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        data = buf.getvalue()
        return data, ref.content_hash or sha256(data)

    def peek_folder(self, path: str) -> str | None:
        """Resolve without creating, so a dry run never writes to Drive."""
        return self.resolve_folder_id(path)

    def names_in(self, folder_id: str) -> set[str]:
        names: set[str] = set()
        page_token = None
        while True:
            resp = self._q(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken, files(name)",
                pageSize=200,
                pageToken=page_token,
            ).execute()
            names.update(f["name"] for f in resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return names

    def rename(self, file_id: str, new_name: str) -> str:
        self.service.files().update(
            fileId=file_id, body={"name": new_name}, supportsAllDrives=True
        ).execute()
        return file_id  # Drive ids are stable across a rename

    def move(self, file_id: str, new_parent_id: str, old_parent_id: str | None) -> str:
        self.service.files().update(
            fileId=file_id,
            addParents=new_parent_id,
            removeParents=old_parent_id or "",
            fields="id,parents",
            supportsAllDrives=True,
        ).execute()
        return file_id  # and across a move

    def rename_and_move(
        self, file_id: str, new_name: str, new_parent_id: str, old_parent_id: str | None
    ) -> str:
        """One Drive call that both renames and reparents.

        Drive applies the whole update atomically, so the file is never briefly
        sitting in its old folder under its new name.
        """
        self.service.files().update(
            fileId=file_id,
            body={"name": new_name},
            addParents=new_parent_id,
            removeParents=old_parent_id or "",
            fields="id,name,parents",
            supportsAllDrives=True,
        ).execute()
        return file_id


def _parse_time(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
