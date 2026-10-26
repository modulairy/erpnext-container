"""Exercise the installed libraries, including CVE-2026-37555's WAV path."""
import ctypes
import os
import struct
import tempfile


class Info(ctypes.Structure):
    _fields_ = [('frames', ctypes.c_int64), ('samplerate', ctypes.c_int),
                ('channels', ctypes.c_int), ('format', ctypes.c_int),
                ('sections', ctypes.c_int), ('seekable', ctypes.c_int)]


def main():
    x11 = ctypes.CDLL('libX11.so.6')
    assert x11.XInitThreads() != 0
    ctypes.CDLL('libXrender.so.1')
    ctypes.CDLL('libXtst.so.6')
    lib = ctypes.CDLL('libsndfile.so.1')
    lib.sf_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(Info)]
    lib.sf_open.restype = ctypes.c_void_p
    lib.sf_close.argtypes = [ctypes.c_void_p]
    lib.sf_strerror.argtypes = [ctypes.c_void_p]
    lib.sf_strerror.restype = ctypes.c_char_p
    blockalign, blocks, samples = 25004, 50000, 50001
    data_size = blockalign*blocks
    fmt = struct.pack('<HHIIHHHH', 0x11, 1, 8000, 4000, blockalign, 4, 2, samples)
    header = (b'RIFF'+struct.pack('<I', 4+8+len(fmt)+8+data_size)+b'WAVEfmt '+
              struct.pack('<I', len(fmt))+fmt+b'data'+struct.pack('<I', data_size))
    # Sparse metadata fixture: only the small header is allocated or read.
    # The unpatched library overflows its frame calculation and rejects this file.
    with tempfile.NamedTemporaryFile() as fixture:
        fixture.write(header)
        fixture.truncate(len(header)+data_size)
        fixture.flush()
        info = Info()
        handle = lib.sf_open(os.fsencode(fixture.name), 0x10, ctypes.byref(info))
        assert handle, lib.sf_strerror(None)
        try:
            assert info.frames == samples*blocks, (info.frames, samples*blocks)
        finally:
            assert lib.sf_close(handle) == 0
    print('PASS: Xorg library loading and actual WAV IMA-ADPCM overflow regression', flush=True)


if __name__ == '__main__':
    main()
