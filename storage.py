"""
Storage helpers for Azure Table, Queue, and Blob operations.

Centralizes all Azure Storage SDK interactions:
- Table Storage (job status tracking)
- Queue Storage (job enqueueing)
- Blob Storage (result artifacts, work-in-progress state, SAS URLs)
"""

import datetime
import json
import logging

from azure.data.tables import TableServiceClient
from azure.storage.queue import QueueClient
from azure.storage.blob import (
    BlobClient,
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions,
)
from azure.core.exceptions import ResourceExistsError

from config import (
    STORAGE_CONN_STR,
    JOB_TABLE,
    JOB_PK,
    RESULT_CONTAINER,
    WORK_CONTAINER,
    QUEUE_NAME,
    SAS_HOURS_VALID,
)


# =============================================================================
# Low-level clients
# =============================================================================
def get_table_client():
    """Return TableClient for JobStatus table (creates if missing)."""
    svc = TableServiceClient.from_connection_string(STORAGE_CONN_STR)
    svc.create_table_if_not_exists(JOB_TABLE)
    return svc.get_table_client(JOB_TABLE)


def get_queue_client() -> QueueClient:
    """Return QueueClient for wpjobs (creates if missing)."""
    qc = QueueClient.from_connection_string(STORAGE_CONN_STR, QUEUE_NAME)
    try:
        qc.create_queue()
    except ResourceExistsError:
        pass
    return qc


def get_blob_service() -> BlobServiceClient:
    """BlobServiceClient (for container ops & SAS)."""
    return BlobServiceClient.from_connection_string(STORAGE_CONN_STR)


def get_blob_client(op_id: str) -> BlobClient:
    """BlobClient for results/{op_id}.json (container created if missing)."""
    bsc = get_blob_service()
    try:
        bsc.create_container(RESULT_CONTAINER)
    except ResourceExistsError:
        pass
    return bsc.get_blob_client(container=RESULT_CONTAINER, blob=f"{op_id}.json")


def get_work_blob_client(op_id: str) -> BlobClient:
    """BlobClient for work/{op_id}.json (container created if missing)."""
    bsc = get_blob_service()
    try:
        bsc.create_container(WORK_CONTAINER)
    except ResourceExistsError:
        pass
    return bsc.get_blob_client(container=WORK_CONTAINER, blob=f"{op_id}.json")


# =============================================================================
# Bootstrap
# =============================================================================
def ensure_storage_objects():
    """Pre-create table, queue, and blob containers."""
    get_queue_client()
    get_table_client()
    try:
        get_blob_service().create_container(RESULT_CONTAINER)
    except ResourceExistsError:
        pass


# =============================================================================
# SAS URL generation
# =============================================================================
def make_sas_url(op_id: str, hours_valid: int = SAS_HOURS_VALID) -> str | None:
    """Generate short-term read-only SAS URL for results/{op_id}.json."""
    try:
        bsc = get_blob_service()
        acct = bsc.account_name
        expires = datetime.datetime.utcnow() + datetime.timedelta(hours=hours_valid)
        sas = generate_blob_sas(
            account_name=acct,
            container_name=RESULT_CONTAINER,
            blob_name=f"{op_id}.json",
            account_key=getattr(bsc.credential, "account_key", None),
            permission=BlobSasPermissions(read=True),
            expiry=expires,
        )
        return f"https://{acct}.blob.core.windows.net/{RESULT_CONTAINER}/{op_id}.json?{sas}"
    except Exception:
        return None


# =============================================================================
# Job status (Table Storage)
# =============================================================================
def status_upsert(op_id: str, status: str, **extra):
    """Insert or update job status row."""
    tc = get_table_client()
    entity = {
        "PartitionKey": JOB_PK,
        "RowKey": op_id,
        "status": status,
        "updatedUtc": datetime.datetime.utcnow().isoformat() + "Z",
        **extra,
    }
    tc.upsert_entity(entity)


def status_get(op_id: str) -> dict | None:
    """Retrieve job status row or None."""
    try:
        return get_table_client().get_entity(JOB_PK, op_id)
    except Exception:
        return None


# =============================================================================
# Work-in-progress state (Blob Storage)
# =============================================================================
def state_load(op_id: str) -> dict | None:
    """Load intermediate job state from work container."""
    try:
        raw = get_work_blob_client(op_id).download_blob().readall()
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def state_save(op_id: str, state: dict) -> None:
    """Persist intermediate job state to work container."""
    get_work_blob_client(op_id).upload_blob(
        json.dumps(state, ensure_ascii=False).encode("utf-8"),
        overwrite=True,
    )


def progress(op_id: str, phase: str, done: int, total: int, **extra):
    """Update job status with progress percentage."""
    pct = int((done / max(1, total)) * 100)
    status_upsert(op_id, "working", phase=phase, progress=pct, **extra)
