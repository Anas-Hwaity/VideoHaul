from __future__ import annotations


def allocate_bandwidth(jobs, global_limit_bps: int | None, mode: str = "equal", selected_job_id: str | None = None) -> dict[str, int | None]:
    values = list(jobs)
    if not values:
        return {}
    global_limit = positive_limit(global_limit_bps)
    if global_limit is None:
        return {job.job_id: positive_limit(job.settings.speed_limit_bps) for job in values}
    normalized = str(mode or "equal").lower()
    if normalized == "finish_one_fastest":
        weights = finish_fastest_weights(values)
    elif normalized == "prioritize_selected":
        weights = selected_weights(values, selected_job_id)
    else:
        weights = {job.job_id: 1.0 for job in values}
    jobs_by_id = {job.job_id: job for job in values}
    allocated: dict[str, int] = {}
    pending = set(jobs_by_id)
    remaining = global_limit
    while pending and remaining > 0:
        total_weight = sum(weights.get(identity, 1.0) for identity in pending) or float(len(pending))
        capped = []
        for identity in pending:
            share = remaining * weights.get(identity, 1.0) / total_weight
            own = positive_limit(jobs_by_id[identity].settings.speed_limit_bps)
            if own is not None and own <= share:
                allocated[identity] = own
                remaining -= own
                capped.append(identity)
        if not capped:
            for identity in sorted(pending):
                share = max(1, int(remaining * weights.get(identity, 1.0) / total_weight))
                allocated[identity] = share
            break
        pending.difference_update(capped)
    for identity in pending:
        allocated.setdefault(identity, 1)
    return allocated


def finish_fastest_weights(jobs) -> dict[str, float]:
    def remaining(job):
        total = job.progress.total_bytes
        if total is None or total <= 0:
            return float("inf")
        return max(0, total - job.progress.downloaded_bytes)

    target = min(jobs, key=lambda job: (remaining(job), job.queue_position))
    weights = {job.job_id: 1.0 for job in jobs}
    weights[target.job_id] = max(9.0, float(len(jobs) * 4))
    return weights


def selected_weights(jobs, selected_job_id: str | None) -> dict[str, float]:
    weights = {job.job_id: 1.0 for job in jobs}
    if selected_job_id in weights:
        weights[selected_job_id] = max(7.0, float(len(jobs) * 3))
    return weights


def positive_limit(value) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None
