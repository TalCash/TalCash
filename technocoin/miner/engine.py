"""Multi-core mining.

One worker process per core. Worker i tries nonces i, i + n, i + 2n, ... so no
two workers repeat work. Workers live as long as the Miner does; to mine a new
header the Miner bumps a shared job number and hands every worker the new job,
and workers check that number before every hash, so stale work stops within
one hash (a few milliseconds).

Nearly all the time goes into Argon2id inside libsodium, so this Python miner
runs about as fast as a native one would.
"""

import multiprocessing
import os
import queue
import time
from dataclasses import dataclass

from ..core.block import BlockHeader
from ..core.params import PowParams
from ..core.pow import pow_hash_bytes

_NONCE_SIZE = 8


@dataclass(frozen=True)
class Job:
    job_id: int
    header_prefix: bytes  # the serialized header without its nonce
    target: int
    pow: PowParams
    first_nonce: int
    stride: int


def _latest_job(jobs) -> Job | None:
    """Wait for a job, then skip ahead to the newest one queued."""
    job = jobs.get()
    while True:
        try:
            job = jobs.get_nowait()
        except queue.Empty:
            return job


def _worker(index: int, jobs, results, current_job, hash_counts) -> None:
    try:
        _work(index, jobs, results, current_job, hash_counts)
    except KeyboardInterrupt:
        pass  # Ctrl+C reaches every process in the console; the parent shuts everything down


def _work(index: int, jobs, results, current_job, hash_counts) -> None:
    job = _latest_job(jobs)
    while job is not None:
        nonce = job.first_nonce
        while True:
            if current_job.value != job.job_id:
                job = _latest_job(jobs)
                break
            digest = pow_hash_bytes(job.header_prefix + nonce.to_bytes(_NONCE_SIZE, "big"), job.pow)
            hash_counts[index] += 1
            if int.from_bytes(digest, "big") <= job.target:
                results.put((job.job_id, nonce))
                job = _latest_job(jobs)
                break
            nonce += job.stride
            if nonce >= 1 << 64:  # this job is exhausted; wait for the next one
                job = _latest_job(jobs)
                break


class Miner:
    def __init__(self, workers: int | None = None) -> None:
        self.workers = workers or os.cpu_count() or 1
        context = multiprocessing.get_context("spawn")  # same behaviour on Windows, macOS and Linux
        self._current_job = context.RawValue("q", 0)
        self._hash_counts = context.RawArray("q", self.workers)
        self._jobs = [context.Queue() for _ in range(self.workers)]
        self._results = context.Queue()
        self._job_id = 0
        self._processes = [
            context.Process(
                target=_worker,
                args=(i, self._jobs[i], self._results, self._current_job, self._hash_counts),
                daemon=True,
                name=f"technocoin-miner-{i}",
            )
            for i in range(self.workers)
        ]
        for process in self._processes:
            process.start()

    def mine(self, header: BlockHeader, pow_params: PowParams) -> int:
        """Start (or switch to) mining this header. Returns the job id."""
        self._job_id += 1
        self._current_job.value = self._job_id
        prefix = header.serialize()[:-_NONCE_SIZE]
        for i, jobs in enumerate(self._jobs):
            jobs.put(Job(self._job_id, prefix, header.target, pow_params, i, self.workers))
        return self._job_id

    def wait(self, timeout: float) -> int | None:
        """Nonce that solves the current job, or None if none was found within `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                job_id, nonce = self._results.get(timeout=remaining)
            except queue.Empty:
                return None
            if job_id == self._job_id:
                return nonce
            # A solution for an older job: ignore it.

    def pause(self) -> None:
        """Stop hashing until the next mine() call."""
        self._job_id += 1
        self._current_job.value = self._job_id

    def total_hashes(self) -> int:
        return sum(self._hash_counts)

    def close(self) -> None:
        self.pause()
        for jobs in self._jobs:
            jobs.put(None)
        for process in self._processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()

    def __enter__(self) -> "Miner":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
