"""The dependency-free QR encoder in app/qr.py.

Three layers of checking, because a QR code that scans on a laptop but not on a
phone is worse than none at all:

* structural checks that stand on their own (the shape of the symbol, the
  finders, the timing patterns, the SVG);
* a round trip — the tests read the symbol back the way a scanner would, which
  exercises the mask, the interleaving and the Reed-Solomon codewords without
  trusting the encoder's own view of them;
* a module-for-module comparison against `segno`, when it is installed. It is a
  development-only dependency: the appliance and CI do not have it, so those
  comparisons skip rather than fail.

The reader below is written from the standard rather than reusing the
encoder's placement code, so the two have to agree by accident of being right.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from app import qr
from app.qr import DataTooLongError, QRCodeError, qr_matrix, qr_svg

try:  # pragma: no cover - depends on the developer's machine
    import segno
except ImportError:  # pragma: no cover
    segno = None

needs_segno = pytest.mark.skipif(segno is None, reason="segno is not installed")

SVG = "{http://www.w3.org/2000/svg}"

# The real payload: the pairing URL the kiosk puts in the corner of the TV.
CONTROLLER_URL = "http://192.168.1.20:8080/c/abc123DEF456"

SAMPLES = [
    "",
    "A",
    "hello",
    CONTROLLER_URL,
    "http://10.0.0.5:8080/c/" + "Q7wR2t" * 2,
    "https://example.com/a/longer/path?with=query&more=1#fragment",
    "0123456789" * 6,
    "x" * 100,
]


# --------------------------------------------------------------- a reader
#
# Enough of a QR decoder to prove the encoder produced something a scanner can
# read: format information, mask, codeword traversal, de-interleaving, a
# Reed-Solomon syndrome check and the byte-mode payload.

_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _build_gf():
    value = 1
    for exponent in range(255):
        _GF_EXP[exponent] = value
        _GF_LOG[value] = exponent
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for exponent in range(255, 512):
        _GF_EXP[exponent] = _GF_EXP[exponent - 255]


_build_gf()


def _gf_multiply(left, right):
    if left == 0 or right == 0:
        return 0
    return _GF_EXP[_GF_LOG[left] + _GF_LOG[right]]


def _format_value(level_bits, mask):
    """The 15 bits the standard says a symbol with this level and mask carries."""
    data = (level_bits << 3) | mask
    remainder = data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
    return ((data << 10) | remainder) ^ 0x5412


_FORMAT_VALUES = {
    _format_value(bits, mask): (level, mask)
    for level, bits in (("L", 0b01), ("M", 0b00), ("Q", 0b11), ("H", 0b10))
    for mask in range(8)
}


def function_modules(size):
    """Every module a reader must skip when collecting codewords."""
    version = (size - 17) // 4
    grid = [[False] * size for _ in range(size)]

    def fill(rows, cols):
        for row in rows:
            for col in cols:
                grid[row][col] = True

    # The finders with their separators occupy an 8x8 corner each.
    fill(range(8), range(8))
    fill(range(8), range(size - 8, size))
    fill(range(size - 8, size), range(8))
    # Both timing patterns, and the format information beside every finder.
    fill([6], range(size))
    fill(range(size), [6])
    fill([8], list(range(9)) + list(range(size - 8, size)))
    fill(list(range(9)) + list(range(size - 8, size)), [8])
    # Alignment patterns, minus the three that would sit on a finder.
    positions = qr._ALIGNMENT_POSITIONS[version]
    if positions:
        corners = {
            (positions[0], positions[0]),
            (positions[0], positions[-1]),
            (positions[-1], positions[0]),
        }
        for row in positions:
            for col in positions:
                if (row, col) not in corners:
                    fill(range(row - 2, row + 3), range(col - 2, col + 3))
    if version >= 7:
        fill(range(6), range(size - 11, size - 8))
        fill(range(size - 11, size - 8), range(6))
    return grid


def read_format(matrix):
    """The error-correction level and mask, from both copies of the field."""
    size = len(matrix)
    first = second = 0
    for index in range(15):
        if index < 6:
            row, col = index, 8
        elif index < 8:
            row, col = index + 1, 8
        elif index == 8:
            row, col = 8, 7
        else:
            row, col = 8, 14 - index
        first |= int(matrix[row][col]) << index

        if index < 8:
            row, col = 8, size - 1 - index
        else:
            row, col = size - 15 + index, 8
        second |= int(matrix[row][col]) << index

    assert first == second, "the two copies of the format information disagree"
    assert first in _FORMAT_VALUES, f"format bits {first:015b} are not a valid code"
    return _FORMAT_VALUES[first]


def read_codewords(matrix, mask):
    """Undo the mask and walk the zig-zag, most significant bit first."""
    size = len(matrix)
    skip = function_modules(size)
    flip = _MASKS[mask]
    bits = []
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for step in range(size):
            for col in (right, right - 1):
                upward = ((right + 1) & 2) == 0
                row = size - 1 - step if upward else step
                if skip[row][col]:
                    continue
                value = bool(matrix[row][col])
                bits.append(int(value ^ flip(row, col)))
        right -= 2

    codewords = bytearray()
    for index in range(0, len(bits) - 7, 8):
        byte = 0
        for bit in bits[index : index + 8]:
            byte = (byte << 1) | bit
        codewords.append(byte)
    return codewords


def decode(matrix):
    """Read a symbol back: returns (level, mask, payload bytes)."""
    size = len(matrix)
    version = (size - 17) // 4
    level, mask = read_format(matrix)
    stream = read_codewords(matrix, mask)

    ec_per_block, groups = qr._EC_BLOCKS[version][level]
    lengths = [per for count, per in groups for _ in range(count)]
    total = sum(lengths) + ec_per_block * len(lengths)
    stream = stream[:total]

    # De-interleave: the encoder took one codeword from each block in turn.
    blocks = [bytearray() for _ in lengths]
    position = 0
    for index in range(max(lengths)):
        for block, length in zip(blocks, lengths):
            if index < length:
                block.append(stream[position])
                position += 1
    ec_blocks = [bytearray() for _ in lengths]
    for _ in range(ec_per_block):
        for block in ec_blocks:
            block.append(stream[position])
            position += 1

    # Every block, data plus its error-correction codewords, is a multiple of
    # the generator polynomial: evaluating it at the first `ec_per_block`
    # powers of 2 must give zero, or the codewords are wrong.
    for data_block, ec_block in zip(blocks, ec_blocks):
        codeword = data_block + ec_block
        for power in range(ec_per_block):
            result = 0
            for coefficient in codeword:
                result = _gf_multiply(result, _GF_EXP[power]) ^ coefficient
            assert result == 0, "a block fails its Reed-Solomon check"

    data = bytearray()
    for block in blocks:
        data.extend(block)
    bits = "".join(f"{byte:08b}" for byte in data)
    assert bits[:4] == "0100", "not byte mode"
    count_bits = 8 if version <= 9 else 16
    length = int(bits[4 : 4 + count_bits], 2)
    payload = bytearray()
    for index in range(length):
        start = 4 + count_bits + index * 8
        payload.append(int(bits[start : start + 8], 2))
    return level, mask, bytes(payload)


# ------------------------------------------------------------- structure


class TestStructure:
    @pytest.mark.parametrize(
        ("data", "version"),
        [
            ("", 1),
            ("x" * 14, 1),
            ("x" * 15, 2),
            ("x" * 26, 2),
            ("x" * 27, 3),
            (CONTROLLER_URL, 3),
            ("x" * 122, 7),
            ("x" * 123, 8),
            ("x" * 213, 10),
        ],
    )
    def test_the_smallest_version_that_fits_is_used(self, data, version):
        """Level M, the default: 14 bytes fit in version 1, 15 do not."""
        matrix = qr_matrix(data)
        assert len(matrix) == version * 4 + 17

    def test_the_matrix_is_square(self):
        matrix = qr_matrix(CONTROLLER_URL)
        assert all(len(row) == len(matrix) for row in matrix)
        assert all(isinstance(module, bool) for row in matrix for module in row)

    @pytest.mark.parametrize("level", ["L", "M", "Q", "H"])
    def test_every_error_correction_level_builds_a_symbol(self, level):
        matrix = qr_matrix(CONTROLLER_URL, ec=level)
        size = len(matrix)
        assert (size - 17) % 4 == 0 and 21 <= size <= 57

    def test_stronger_error_correction_never_shrinks_the_symbol(self):
        sizes = [len(qr_matrix(CONTROLLER_URL, ec=level)) for level in "LMQH"]
        assert sizes == sorted(sizes)

    @pytest.mark.parametrize("data", SAMPLES)
    def test_the_three_finder_patterns_are_exact(self, data):
        """Seven rows of a fixed pattern, plus a light separator around them."""
        matrix = qr_matrix(data)
        size = len(matrix)
        expected = [
            "#######",
            "#     #",
            "# ### #",
            "# ### #",
            "# ### #",
            "#     #",
            "#######",
        ]
        for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
            for row in range(7):
                drawn = "".join(
                    "#" if matrix[top + row][left + col] else " " for col in range(7)
                )
                assert drawn == expected[row], f"finder at {(top, left)} row {row}"
            # The separator: the ring of modules just outside the finder that
            # lies inside the symbol must be light.
            for offset in range(-1, 8):
                for row, col in ((top + offset, left - 1), (top + offset, left + 7),
                                 (top - 1, left + offset), (top + 7, left + offset)):
                    if 0 <= row < size and 0 <= col < size:
                        assert not matrix[row][col], f"separator at {(row, col)}"

    @pytest.mark.parametrize("data", SAMPLES)
    def test_the_timing_patterns_alternate(self, data):
        matrix = qr_matrix(data)
        size = len(matrix)
        for offset in range(8, size - 8):
            expected = offset % 2 == 0
            assert matrix[6][offset] is expected, f"row 6, column {offset}"
            assert matrix[offset][6] is expected, f"column 6, row {offset}"

    @pytest.mark.parametrize("data", SAMPLES)
    def test_the_dark_module_is_set(self, data):
        """The one module that is dark in every symbol ever made."""
        matrix = qr_matrix(data)
        assert matrix[len(matrix) - 8][8] is True

    def test_alignment_patterns_appear_from_version_two(self):
        matrix = qr_matrix(CONTROLLER_URL)  # version 3: one pattern at (22, 22)
        centre = 22
        assert matrix[centre][centre] is True
        assert not matrix[centre][centre - 1]
        assert matrix[centre][centre - 2] is True

    def test_output_is_deterministic(self):
        assert qr_matrix(CONTROLLER_URL) == qr_matrix(CONTROLLER_URL)
        assert qr_svg(CONTROLLER_URL) == qr_svg(CONTROLLER_URL)

    def test_data_that_will_not_fit_is_refused(self):
        with pytest.raises(DataTooLongError) as excinfo:
            qr_matrix("x" * 214)
        message = str(excinfo.value)
        assert "213" in message and "version 10" in message
        assert isinstance(excinfo.value, ValueError)

    def test_the_limit_depends_on_the_error_correction_level(self):
        assert len(qr_matrix("x" * 119, ec="H")) == 57
        with pytest.raises(DataTooLongError):
            qr_matrix("x" * 120, ec="H")

    def test_an_unknown_error_correction_level_is_refused(self):
        with pytest.raises(QRCodeError):
            qr_matrix("hello", ec="X")

    @pytest.mark.parametrize("version", range(1, 11))
    def test_the_block_table_agrees_with_itself(self, version):
        """Every level of a version must fill the same number of codewords."""
        totals = set()
        for level, (ec_per_block, groups) in qr._EC_BLOCKS[version].items():
            blocks = sum(count for count, _ in groups)
            totals.add(sum(count * per for count, per in groups) + ec_per_block * blocks)
        assert len(totals) == 1, f"version {version}: {totals}"

    @pytest.mark.parametrize("version", range(1, 11))
    def test_the_codewords_fill_the_symbol(self, version):
        """The table's codeword count must match the room actually available."""
        symbol = qr._Symbol(version)
        symbol.draw_function_patterns()
        free = sum(row.count(0) for row in symbol.function)
        total = 0
        for ec_per_block, groups in [qr._EC_BLOCKS[version]["M"]]:
            blocks = sum(count for count, _ in groups)
            total = sum(count * per for count, per in groups) + ec_per_block * blocks
        remainder = 7 if 2 <= version <= 6 else 0
        assert free == total * 8 + remainder


# ------------------------------------------------------------ round trip


class TestRoundTrip:
    @pytest.mark.parametrize("data", SAMPLES)
    @pytest.mark.parametrize("level", ["L", "M", "Q", "H"])
    def test_the_symbol_reads_back_as_the_original_data(self, data, level):
        matrix = qr_matrix(data, ec=level)
        read_level, mask, payload = decode(matrix)
        assert read_level == level
        assert 0 <= mask <= 7
        assert payload == data.encode("utf-8")

    def test_a_symbol_at_every_version_reads_back(self):
        """One payload per version, sized to fill it exactly."""
        for version in range(1, 11):
            data = "d" * qr._byte_capacity(version, "M")
            matrix = qr_matrix(data, ec="M")
            assert len(matrix) == version * 4 + 17
            assert decode(matrix)[2] == data.encode()

    def test_the_mask_chosen_is_the_lowest_scoring_one(self):
        """The eight masks are really evaluated, not merely applied."""
        payload = CONTROLLER_URL.encode()
        version = qr._choose_version(payload, "M")
        message = qr._final_message(qr._encode_payload(payload, version, "M"), version, "M")
        symbol = qr._Symbol(version)
        symbol.draw_function_patterns()
        symbol.draw_codewords(message)

        scores = []
        for pattern in range(8):
            symbol.apply_mask(pattern)
            scores.append(symbol.penalty())
            symbol.apply_mask(pattern)
        assert len(set(scores)) > 1, "the masks all scored the same; is scoring live?"
        assert decode(qr_matrix(CONTROLLER_URL))[1] == scores.index(min(scores))


# -------------------------------------------------------------------- svg


def svg_modules(document, scale, quiet_zone):
    """Rebuild the module grid from the path in an SVG, in module coordinates."""
    root = ET.fromstring(document)
    path = root.find(f"{SVG}path")
    offset = quiet_zone * scale
    dark = set()
    for x, y, width in re.findall(r"M(\d+) (\d+)h(\d+)v", path.get("d")):
        x, y, width = int(x), int(y), int(width)
        assert (x - offset) % scale == 0 and (y - offset) % scale == 0
        assert width % scale == 0
        for step in range(width // scale):
            dark.add(((y - offset) // scale, (x - offset) // scale + step))
    return dark


class TestSvg:
    def test_the_document_is_well_formed_and_self_contained(self):
        document = qr_svg(CONTROLLER_URL)
        root = ET.fromstring(document)
        assert root.tag == f"{SVG}svg"
        assert root.find(f"{SVG}rect") is not None
        assert root.find(f"{SVG}path") is not None
        # It is served as an image and shown in an <img>: a stylesheet, a
        # script or an external image would never load.
        assert "<script" not in document and "<style" not in document
        assert "href" not in document and "url(" not in document

    @pytest.mark.parametrize(("scale", "quiet_zone"), [(1, 0), (3, 2), (4, 4), (10, 4)])
    def test_the_size_follows_the_scale_and_quiet_zone(self, scale, quiet_zone):
        matrix = qr_matrix(CONTROLLER_URL)
        extent = (len(matrix) + quiet_zone * 2) * scale
        root = ET.fromstring(qr_svg(CONTROLLER_URL, scale=scale, quiet_zone=quiet_zone))
        assert root.get("width") == str(extent)
        assert root.get("height") == str(extent)
        assert root.get("viewBox") == f"0 0 {extent} {extent}"

    def test_the_background_covers_the_whole_image(self):
        """Including the quiet zone: a dark wallpaper behind it would not scan."""
        root = ET.fromstring(qr_svg(CONTROLLER_URL, scale=4, quiet_zone=4, light="#ffffff"))
        rect = root.find(f"{SVG}rect")
        extent = (len(qr_matrix(CONTROLLER_URL)) + 8) * 4
        assert rect.get("fill") == "#ffffff"
        assert (rect.get("x"), rect.get("y")) == ("0", "0")
        assert (rect.get("width"), rect.get("height")) == (str(extent), str(extent))

    @pytest.mark.parametrize("data", SAMPLES)
    def test_the_drawn_modules_are_the_matrix(self, data):
        matrix = qr_matrix(data)
        drawn = svg_modules(qr_svg(data, scale=3, quiet_zone=4), scale=3, quiet_zone=4)
        expected = {
            (row, col)
            for row, modules in enumerate(matrix)
            for col, dark in enumerate(modules)
            if dark
        }
        assert drawn == expected

    def test_nothing_is_drawn_in_the_quiet_zone(self):
        scale, quiet = 4, 4
        size = len(qr_matrix(CONTROLLER_URL))
        root = ET.fromstring(qr_svg(CONTROLLER_URL, scale=scale, quiet_zone=quiet))
        for x, y, width in re.findall(r"M(\d+) (\d+)h(\d+)v", root.find(f"{SVG}path").get("d")):
            left, top, run = int(x), int(y), int(width)
            assert left >= quiet * scale and top >= quiet * scale
            assert left + run <= (quiet + size) * scale
            assert top + scale <= (quiet + size) * scale

    def test_colours_can_be_changed(self):
        root = ET.fromstring(qr_svg("hi", dark="#123456", light="white"))
        assert root.find(f"{SVG}rect").get("fill") == "white"
        assert root.find(f"{SVG}path").get("fill") == "#123456"

    @pytest.mark.parametrize(
        "colour", ['#000" onload="x', "rgb(0,0,0)", "", "#12345", "javascript:alert(1)"]
    )
    def test_colours_that_could_break_out_of_the_attribute_are_refused(self, colour):
        with pytest.raises(QRCodeError):
            qr_svg("hi", dark=colour)

    @pytest.mark.parametrize("kwargs", [{"scale": 0}, {"scale": -2}, {"quiet_zone": -1}])
    def test_impossible_geometry_is_refused(self, kwargs):
        with pytest.raises(QRCodeError):
            qr_svg("hi", **kwargs)

    def test_oversized_data_raises_from_the_svg_entry_point_too(self):
        with pytest.raises(DataTooLongError):
            qr_svg("x" * 500)


# ------------------------------------------------------------------ segno
#
# segno pads differently from this module. ISO/IEC 18004 7.4.10 adds padding
# bits only "if the bit stream length is such that it does not end at a
# codeword boundary"; in byte mode with a four-bit terminator it always does,
# so the first pad codeword should be 11101100. segno adds a whole byte of
# zeros first, which is harmless (a reader stops at the terminator) but shifts
# every following codeword, so the two matrices differ for any data that needs
# padding. This module follows the standard, and agrees with nayuki's reference
# implementation and python-qrcode. The comparison below therefore comes in two
# parts: symbols that need no padding at all are compared as they are, and for
# the rest the padding convention is swapped so the comparison is about
# everything else — Reed-Solomon, interleaving, placement, masking and the
# format and version bits.


def segno_matrix(data, level):
    code = segno.make(
        data, error=level, mode="byte", boost_error=False, micro=False, encoding="utf-8"
    )
    return [[bool(module) for module in row] for row in code.matrix], int(code.version)


def matrix_with_segno_padding(data, level):
    """This encoder's symbol, built with segno's padding convention."""
    payload = data.encode("utf-8")
    version = qr._choose_version(payload, level)
    capacity_bits = qr._data_codewords(version, level) * 8

    bits = qr._Bits()
    bits.append(qr._BYTE_MODE, 4)
    bits.append(len(payload), qr._character_count_bits(version))
    for byte in payload:
        bits.append(byte, 8)
    bits.append(0, min(4, capacity_bits - len(bits)))
    bits.append(0, 8 - len(bits) % 8)  # segno pads even when already aligned
    padding = (0xEC, 0x11)
    index = 0
    while len(bits) < capacity_bits:
        bits.append(padding[index % 2], 8)
        index += 1
    del bits.bits[capacity_bits:]

    message = qr._final_message(bits.to_bytes(), version, level)
    symbol = qr._Symbol(version)
    symbol.draw_function_patterns()
    symbol.draw_codewords(message)
    mask = symbol.choose_mask()
    symbol.apply_mask(mask)
    symbol.draw_format_info(level, mask)
    symbol.draw_version_info()
    return [[bool(module) for module in row] for row in symbol.modules]


@needs_segno
class TestAgainstSegno:
    @pytest.mark.parametrize("level", ["L", "M", "Q", "H"])
    @pytest.mark.parametrize("version", range(1, 11))
    def test_a_full_symbol_is_identical(self, version, level):
        """Data that fills the version exactly needs no padding at all.

        Nothing is accommodated here: every version and every level, compared
        module for module against segno.
        """
        data = "x" * qr._byte_capacity(version, level)
        theirs, their_version = segno_matrix(data, level)
        assert their_version == version
        assert qr_matrix(data, ec=level) == theirs

    @pytest.mark.parametrize("level", ["L", "M", "Q", "H"])
    @pytest.mark.parametrize("data", SAMPLES)
    def test_everything_but_the_padding_is_identical(self, data, level):
        theirs, _ = segno_matrix(data, level)
        assert matrix_with_segno_padding(data, level) == theirs

    @pytest.mark.parametrize("level", ["L", "M", "Q", "H"])
    def test_the_lengths_where_the_version_rolls_over(self, level):
        for version in range(1, 11):
            capacity = qr._byte_capacity(version, level)
            for length in (capacity - 1, capacity, capacity + 1):
                if not 0 <= length <= qr._byte_capacity(10, level):
                    continue
                data = "u" * length
                theirs, their_version = segno_matrix(data, level)
                assert len(qr_matrix(data, ec=level)) == len(theirs), length
                assert qr._choose_version(data.encode(), level) == their_version
                assert matrix_with_segno_padding(data, level) == theirs

    @pytest.mark.parametrize("data", SAMPLES)
    def test_segno_symbols_read_back_with_our_own_reader(self, data):
        """The reader above is not tuned to this encoder: it reads segno too."""
        theirs, _ = segno_matrix(data, "M")
        level, _, payload = decode(theirs)
        assert (level, payload) == ("M", data.encode())
