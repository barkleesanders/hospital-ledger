"""Validate aggregate parser concurrency before any ingest process starts."""


def workers_per_shard(total_workers: int, shards: int) -> int:
    if total_workers < 1 or shards < 1 or shards > total_workers:
        raise ValueError("worker budget and shards must be positive; shards cannot exceed budget")
    return total_workers // shards


def validate_workers(workers: int, budget: int) -> int:
    if not 1 <= workers <= budget:
        raise ValueError("workers must be positive and fit the aggregate worker budget")
    return workers
