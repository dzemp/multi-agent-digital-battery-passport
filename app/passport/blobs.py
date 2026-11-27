"""Blob storage for evidence files, passport snapshots and package intake.

Both implementations keep every version of a blob, like Azure Blob versioning. A version ID lets
the app prove later which exact bytes it recorded, even if someone overwrites the current blob.
"""

import re
from pathlib import Path

CONTAINERS = ("evidence", "passports", "intake")
SAFE_PATH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


class BlobExists(Exception):
    pass


def _check(container, path):
    if container not in CONTAINERS or not SAFE_PATH.match(path) or ".." in path:
        raise ValueError(f"unsafe blob location {container}/{path}")


class LocalBlobs:
    """Files on disk. root/<container>/<path> is current; root/.versions/... keeps every version."""

    kind = "local"

    def __init__(self, root):
        self.root = Path(root)

    def _current(self, container, path):
        return self.root / container / path

    def _versions(self, container, path):
        return self.root / ".versions" / container / path

    def put(self, container, path, data, content_type="application/octet-stream", overwrite=True):
        _check(container, path)
        target = self._current(container, path)
        if target.exists() and not overwrite:
            raise BlobExists(f"{container}/{path}")
        versions = self._versions(container, path)
        versions.mkdir(parents=True, exist_ok=True)
        version_id = str(len(list(versions.iterdir())) + 1)
        (versions / version_id).write_bytes(data)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return version_id

    def get(self, container, path, version_id=None):
        _check(container, path)
        target = self._versions(container, path) / version_id if version_id else self._current(container, path)
        return target.read_bytes() if target.is_file() else None

    def version_of(self, container, path):
        versions = self._versions(container, path)
        return str(len(list(versions.iterdir()))) if versions.is_dir() else None

    def list(self, container, prefix=""):
        base = self.root / container
        if not base.is_dir():
            return []
        names = (p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
        return sorted(n for n in names if n.startswith(prefix))

    def ping(self):
        return True


class AzureBlobs:
    """Azure Blob Storage with Microsoft Entra authentication (managed identity in Azure, az login on a PC)."""

    kind = "azure"

    def __init__(self, account_url, credential=None):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        credential = credential or DefaultAzureCredential(exclude_interactive_browser_credential=True)
        self.service = BlobServiceClient(account_url, credential=credential)

    def put(self, container, path, data, content_type="application/octet-stream", overwrite=True):
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        _check(container, path)
        client = self.service.get_blob_client(container, path)
        try:
            result = client.upload_blob(data, overwrite=overwrite,
                                        content_settings=ContentSettings(content_type=content_type))
        except ResourceExistsError as exc:
            raise BlobExists(f"{container}/{path}") from exc
        if not result.get("version_id"):
            raise RuntimeError("blob versioning is off; the app needs version IDs as proof")
        return result["version_id"]

    def get(self, container, path, version_id=None):
        from azure.core.exceptions import ResourceNotFoundError

        _check(container, path)
        try:
            return self.service.get_blob_client(container, path).download_blob(version_id=version_id).readall()
        except ResourceNotFoundError:
            return None

    def version_of(self, container, path):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self.service.get_blob_client(container, path).get_blob_properties().version_id
        except ResourceNotFoundError:
            return None

    def list(self, container, prefix=""):
        return sorted(self.service.get_container_client(container).list_blob_names(name_starts_with=prefix))

    def ping(self):
        self.service.get_container_client("passports").get_container_properties()
        return True
