"""DEC-009 visual dedup: 64-bit dHash over a 9x8 grayscale downsample.

OpenCV decodes; the hash comparison itself is pure. cv2 is imported LAZILY:
the image stack needs system libraries, but dedup must never break ingestion —
if decoding is unavailable, dhash returns None and the visual layer is skipped.

Re-forwarded / re-compressed screenshots keep a small Hamming distance, so the
caller compares within a bit budget rather than requiring exact equality.
"""


def dhash(image_bytes: bytes) -> str | None:
    """64-bit dHash as 16 hex chars, or None when the bytes are not decodable
    as an image (never raises — dedup must not break ingestion)."""
    try:  # lazy import: a broken image stack must not crash the importing app
        import cv2
        import numpy as np

        array: np.ndarray = np.frombuffer(image_bytes, dtype=np.uint8)
        decoded = cv2.imdecode(array, cv2.IMREAD_GRAYSCALE)
        if decoded is None:
            return None
        small = cv2.resize(decoded, (9, 8), interpolation=cv2.INTER_AREA)
        bits = small[:, 1:] > small[:, :-1]  # 8 rows x 8 comparisons = 64 bits
        packed = np.packbits(bits.flatten())
        return format(int.from_bytes(packed.tobytes(), "big"), "016x")
    except Exception:  # noqa: BLE001 — any decode failure yields "no hash"
        return None


def hamming_hex(a: str | None, b: str | None) -> int | None:
    """Bit distance between two hex hashes; None when either is missing."""
    if not a or not b:
        return None
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return None
