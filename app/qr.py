"""QR codes for the room controller, without a QR library.

The appliance ships a short dependency list on purpose and lives on room
networks that often have no route to the internet, so pulling in a QR package
is not an option: this module encodes the pairing URL itself.

Scope, deliberately narrow and stated up front:

* **Byte mode only.** The payload is an ASCII URL. Anything else is encoded as
  UTF-8 with no ECI header, which is what readers assume in practice, but this
  module makes no promises about non-ASCII text.
* **Versions 1 to 10** (21x21 up to 57x57 modules), chosen automatically for
  the data. Version 10 holds 213 bytes at level M, roughly three times the
  pairing URL; going further would only shrink the modules on the TV.
* **All four error-correction levels** (L, M, Q, H), defaulting to M. A QR on a
  TV is read from a metre or two in good light, so M is plenty, and every step
  up costs modules. The level is a parameter and is correct for each level.
* Reed-Solomon error correction over GF(256), the eight data masks scored with
  the four standard penalty rules, and BCH-protected format and version
  information.

Not implemented, and not needed here: numeric, alphanumeric and kanji modes,
ECI, structured append, Micro QR, and versions 11 to 40. Data that does not fit
raises :class:`DataTooLongError` rather than quietly producing a symbol no
phone can read.

Correctness is not taken on trust: tests/test_qr.py compares the output
module-for-module against the ``segno`` package across every level and the
lengths where the version rolls over. ``segno`` is a development-only
dependency and is deliberately absent from requirements.txt and the appliance.

Section numbers in the comments refer to ISO/IEC 18004.
"""

from __future__ import annotations

import re

__all__ = ["DataTooLongError", "QRCodeError", "EC_LEVELS", "qr_matrix", "qr_svg"]


class QRCodeError(ValueError):
    """Something about the request cannot produce a QR code."""


class DataTooLongError(QRCodeError):
    """The data does not fit in the versions this module implements."""


# The versions implemented here. See the module docstring for why it stops at
# ten rather than forty.
MIN_VERSION = 1
MAX_VERSION = 10

EC_LEVELS = ("L", "M", "Q", "H")

# 7.9.1 Table 12: the two bits identifying the level inside the format
# information. They are deliberately not in L < M < Q < H order.
_EC_FORMAT_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}

_BYTE_MODE = 0b0100

# Table 9, restricted to the versions above. For each version and level:
#   (error-correction codewords per block,
#    ((number of blocks, data codewords in each), ...))
# A version with two entries has two block sizes differing by one codeword;
# the larger blocks always come second.
_EC_BLOCKS: dict[int, dict[str, tuple[int, tuple[tuple[int, int], ...]]]] = {
    1: {
        "L": (7, ((1, 19),)),
        "M": (10, ((1, 16),)),
        "Q": (13, ((1, 13),)),
        "H": (17, ((1, 9),)),
    },
    2: {
        "L": (10, ((1, 34),)),
        "M": (16, ((1, 28),)),
        "Q": (22, ((1, 22),)),
        "H": (28, ((1, 16),)),
    },
    3: {
        "L": (15, ((1, 55),)),
        "M": (26, ((1, 44),)),
        "Q": (18, ((2, 17),)),
        "H": (22, ((2, 13),)),
    },
    4: {
        "L": (20, ((1, 80),)),
        "M": (18, ((2, 32),)),
        "Q": (26, ((2, 24),)),
        "H": (16, ((4, 9),)),
    },
    5: {
        "L": (26, ((1, 108),)),
        "M": (24, ((2, 43),)),
        "Q": (18, ((2, 15), (2, 16))),
        "H": (22, ((2, 11), (2, 12))),
    },
    6: {
        "L": (18, ((2, 68),)),
        "M": (16, ((4, 27),)),
        "Q": (24, ((4, 19),)),
        "H": (28, ((4, 15),)),
    },
    7: {
        "L": (20, ((2, 78),)),
        "M": (18, ((4, 31),)),
        "Q": (18, ((2, 14), (4, 15))),
        "H": (26, ((4, 13), (1, 14))),
    },
    8: {
        "L": (24, ((2, 97),)),
        "M": (22, ((2, 38), (2, 39))),
        "Q": (22, ((4, 18), (2, 19))),
        "H": (26, ((4, 14), (2, 15))),
    },
    9: {
        "L": (30, ((2, 116),)),
        "M": (22, ((3, 36), (2, 37))),
        "Q": (20, ((4, 16), (4, 17))),
        "H": (24, ((4, 12), (4, 13))),
    },
    10: {
        "L": (18, ((2, 68), (2, 69))),
        "M": (26, ((4, 43), (1, 44))),
        "Q": (24, ((6, 19), (2, 20))),
        "H": (28, ((6, 15), (2, 16))),
    },
}

# Annex E: the row/column coordinates of the alignment pattern centres. Every
# combination gets a pattern except the three that would sit on a finder.
_ALIGNMENT_POSITIONS: dict[int, tuple[int, ...]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
}


# --------------------------------------------------------------- GF(256)
#
# Reed-Solomon works over the field GF(256): 256 "numbers" that can be added
# and multiplied without ever leaving the set. Addition is XOR. Multiplication
# is polynomial multiplication modulo 0x11D (x^8 + x^4 + x^3 + x^2 + 1, the
# primitive polynomial QR codes use, 8.5.2), which is slow to do directly.
#
# Every non-zero element is a power of 2, so tabulating those powers turns
# multiplication into an addition of exponents: a*b = 2^(log a + log b). The
# exponent table is stored twice end to end so that log a + log b (up to 508)
# never needs a modulo.
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _build_gf_tables() -> None:
    value = 1
    for exponent in range(255):
        _GF_EXP[exponent] = value
        _GF_LOG[value] = exponent
        value <<= 1
        if value & 0x100:  # degree 8 reached: reduce modulo the polynomial
            value ^= 0x11D
    for exponent in range(255, 512):
        _GF_EXP[exponent] = _GF_EXP[exponent - 255]


_build_gf_tables()


def _gf_multiply(left: int, right: int) -> int:
    """Multiply two GF(256) elements. Zero has no logarithm, hence the guard."""
    if left == 0 or right == 0:
        return 0
    return _GF_EXP[_GF_LOG[left] + _GF_LOG[right]]


def _rs_generator(degree: int) -> list[int]:
    """The generator polynomial (x - 2^0)(x - 2^1)...(x - 2^(degree-1)).

    Coefficients run from the highest power down, so the leading one is always
    1. Subtraction is XOR in this field, so the signs do not matter.
    """
    poly = [1]
    for exponent in range(degree):
        # Multiply by (x + 2^exponent), one term at a time.
        product = [0] * (len(poly) + 1)
        for index, coefficient in enumerate(poly):
            product[index] ^= coefficient  # coefficient * x
            product[index + 1] ^= _gf_multiply(coefficient, _GF_EXP[exponent])
        poly = product
    return poly


# The generators are the same for every symbol, so build each one once.
_GENERATOR_CACHE: dict[int, list[int]] = {}


def _rs_error_codewords(data: bytes, count: int) -> bytearray:
    """The `count` error-correction codewords for one block of data.

    This is the remainder of the data polynomial (shifted up by `count`)
    divided by the generator polynomial: extended synthetic division, done in
    place because the leading coefficient of the generator is always 1.
    """
    generator = _GENERATOR_CACHE.get(count)
    if generator is None:
        generator = _rs_generator(count)
        _GENERATOR_CACHE[count] = generator

    remainder = bytearray(count)
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + bytearray(1)
        if factor:  # multiplying by zero would only XOR in zeros
            for index in range(count):
                remainder[index] ^= _gf_multiply(generator[index + 1], factor)
    return remainder


# ------------------------------------------------------------ bit stream


class _Bits:
    """A most-significant-bit-first bit stream, as a list of 0/1 ints."""

    def __init__(self) -> None:
        self.bits: list[int] = []

    def __len__(self) -> int:
        return len(self.bits)

    def append(self, value: int, length: int) -> None:
        for shift in range(length - 1, -1, -1):
            self.bits.append((value >> shift) & 1)

    def to_bytes(self) -> bytearray:
        data = bytearray(len(self.bits) // 8)
        for index, bit in enumerate(self.bits):
            if bit:
                data[index >> 3] |= 0x80 >> (index & 7)
        return data


def _character_count_bits(version: int) -> int:
    """Table 3: byte mode counts characters in 8 bits up to version 9, 16 after."""
    return 8 if version <= 9 else 16


def _data_codewords(version: int, ec: str) -> int:
    ec_per_block, groups = _EC_BLOCKS[version][ec]
    return sum(blocks * per_block for blocks, per_block in groups)


def _byte_capacity(version: int, ec: str) -> int:
    """How many payload bytes fit, after the mode and length header."""
    header = 4 + _character_count_bits(version)
    return (_data_codewords(version, ec) * 8 - header) // 8


def _choose_version(payload: bytes, ec: str) -> int:
    """The smallest implemented version that holds the payload."""
    for version in range(MIN_VERSION, MAX_VERSION + 1):
        if len(payload) <= _byte_capacity(version, ec):
            return version
    raise DataTooLongError(
        f"{len(payload)} bytes of data will not fit in a QR code: the largest "
        f"symbol this module builds (version {MAX_VERSION}, error correction "
        f"{ec}) holds {_byte_capacity(MAX_VERSION, ec)} bytes. Shorten the "
        f"data, or use a lower error-correction level."
    )


def _encode_payload(payload: bytes, version: int, ec: str) -> bytearray:
    """7.4: mode, length, data, terminator and padding, as whole codewords."""
    capacity_bits = _data_codewords(version, ec) * 8
    bits = _Bits()
    bits.append(_BYTE_MODE, 4)
    bits.append(len(payload), _character_count_bits(version))
    for byte in payload:
        bits.append(byte, 8)

    # 7.4.9: up to four zero bits mark the end, but a nearly full symbol simply
    # gets fewer of them rather than overflowing.
    bits.append(0, min(4, capacity_bits - len(bits)))
    # 7.4.10: pad to a codeword boundary, then repeat these two filler
    # codewords (11101100, 00010001) until the symbol is full.
    bits.append(0, -len(bits) % 8)
    padding = (0xEC, 0x11)
    index = 0
    while len(bits) < capacity_bits:
        bits.append(padding[index % 2], 8)
        index += 1
    return bits.to_bytes()


def _final_message(data: bytearray, version: int, ec: str) -> bytearray:
    """7.6: split into blocks, add error correction, then interleave.

    Interleaving is what makes a QR code survive a coffee ring: a scratch that
    destroys a run of consecutive codewords in the picture damages one or two
    codewords in each block rather than wiping out a whole block, and each
    block can repair a few codewords of its own.
    """
    ec_per_block, groups = _EC_BLOCKS[version][ec]
    data_blocks: list[bytearray] = []
    ec_blocks: list[bytearray] = []
    offset = 0
    for blocks, per_block in groups:
        for _ in range(blocks):
            block = data[offset : offset + per_block]
            offset += per_block
            data_blocks.append(block)
            ec_blocks.append(_rs_error_codewords(bytes(block), ec_per_block))

    message = bytearray()
    # Take the first codeword of every block, then the second, and so on. The
    # short blocks (when a version mixes two sizes) simply run out first.
    longest = max(len(block) for block in data_blocks)
    for index in range(longest):
        for block in data_blocks:
            if index < len(block):
                message.append(block[index])
    # The error-correction blocks are all the same length, so they interleave
    # without the ragged edge.
    for index in range(ec_per_block):
        for block in ec_blocks:
            message.append(block[index])
    return message


# ------------------------------------------------------------ the symbol

_FINDER = (
    (1, 1, 1, 1, 1, 1, 1),
    (1, 0, 0, 0, 0, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 0, 0, 0, 0, 1),
    (1, 1, 1, 1, 1, 1, 1),
)

# 7.8.2 Table 10. Each returns True where the module is flipped.
_MASKS = (
    lambda row, col: (row + col) % 2 == 0,
    lambda row, col: row % 2 == 0,
    lambda row, col: col % 3 == 0,
    lambda row, col: (row + col) % 3 == 0,
    lambda row, col: (row // 2 + col // 3) % 2 == 0,
    lambda row, col: (row * col) % 2 + (row * col) % 3 == 0,
    lambda row, col: ((row * col) % 2 + (row * col) % 3) % 2 == 0,
    lambda row, col: ((row + col) % 2 + (row * col) % 3) % 2 == 0,
)

# The 1:1:3:1:1 dark/light run that penalty rule 3 hunts for: it is the
# signature of a finder pattern, and one in the data would confuse a reader.
_FINDER_RUN = bytes((1, 0, 1, 1, 1, 0, 1))


class _Symbol:
    """One QR symbol under construction."""

    def __init__(self, version: int) -> None:
        self.version = version
        self.size = version * 4 + 17
        # 0/1 ints rather than booleans: the penalty scoring below searches
        # whole rows as bytes, which is far quicker than module by module.
        self.modules = [bytearray(self.size) for _ in range(self.size)]
        # Function modules (finders, timing, alignment, and the reserved format
        # and version areas) are never masked and never carry data.
        self.function = [bytearray(self.size) for _ in range(self.size)]

    # -- construction ----------------------------------------------------

    def _set_function(self, row: int, col: int, dark: int) -> None:
        self.modules[row][col] = dark
        self.function[row][col] = 1

    def draw_function_patterns(self) -> None:
        size = self.size
        # 6.3.3/6.3.4: three finders, each with a one-module light separator on
        # the sides that face into the symbol.
        for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
            for row in range(-1, 8):
                for col in range(-1, 8):
                    r, c = top + row, left + col
                    if 0 <= r < size and 0 <= c < size:
                        inside = 0 <= row < 7 and 0 <= col < 7
                        self._set_function(r, c, _FINDER[row][col] if inside else 0)

        # 6.3.5: the timing patterns run between the finders and give a reader
        # the module pitch.
        for offset in range(8, size - 8):
            dark = 1 - (offset % 2)
            self._set_function(6, offset, dark)
            self._set_function(offset, 6, dark)

        # 6.3.6: alignment patterns, skipping the three that would land on a
        # finder pattern.
        positions = _ALIGNMENT_POSITIONS[self.version]
        if positions:
            first, last = positions[0], positions[-1]
            skip = {(first, first), (first, last), (last, first)}
            for row in positions:
                for col in positions:
                    if (row, col) in skip:
                        continue
                    for dr in range(-2, 3):
                        for dc in range(-2, 3):
                            ring = max(abs(dr), abs(dc))
                            self._set_function(row + dr, col + dc, int(ring != 1))

        # Reserve the format information (7.9) around the finders, including
        # the module at (size - 8, 8) that is always dark. The bits themselves
        # depend on the mask, so they are written once the mask is chosen.
        for offset in range(9):
            # (8, 6) and (6, 8) look like they belong here, but they are the
            # first module of each timing pattern and must keep that value.
            if offset == 6:
                continue
            self._set_function(8, offset, 0)
            self._set_function(offset, 8, 0)
        for offset in range(8):
            self._set_function(8, size - 1 - offset, 0)
            self._set_function(size - 1 - offset, 8, 0)

        # 7.10: versions 7 and up repeat their version number in two corners.
        if self.version >= 7:
            for index in range(18):
                row, col = index // 3, size - 11 + index % 3
                self._set_function(row, col, 0)
                self._set_function(col, row, 0)

    def draw_codewords(self, message: bytes) -> None:
        """7.7.3: the message zig-zags up and down two-module-wide columns.

        Placement starts at the bottom right and works left, skipping the
        column occupied by the vertical timing pattern. Any modules left over
        at the end (the "remainder bits" some versions need) stay light; they
        are still masked, exactly as a zero data bit would be.
        """
        size = self.size
        bit_index = 0
        total_bits = len(message) * 8
        right = size - 1
        while right >= 1:
            if right == 6:  # the timing column is not part of a pair
                right = 5
            for vertical in range(size):
                for column in (right, right - 1):
                    upward = ((right + 1) & 2) == 0
                    row = (size - 1 - vertical) if upward else vertical
                    if self.function[row][column] or bit_index >= total_bits:
                        continue
                    byte = message[bit_index >> 3]
                    self.modules[row][column] = (byte >> (7 - (bit_index & 7))) & 1
                    bit_index += 1
            right -= 2

    def apply_mask(self, pattern: int) -> None:
        """XOR one of the eight masks over the data modules (7.8.1).

        Calling it a second time with the same pattern undoes it.
        """
        mask = _MASKS[pattern]
        for row in range(self.size):
            modules = self.modules[row]
            function = self.function[row]
            for col in range(self.size):
                if not function[col] and mask(row, col):
                    modules[col] ^= 1

    def draw_format_info(self, ec: str, pattern: int) -> None:
        """7.9: five bits of level and mask, protected by a BCH(15,5) code."""
        data = (_EC_FORMAT_BITS[ec] << 3) | pattern
        remainder = data
        for _ in range(10):
            # Divide by the generator 0b10100110111 (0x537) over GF(2): shift
            # up, and whenever the result reaches degree 10, XOR it away.
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        # The mask keeps the all-zero format (level M, mask 0) from looking
        # like a blank area to a reader.
        bits = ((data << 10) | remainder) ^ 0x5412

        size = self.size
        for index in range(15):
            bit = (bits >> index) & 1
            # First copy: down the left of the top-right finder, then left
            # along the row under the top-left finder, hopping over the timing
            # module in both cases.
            if index < 6:
                self._set_function(index, 8, bit)
            elif index < 8:
                self._set_function(index + 1, 8, bit)
            elif index == 8:
                self._set_function(8, 7, bit)
            else:
                self._set_function(8, 14 - index, bit)

            # Second copy: along the row beside the top-right finder and up
            # from the bottom-left one, so damage to one corner is survivable.
            if index < 8:
                self._set_function(8, size - 1 - index, bit)
            else:
                self._set_function(size - 15 + index, 8, bit)

        self._set_function(size - 8, 8, 1)  # the module that is always dark

    def draw_version_info(self) -> None:
        """7.10: six version bits with a BCH(18,6) code, on versions 7 and up."""
        if self.version < 7:
            return
        remainder = self.version
        for _ in range(12):
            remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
        bits = (self.version << 12) | remainder

        size = self.size
        for index in range(18):
            bit = (bits >> index) & 1
            row, col = index // 3, size - 11 + index % 3
            self._set_function(row, col, bit)  # above the bottom-left finder
            self._set_function(col, row, bit)  # left of the top-right finder

    # -- mask selection --------------------------------------------------

    def choose_mask(self) -> int:
        """Score all eight masks and keep the least bad one (7.8.3).

        The format information is *not* on the symbol yet, so those modules
        score as light. That is deliberate: the format bits encode the mask, so
        including them would make the score depend on its own answer. It also
        matches segno, which the tests compare against.
        """
        best_pattern = 0
        best_score = None
        for pattern in range(8):
            self.apply_mask(pattern)
            score = self.penalty()
            self.apply_mask(pattern)  # undo
            if best_score is None or score < best_score:
                best_score, best_pattern = score, pattern
        return best_pattern

    def penalty(self) -> int:
        """The four penalty rules of Table 11, summed.

        They exist to steer the mask away from patterns that confuse readers:
        long same-coloured runs, large blocks, anything resembling a finder,
        and an unbalanced light/dark mix.
        """
        size = self.size
        rows = self.modules
        columns = [bytearray(rows[row][col] for row in range(size)) for col in range(size)]

        score = 0
        for line in rows:
            score += _run_penalty(line) + _finder_penalty(line, size)
        for line in columns:
            score += _run_penalty(line) + _finder_penalty(line, size)

        # Rule 2: every 2x2 block of one colour costs 3. A larger block is
        # counted once per 2x2 square inside it, which is what the standard's
        # 3 * (m - 1) * (n - 1) works out to.
        for row in range(size - 1):
            upper, lower = rows[row], rows[row + 1]
            for col in range(size - 1):
                value = upper[col]
                if value == upper[col + 1] == lower[col] == lower[col + 1]:
                    score += 3

        # Rule 4: 10 points for every 5% the dark proportion strays from half.
        # Done in integers so rounding cannot drift at the boundaries.
        dark = sum(sum(row) for row in rows)
        total = size * size
        score += 10 * (abs(dark * 200 - total * 100) // (total * 10))
        return score


def _run_penalty(line: bytearray) -> int:
    """Rule 1: a run of five same-coloured modules costs 3, plus 1 per extra."""
    score = 0
    run = 1
    for index in range(1, len(line)):
        if line[index] == line[index - 1]:
            run += 1
            continue
        if run >= 5:
            score += run - 2
        run = 1
    if run >= 5:
        score += run - 2
    return score


def _finder_penalty(line: bytearray, size: int) -> int:
    """Rule 3: 40 points per finder-like run with a light margin beside it.

    The pattern only counts when four light modules precede or follow it. The
    quiet zone around the symbol is light, so a pattern flush against an edge
    counts. When a match does not qualify the search resumes four modules in,
    because these patterns can overlap each other.
    """
    score = 0
    view = bytes(line)
    index = view.find(_FINDER_RUN)
    while index != -1:
        after = index + 7
        if (
            index == 0
            or index == size - 7
            or not any(view[max(index - 4, 0) : index])
            or not any(view[after : after + 4])
        ):
            score += 40
            resume = after
        else:
            resume = index + 4
        index = view.find(_FINDER_RUN, resume)
    return score


# ---------------------------------------------------------- public API


def _normalise_ec(ec: str) -> str:
    level = str(ec or "").strip().upper()
    if level not in EC_LEVELS:
        raise QRCodeError(
            f"Unknown error-correction level {ec!r}: expected one of "
            + ", ".join(EC_LEVELS)
        )
    return level


def qr_matrix(data: str, *, ec: str = "M") -> list[list[bool]]:
    """Encode `data` and return the modules, `True` where dark.

    The matrix has no quiet zone: it is exactly the symbol, row by row from the
    top. Raises :class:`DataTooLongError` if the data is too long for the
    versions this module implements.
    """
    level = _normalise_ec(ec)
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)

    version = _choose_version(payload, level)
    message = _final_message(_encode_payload(payload, version, level), version, level)

    symbol = _Symbol(version)
    symbol.draw_function_patterns()
    symbol.draw_codewords(message)
    pattern = symbol.choose_mask()
    symbol.apply_mask(pattern)
    symbol.draw_format_info(level, pattern)
    symbol.draw_version_info()
    return [[bool(module) for module in row] for row in symbol.modules]


# Colours end up inside SVG attributes, so keep them to shapes that cannot
# close the attribute or open a tag: #rgb, #rrggbb or a plain CSS colour name.
_COLOUR = re.compile(r"^(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|[a-zA-Z]{3,20})$")


def _check_colour(value: str, name: str) -> str:
    if not _COLOUR.match(str(value or "")):
        raise QRCodeError(
            f"{name} must be a hex colour such as #000000 or a colour name, "
            f"not {value!r}"
        )
    return str(value)


def qr_svg(
    data: str,
    *,
    scale: int = 4,
    quiet_zone: int = 4,
    dark: str = "#000000",
    light: str = "#ffffff",
    ec: str = "M",
) -> str:
    """Encode `data` as a standalone SVG document.

    `scale` is the size of one module in SVG user units (which are CSS pixels
    here, since the document declares its own width and height) and
    `quiet_zone` the light margin around the symbol, in modules: four is the
    standard minimum and readers rely on it.

    The result carries no external references at all — no CSS, no script, no
    font — because it is served as image/svg+xml and displayed inside an
    ``<img>``, where such references would be ignored anyway.
    """
    scale = int(scale)
    quiet_zone = int(quiet_zone)
    if scale < 1:
        raise QRCodeError(f"scale must be at least 1 module, not {scale}")
    if quiet_zone < 0:
        raise QRCodeError(f"quiet_zone cannot be negative, got {quiet_zone}")
    dark = _check_colour(dark, "dark")
    light = _check_colour(light, "light")

    matrix = qr_matrix(data, ec=ec)
    size = len(matrix)
    extent = (size + quiet_zone * 2) * scale
    offset = quiet_zone * scale

    # One path of horizontal runs rather than a rect per module: a version 10
    # symbol has some 1,600 dark modules, and the runs cut that by roughly two
    # thirds without changing a pixel.
    parts: list[str] = []
    for row_index, row in enumerate(matrix):
        col = 0
        while col < size:
            if not row[col]:
                col += 1
                continue
            start = col
            while col < size and row[col]:
                col += 1
            width = (col - start) * scale
            x = offset + start * scale
            y = offset + row_index * scale
            parts.append(f"M{x} {y}h{width}v{scale}h-{width}z")
    path = "".join(parts)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        f'viewBox="0 0 {extent} {extent}" width="{extent}" height="{extent}" '
        'shape-rendering="crispEdges" role="img" aria-label="QR code">'
        "<title>QR code</title>"
        # The background covers the quiet zone too: without it the symbol would
        # sit on whatever is behind the image, and a dark wallpaper behind a
        # transparent margin stops it scanning.
        f'<rect x="0" y="0" width="{extent}" height="{extent}" fill="{light}"/>'
        f'<path fill="{dark}" d="{path}"/>'
        "</svg>\n"
    )
