"""
Turning a SecurityAccess seed into the key the ECU expects.

Two sources: the simple mask the simulated ECU uses, and a seed & key DLL of the kind car makers hand
out, which CAN Expert calls the same way a Vector tool does - GenerateKeyEx, the shape those DLLs are
written to. The call is kept apart from the window so it can be tested against a stub.
"""
from __future__ import annotations

import ctypes
from pathlib import Path

KEY_BUFFER = 256          # bytes offered to the DLL for the key it produces
RESULT_OK = 0
RESULT_NAMES = {0: "ok", 1: "buffer too small", 2: "security level not supported",
                3: "variant not supported", 4: "unspecified error"}


class SeedKeyError(Exception):
    """The DLL could not be loaded, or refused to produce a key."""


def xor_key(mask: int):
    """key = seed XOR mask, byte by byte - what dummy_ecu.py and the example script use."""
    return lambda seed: bytes(byte ^ (mask & 0xFF) for byte in seed)


def load_library(path):
    """The seed & key DLL at path, with GenerateKeyEx described to ctypes."""
    path = Path(path)
    if not path.exists():
        raise SeedKeyError(f"{path} does not exist")
    try:
        library = ctypes.CDLL(str(path))
    except OSError as exc:                 # wrong architecture is the usual reason
        raise SeedKeyError(f"Cannot load {path.name}: {exc}") from None
    if not hasattr(library, "GenerateKeyEx"):
        raise SeedKeyError(f"{path.name} has no GenerateKeyEx function")
    library.GenerateKeyEx.restype = ctypes.c_int
    library.GenerateKeyEx.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint, ctypes.c_uint,
                                      ctypes.c_char_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint,
                                      ctypes.POINTER(ctypes.c_uint)]
    return library


def generate_key(library, seed: bytes, level: int, variant: str = "") -> bytes:
    """Call GenerateKeyEx(seed, level, variant) and return the key it wrote."""
    seed = bytes(seed)
    seed_array = (ctypes.c_ubyte * max(1, len(seed)))(*seed)
    key_array = (ctypes.c_ubyte * KEY_BUFFER)()
    size = ctypes.c_uint(0)
    result = library.GenerateKeyEx(seed_array, len(seed), int(level), variant.encode("latin-1"),
                                   key_array, KEY_BUFFER, ctypes.byref(size))
    if result != RESULT_OK:
        raise SeedKeyError(f"GenerateKeyEx: {RESULT_NAMES.get(result, f'error {result}')}")
    if not 0 < size.value <= KEY_BUFFER:
        raise SeedKeyError(f"GenerateKeyEx returned a key of {size.value} bytes")
    return bytes(key_array[:size.value])


def dll_key(path, level: int, variant: str = "", library=None):
    """compute_key(seed) -> key, backed by a seed & key DLL. The library is loaded once, not per seed."""
    library = library if library is not None else load_library(path)
    return lambda seed: generate_key(library, seed, level, variant)
