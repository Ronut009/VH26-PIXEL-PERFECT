import hashlib


def compute_fingerprint(service: str, alertname: str, labels: dict[str, str]) -> str:
    label_part = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    raw = f"{service}|{alertname}|{label_part}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
