from __future__ import annotations

from dataclasses import dataclass

from jarvis.workers import ActiveTaskSnapshot, QueueWorker


@dataclass(slots=True)
class WorkerSnapshot:
    name: str
    pending: int
    active: list[ActiveTaskSnapshot]


class TaskStatusProvider:
    def __init__(self, workers: list[QueueWorker]) -> None:
        self._workers = workers

    async def snapshot(self) -> list[WorkerSnapshot]:
        snapshots: list[WorkerSnapshot] = []
        for worker in self._workers:
            active = await worker.snapshot()
            snapshots.append(
                WorkerSnapshot(
                    name=worker.name,
                    pending=worker.pending_count(),
                    active=active,
                )
            )
        return snapshots
