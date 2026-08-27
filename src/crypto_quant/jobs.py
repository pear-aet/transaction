from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .storage import Repository

logger = logging.getLogger(__name__)


class JobManager:
    def __init__(self, repository: Repository, workers: int = 2) -> None:
        self.repository = repository
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crypto-quant")

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    def submit(
        self,
        kind: str,
        request: dict[str, Any],
        task: Callable[[Callable[[], bool]], dict[str, Any]],
    ) -> str:
        identifier = self.repository.create_job(kind, request, self._now())

        def run() -> None:
            if self.repository.job_cancelled(identifier):
                self.repository.update_job(identifier, "cancelled", self._now())
                return
            self.repository.update_job(identifier, "running", self._now())
            logger.info("job started", extra={"job_id": identifier, "event_kind": kind})
            try:
                result = task(lambda: self.repository.job_cancelled(identifier))
                status = "cancelled" if self.repository.job_cancelled(identifier) else "completed"
                self.repository.update_job(identifier, status, self._now(), result=result)
                logger.info(
                    "job finished",
                    extra={"job_id": identifier, "event_kind": status},
                )
            except Exception as exc:
                self.repository.update_job(identifier, "failed", self._now(), error=str(exc))
                logger.exception(
                    "job failed",
                    extra={"job_id": identifier, "event_kind": kind},
                )

        self.pool.submit(run)
        return identifier
