"""Private, app-owned staging for browser block uploads; never accepts a blob path from clients."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.config import Settings


class UploadError(ValueError):
    pass


@dataclass
class UploadSession:
    id: str
    filename: str
    size: int
    content_type: str
    expires: float
    lease_id: str = ""
    job_id: str = ""
    request_signature: str = ""
    canceled: bool = False
    cleaned: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def blob_name(self) -> str:
        return f"staging/{self.id}/video"


class BlobUploads:
    def __init__(self):
        self.sessions: dict[str, UploadSession] = {}

    def reserve(self, filename: str, size: int, content_type: str, settings: Settings) -> UploadSession:
        if not settings.blob_configured:
            raise UploadError("Large-file uploads are not configured.")
        if not 0 < size <= settings.max_blob_upload_bytes:
            raise UploadError(f"Video size must be between 1 and {settings.max_blob_upload_bytes} bytes.")
        now = time.time()
        self.sessions = {key: session for key, session in self.sessions.items() if session.expires > now or (session.job_id and not session.cleaned)}
        if sum(not session.job_id and not session.canceled for session in self.sessions.values()) >= 10:
            raise UploadError("Too many pending uploads. Finish or cancel an upload first.")
        session = UploadSession(uuid.uuid4().hex, filename, size, content_type, now + 7200)
        self.sessions[session.id] = session
        return session

    def get(self, upload_id: str) -> UploadSession:
        session = self.sessions.get(upload_id)
        if not session or session.canceled or (not session.job_id and session.expires <= time.time()):
            raise UploadError("Upload expired or was canceled. Select the file and try again.")
        return session

    @asynccontextmanager
    async def _client(self, settings: Settings):
        from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
        from azure.storage.blob.aio import BlobServiceClient

        credential = ManagedIdentityCredential() if settings.blob_use_managed_identity else AzureCliCredential(tenant_id=settings.blob_tenant_id or None)
        async with credential:
            async with BlobServiceClient(
                f"https://{settings.blob_account_name}.blob.core.windows.net", credential,
                retry_total=2, connection_timeout=15, read_timeout=60,
                logging_enable=False,
            ) as client:
                yield client

    async def _signed_url(self, client, session: UploadSession, settings: Settings, *, read: bool) -> str:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=5)
        expiry = now + timedelta(hours=3) if read else datetime.fromtimestamp(session.expires, timezone.utc)
        key = await client.get_user_delegation_key(start, expiry)
        token = generate_blob_sas(
            account_name=settings.blob_account_name, container_name=settings.blob_container,
            blob_name=session.blob_name, user_delegation_key=key,
            permission=BlobSasPermissions(read=True) if read else BlobSasPermissions(create=True, write=True),
            start=start, expiry=expiry, protocol="https",
        )
        blob = client.get_blob_client(settings.blob_container, session.blob_name)
        return f"{blob.url}?{token}"

    async def upload_url(self, session: UploadSession, settings: Settings) -> str:
        async with self._client(settings) as client:
            return await self._signed_url(client, session, settings, read=False)

    async def seal(self, session: UploadSession, settings: Settings) -> str:
        from azure.storage.blob.aio import BlobLeaseClient

        async with self._client(settings) as client:
            blob = client.get_blob_client(settings.blob_container, session.blob_name)
            if not session.lease_id:
                lease = BlobLeaseClient(blob)
                await lease.acquire(lease_duration=60)
                session.lease_id = lease.id
            else:
                await BlobLeaseClient(blob, lease_id=session.lease_id).renew()
            properties = await blob.get_blob_properties()
            if properties.size != session.size:
                raise UploadError("Uploaded size does not match the selected file. Cancel and upload again.")
            if properties.content_settings.content_type != session.content_type:
                raise UploadError("Uploaded content type does not match the selected video.")
            return await self._signed_url(client, session, settings, read=True)

    async def keepalive(self, session: UploadSession, settings: Settings) -> None:
        from azure.storage.blob.aio import BlobLeaseClient

        async with self._client(settings) as client:
            blob = client.get_blob_client(settings.blob_container, session.blob_name)
            lease = BlobLeaseClient(blob, lease_id=session.lease_id)
            while True:
                await asyncio.sleep(20)
                await lease.renew()

    async def cleanup(self, session: UploadSession, settings: Settings) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        async with session.lock:
            if session.cleaned:
                return
            async with self._client(settings) as client:
                blob = client.get_blob_client(settings.blob_container, session.blob_name)
                try:
                    await blob.delete_blob(lease=session.lease_id or None, delete_snapshots="include")
                except ResourceNotFoundError:
                    pass
            session.cleaned = True


uploads = BlobUploads()