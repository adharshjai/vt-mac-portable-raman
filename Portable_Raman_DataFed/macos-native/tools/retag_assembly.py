"""Copy a pure-IL .NET assembly and re-tag its PE machine field so the arm64 runtime
on macOS will load it. The IL and metadata are untouched; only the 2-byte COFF
'Machine' value changes (AMD64 0x8664 -> ARM64 0xAA64). Refuses assemblies that
contain native code (CLR header flag ILONLY must be set).
Usage: python retag_assembly.py <in.dll> <out.dll>"""
import struct, sys
src, dst = sys.argv[1], sys.argv[2]
d = bytearray(open(src, 'rb').read())
pe = struct.unpack_from('<I', d, 0x3C)[0]
assert d[pe:pe+4] == b'PE\0\0', 'not a PE file'
machine = struct.unpack_from('<H', d, pe + 4)[0]
opt = pe + 24; magic = struct.unpack_from('<H', d, opt)[0]; plus = magic == 0x20B
ddoff = opt + (112 if plus else 96)
clr_rva, clr_size = struct.unpack_from('<II', d, ddoff + 14 * 8)
assert clr_rva, 'no CLR header: not a managed assembly'
nsec = struct.unpack_from('<H', d, pe + 6)[0]; secoff = opt + struct.unpack_from('<H', d, pe + 20)[0]
def rva2off(r):
    for i in range(nsec):
        s = secoff + i * 40; vsize, va, rsize, raw = struct.unpack_from('<IIII', d, s + 8)
        if va <= r < va + max(vsize, rsize): return raw + (r - va)
    raise SystemExit('bad RVA')
flags = struct.unpack_from('<I', d, rva2off(clr_rva) + 16)[0]
assert flags & 1, 'assembly is not IL-only (contains native code); cannot re-tag'
print(f'input machine=0x{machine:04X} {"PE32+" if plus else "PE32"} CLR flags=0x{flags:X} (ILONLY)')
struct.pack_into('<H', d, pe + 4, 0xAA64)
open(dst, 'wb').write(d); print(f'wrote {dst} with machine=0xAA64 (ARM64); {len(d)} bytes, everything else identical')
