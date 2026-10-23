"""Uploaded papers and the pictures attached to questions - the only module
that touches file storage, as store.py is the only one that touches the
database (store.py calls this; nothing else should).

Azure Blob Storage when EDSIDER_BLOB_CONNECTION_STRING is set - one private
container, EDSIDER_BLOB_CONTAINER (default "intake-files") - otherwise a
folder on this machine, EDSIDER_FILES_DIR (default data/files, gitignored),
which is what the tests and a laptop without the storage account use.

Why not the database: files were kept in PostgreSQL (bytea) so there was
one thing to back up. Measured on the live data, an uploaded paper averages
1.1 MB (largest 6.3 MB), so 100,000 papers would be about 110 GB on
EdSider's own database server - its most expensive storage, copied again by
every backup, and each download holding one of its 50 connections. The
database now keeps only where a file is, its size and its checksum.

Every environment that opens a paper must reach the same storage: dev,
pre-prod and a local run share one database, so a path recorded by one is
read by all of them.
"""

import hashlib
import mimetypes
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # python-dotenv is optional; the env vars still work
    pass

DEFAULT_CONTAINER = "intake-files"


class _Folder:
    """Files in a folder - for tests and a machine with no storage account."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def _path(self, name):
        path = (self.root / name).resolve()
        # A stored name never climbs out of the folder.
        if self.root not in path.parents:
            raise ValueError(f"not a stored file name: {name!r}")
        return path

    def put(self, name, data, content_type=None):
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, name):
        path = self._path(name)
        return path.read_bytes() if path.exists() else None

    def delete(self, name):
        path = self._path(name)
        if path.exists():
            path.unlink()

    def describe(self):
        return f"folder {self.root}"


class _Blob:
    """Files in one private Azure Blob Storage container."""

    def __init__(self, connection_string, container):
        from azure.storage.blob import BlobServiceClient

        self._service = BlobServiceClient.from_connection_string(connection_string)
        self._container = self._service.get_container_client(container)
        self._ready = False

    def _client(self):
        # Created on first use, private: files are only ever served through
        # the API, behind sign-in and the paper rule, never by a public URL.
        if not self._ready:
            if not self._container.exists():
                self._container.create_container()
            self._ready = True
        return self._container

    def put(self, name, data, content_type=None):
        from azure.storage.blob import ContentSettings

        self._client().upload_blob(
            name, data, overwrite=True,
            content_settings=ContentSettings(content_type=content_type) if content_type else None)

    def get(self, name):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self._client().download_blob(name).readall()
        except ResourceNotFoundError:
            return None

    def delete(self, name):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self._client().delete_blob(name)
        except ResourceNotFoundError:
            pass

    def describe(self):
        return f"Azure Blob Storage {self._service.account_name}/{self._container.container_name}"


_backend = None
_lock = threading.Lock()


def backend():
    """The storage in use, or None where files stay in the database - an
    App Service without the storage settings. Its own disk is replaced on
    every deploy, so a folder there would lose every file uploaded since
    the last one; until the settings are added, the database keeps them."""
    global _backend
    with _lock:
        if _backend is None:
            connection = os.environ.get("EDSIDER_BLOB_CONNECTION_STRING")
            folder = os.environ.get("EDSIDER_FILES_DIR")
            if connection:
                _backend = _Blob(connection,
                                 os.environ.get("EDSIDER_BLOB_CONTAINER") or DEFAULT_CONTAINER)
            elif folder or not os.environ.get("WEBSITE_SITE_NAME"):
                _backend = _Folder(folder or ROOT / "data" / "files")
        return _backend


def available():
    """Whether files go to file storage here (False: the database)."""
    return backend() is not None


def use_folder(path):
    """Keep files in `path` from now on - the tests do, so a test's files
    never reach the real storage account."""
    global _backend
    with _lock:
        _backend = _Folder(path)


def describe():
    """Where files go, for /health - never the key."""
    store = backend()
    return store.describe() if store else "database (no file storage set)"


def _required():
    store = backend()
    if store is None:
        raise RuntimeError("This file is in file storage, but no storage is set here - "
                           "add EDSIDER_BLOB_CONNECTION_STRING (see src/files.py).")
    return store


def put(name, data, content_type=None):
    _required().put(name, data, content_type)


def get(name):
    """The file's bytes, or None when nothing is stored under `name`."""
    return _required().get(name)


def delete(name):
    _required().delete(name)


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def paper_name(doc_id, filename, data):
    """papers/83/3f2a....pdf - by paper, then by content, so the same file
    uploaded again for a paper lands on the same name."""
    suffix = Path(filename or "").suffix.lower()[:10]
    return f"papers/{doc_id}/{checksum(data)[:32]}{suffix}"


def image_name(question_id, image_id, content_type=None):
    suffix = mimetypes.guess_extension(content_type or "") or ""
    if suffix == ".jpe":
        suffix = ".jpg"
    return f"images/{question_id}/{image_id}{suffix}"
