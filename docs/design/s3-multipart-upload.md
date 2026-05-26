# S3 multipart upload (M5)

> **Status:** deferred future work. No code changes proposed in
> this document — captured here so the gap doesn't disappear into
> the review backlog. A focused PR will pick this up once a
> deployment commits to large-file workloads on S3.
>
> **Scope:** the second-pass review's M5 finding —
> `bp_router/storage/s3.py:94-134` `S3FileStore.put` buffers the
> entire upload into a `list[bytes]` in memory before issuing a
> single `PutObject` call. The TODO comment on line 103
> ("Multipart upload (for large files) is a TODO.") is the
> explicit acknowledgement.
>
> **Outcome we want:** large uploads (> ~16 MiB) stream directly
> to S3 via the multipart API, with each part flushed as it's
> received. Memory usage is bounded by `part_size` regardless of
> upload size.

---

## 1. The gap today

`S3FileStore.put` (`bp_router/storage/s3.py:94-134`):

```python
async def put(self, sha256, src, meta):
    h = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    async for chunk in src:
        h.update(chunk)
        size += len(chunk)
        chunks.append(chunk)        # <-- accumulates the WHOLE upload
    ...
    body = b"".join(chunks)         # <-- and concatenates
    async with self._client() as s3:
        await s3.put_object(Bucket=..., Key=..., Body=body, ...)
```

Two memory shapes both bad:

  - `chunks` holds every chunk as a separate `bytes`. Python
    overhead per object is ~33 bytes; for 1 MiB chunks that's
    fine, for 64 KiB chunks it's significant churn.
  - `b"".join(chunks)` then duplicates the whole upload into a
    single contiguous buffer. Peak memory is **2×** the upload
    size right before `put_object` consumes it.

`upload_utils.hash_with_size_cap` (the upload-time guard) caps
incoming bytes at `settings.max_upload_bytes` (default 100 MiB).
So today's worst case is ~200 MiB transient memory per
concurrent S3 upload. With FastAPI workers running concurrent
admits, this is a real OOM vector.

The local-disk backend (`LocalFileStore`) doesn't have this
problem — it streams directly to a temp file and renames on
success. S3 is the only backend with the buffering problem.

## 2. Why multipart is the right shape

S3's `UploadPart` API:

  - Each part is uploaded independently with its own ETag.
  - Parts are 5 MiB minimum (except the last) and 5 GiB maximum.
  - `CompleteMultipartUpload` assembles them server-side; we
    never need the whole object in our memory.
  - On error, `AbortMultipartUpload` cleans up the partials.
  - aioboto3 supports the full multipart API natively; no
    third-party glue needed.

Memory shape becomes: one `part_size` buffer at a time
(default ~16 MiB, tunable). Throughput is also better because
parts can upload in parallel.

## 3. Algorithm

```
async def put(sha256, src, meta):
    h = sha256_hasher()
    size = 0
    part_buf = BytesIO()
    parts = []
    upload_id = None
    key = self._key(sha256)

    try:
        async for chunk in src:
            h.update(chunk)
            size += len(chunk)
            part_buf.write(chunk)
            if part_buf.tell() >= part_size:
                if upload_id is None:
                    upload_id = await s3.create_multipart_upload(...)
                parts.append(await _upload_part(part_buf, len(parts)+1))
                part_buf = BytesIO()

        verify_sha256_and_size(h, size, sha256, meta.byte_size)

        if upload_id is None:
            # Small upload — single PutObject, no multipart overhead.
            await s3.put_object(Body=part_buf.getvalue(), ...)
        else:
            # Final part (anything left in the buffer).
            if part_buf.tell() > 0:
                parts.append(await _upload_part(part_buf, len(parts)+1))
            await s3.complete_multipart_upload(
                Bucket=..., Key=..., UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
    except Exception:
        if upload_id is not None:
            await s3.abort_multipart_upload(
                Bucket=..., Key=..., UploadId=upload_id,
            )
        raise

    return f"s3://{self.bucket}/{key}"
```

Key decisions:

  - **Single-PutObject path for small uploads.** `create_multipart_upload`
    + `complete_multipart_upload` is two extra round-trips.
    Below `part_size`, single `PutObject` is faster and cheaper
    (S3 charges per-request).
  - **Sha256 verification BEFORE complete.** If the incoming
    bytes don't match the claimed sha256, abort the multipart
    upload before assembly. Otherwise we'd briefly have a
    completed object under the wrong key.
  - **Abort on ANY exception.** Network blip mid-upload, hash
    mismatch, size mismatch — all need cleanup. S3 charges for
    incomplete multipart uploads (parts sit around) until aborted
    or expired by lifecycle policy.

## 4. Settings shape

```python
class Settings(BaseSettings):
    # NEW.
    s3_multipart_threshold_bytes: int = 16 * 1024 * 1024
    """Uploads above this size use multipart; below use single
    PutObject. Default 16 MiB matches aioboto3's `S3Transfer`
    default and balances memory vs request count."""

    s3_multipart_part_size_bytes: int = 16 * 1024 * 1024
    """Each multipart part flushed at this size. S3 minimum is
    5 MiB (except the last part). Default 16 MiB ≈ 2-second
    flush at 64 Mbps inbound."""

    s3_multipart_concurrency: int = 4
    """Max concurrent UploadPart calls per upload. Trade-off:
    higher = better throughput on fat pipes, more memory
    (concurrency × part_size). 4 × 16 MiB = 64 MiB peak per
    upload, well under the current ~200 MiB worst case."""
```

All three fields defaulting to safe values means no operator
opt-in is required for the multipart path to start working.
Tuning is per-deployment.

## 5. Implementation plan

Two PRs:

  1. **Refactor `S3FileStore.put` to multipart.** Extract the
     existing single-PutObject path as `_put_single`, add
     `_put_multipart`, dispatch on accumulated size. Keep the
     sha256 + size verification in the shared prologue. Add
     unit tests against `moto` (mock S3) covering: small upload
     (single PutObject), large upload (multipart), abort on
     hash mismatch, abort on size mismatch, abort on mid-stream
     exception.

  2. **Add `S3Transfer`-style concurrency.** Use `asyncio.Semaphore`
     to bound `s3_multipart_concurrency`. Parts upload in
     parallel up to the cap; the main coroutine continues
     reading from `src` while earlier parts upload. Tests pin
     the concurrency cap (e.g. monkeypatch `_upload_part` to
     count concurrent invocations).

PR #1 is the correctness win; PR #2 is the throughput win and
can be deferred independently if needed.

## 6. Operational rollout

  - **No migration of existing objects.** Multipart vs
    single-PutObject is purely a write-path concern; reads are
    identical. Existing objects stay readable unchanged.
  - **Lifecycle policy for incomplete uploads.** Operators
    SHOULD configure an S3 lifecycle rule to abort incomplete
    multipart uploads after N days, so a router crash mid-upload
    doesn't accumulate paid-for partial parts. The doc PR should
    include the recommended lifecycle JSON.
  - **Metrics.** Add `router_s3_upload_bytes_total{outcome}` and
    `router_s3_upload_parts_total{outcome}` counters so
    operators can dashboard the multipart hit rate and abort
    rate.

## 7. Why this isn't being done now

Three reasons:

  1. **No deployment is hitting it.** Current operators use the
     local-disk backend; S3 is supported but not used in
     production. The OOM vector is real but theoretical until
     someone runs S3 + concurrent admits + large uploads.

  2. **`max_upload_bytes` already caps the damage.** The default
     100 MiB cap means worst-case transient memory is ~200 MiB
     per concurrent S3 upload. Painful but not OOM-killing on a
     reasonably-sized FastAPI worker (typical 1-2 GiB RSS
     budget). Operators with large-file workloads have to raise
     `max_upload_bytes` first; multipart becomes a prerequisite
     for that change.

  3. **Test infrastructure cost.** `moto` (mock S3) covers the
     happy path; testing the abort-on-failure path requires
     either a fault-injection layer over `moto` or real S3 +
     network shaping. The §5 PR #1 test harness needs a
     deliberate decision on which approach. Deferring until
     someone owns that test harness work.

## 8. Tracking

This doc is the durable home for the M5 finding. When a
deployment needs large-file S3 support, start here and walk the
implementation plan in §5. Until then, the existing
`max_upload_bytes` cap is the compensating control — keep it at
or below 100 MiB while the buffering put is in place.
