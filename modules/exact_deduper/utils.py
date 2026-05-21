def hash_file(path, chunk_size=1024 * 1024) -> str:
    """
    Hash a file using blake3 if available, otherwise sha256.
    """

    try:
        import blake3
        hasher = blake3.blake3()
    except ImportError:
        import hashlib
        hasher = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)

    return hasher.hexdigest()
