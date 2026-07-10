#!/usr/bin/env python3
"""
Embed DJI Terra PPK CSV output into JPEG EXIF and XMP metadata.

Updates:
  - EXIF GPS: GPSLatitude, GPSLongitude, GPSAltitude
  - XMP drone-dji: AbsoluteAltitude, GpsStatus, RtkStdLon, RtkStdLat, RtkStdHgt
"""

import argparse
import csv
import os
import re
import struct
import sys
from pathlib import Path

import piexif


# ---------------------------------------------------------------------------
# EXIF GPS helpers
# ---------------------------------------------------------------------------

def _decimal_to_dms_rational(decimal_degrees: float):
    """Convert decimal degrees to EXIF DMS rational tuple."""
    d = abs(decimal_degrees)
    degrees = int(d)
    minutes = int((d - degrees) * 60)
    seconds = (d - degrees - minutes / 60) * 3600
    return (
        (degrees, 1),
        (minutes, 1),
        (round(seconds * 1_000_000), 1_000_000),
    )


def _build_gps_ifd(lat: float, lon: float, alt: float) -> dict:
    return {
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: _decimal_to_dms_rational(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: _decimal_to_dms_rational(lon),
        piexif.GPSIFD.GPSAltitudeRef: 0,  # above sea level
        piexif.GPSIFD.GPSAltitude: (round(abs(alt) * 100), 100),
    }


# ---------------------------------------------------------------------------
# XMP helpers
# ---------------------------------------------------------------------------

XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
XMP_HEADER_EXT = b"http://ns.adobe.com/xap/1.0/ext/\x00"

# Attributes in the drone-dji namespace that we may write/update
_DJI_ATTR_RE = re.compile(
    r'(drone-dji:(AbsoluteAltitude|AltitudeType|GpsStatus|RtkFlag|RtkStdLon|RtkStdLat|RtkStdHgt))'
    r'\s*=\s*"[^"]*"'
)

_DJI_NS_DECL = 'xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/"'

# Regex to find the rdf:Description element that carries drone-dji attributes
_RDF_DESC_RE = re.compile(
    r'(<rdf:Description\b[^>]*?)(/>|>)',
    re.DOTALL,
)


def _format_signed(value: float, decimals: int = 2) -> str:
    """Format a float with explicit sign, e.g. +509.43 or -0.03."""
    fmt = f"{{:+.{decimals}f}}"
    return fmt.format(value)


def _update_xmp_attrs(xmp_bytes: bytes, attrs: dict) -> bytes:
    """
    Update or insert drone-dji attributes in an XMP packet.

    attrs: dict of attribute-local-name → value string
           e.g. {"AbsoluteAltitude": "+509.43", "GpsStatus": "RTKFix"}
    """
    try:
        xmp_str = xmp_bytes.decode("utf-8")
    except UnicodeDecodeError:
        xmp_str = xmp_bytes.decode("latin-1")

    # Replace existing drone-dji attributes first
    def replace_attr(m):
        name = m.group(2)
        if name in attrs:
            return f'drone-dji:{name}="{attrs[name]}"'
        return m.group(0)

    new_xmp, replaced_count = _DJI_ATTR_RE.subn(replace_attr, xmp_str)
    replaced_names = set(m.group(2) for m in _DJI_ATTR_RE.finditer(xmp_str))

    # Attributes that weren't already present need to be injected
    missing = {k: v for k, v in attrs.items() if k not in replaced_names}
    if missing:
        new_attrs_str = " ".join(
            f'drone-dji:{k}="{v}"' for k, v in missing.items()
        )

        def inject(m):
            tag_open = m.group(1)
            closing = m.group(2)
            # Make sure namespace is declared
            ns = ""
            if _DJI_NS_DECL not in tag_open:
                ns = f" {_DJI_NS_DECL}"
            return f"{tag_open}{ns} {new_attrs_str}{closing}"

        new_xmp, n = _RDF_DESC_RE.subn(inject, new_xmp, count=1)
        if n == 0:
            # No rdf:Description found — append a minimal block before xmpmeta close
            block = (
                f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                f'<rdf:Description rdf:about="" {_DJI_NS_DECL} {new_attrs_str}/>'
                f'</rdf:RDF>'
            )
            new_xmp = new_xmp.replace("</x:xmpmeta>", f"{block}</x:xmpmeta>")

    return new_xmp.encode("utf-8")


# ---------------------------------------------------------------------------
# JPEG segment I/O
# ---------------------------------------------------------------------------

def _read_jpeg_segments(data: bytes) -> list:
    """
    Parse JPEG into a list of (marker, payload_bytes) tuples.
    Marker 0xFFD8 (SOI) and 0xFFD9 (EOI) have empty payload.
    """
    segments = []
    i = 0
    if data[i:i+2] != b"\xff\xd8":
        raise ValueError("Not a JPEG file")
    segments.append((0xFFD8, b""))
    i = 2

    while i < len(data):
        if data[i] != 0xFF:
            raise ValueError(f"Expected 0xFF at offset {i}, got {data[i]:02x}")
        marker = struct.unpack(">H", data[i:i+2])[0]
        i += 2
        if marker in (0xFFD9, 0xFFD8):
            segments.append((marker, b""))
            if marker == 0xFFD9:
                break
            continue
        if marker == 0xFFDA:  # SOS — rest is compressed data
            segments.append((marker, data[i:]))
            break
        length = struct.unpack(">H", data[i:i+2])[0]
        payload = data[i + 2: i + length]
        segments.append((marker, payload))
        i += length

    return segments


def _segments_to_bytes(segments: list) -> bytes:
    out = []
    for marker, payload in segments:
        out.append(struct.pack(">H", marker))
        if marker in (0xFFD8, 0xFFD9):
            continue
        if marker == 0xFFDA:
            out.append(payload)
        else:
            out.append(struct.pack(">H", len(payload) + 2))
            out.append(payload)
    return b"".join(out)


def _find_xmp_segment(segments: list) -> int:
    """Return index of the XMP APP1 segment, or -1."""
    for idx, (marker, payload) in enumerate(segments):
        if marker == 0xFFE1 and payload.startswith(XMP_HEADER):
            return idx
    return -1


def _find_exif_segment(segments: list) -> int:
    """Return index of the Exif APP1 segment, or -1."""
    for idx, (marker, payload) in enumerate(segments):
        if marker == 0xFFE1 and payload.startswith(b"Exif\x00\x00"):
            return idx
    return -1


# ---------------------------------------------------------------------------
# Main embed function
# ---------------------------------------------------------------------------

def embed_metadata(
    image_path: str,
    lat: float,
    lon: float,
    alt: float,
    h_acc: float,
    v_acc: float,
    gps_status: str = "RTK",
    overwrite: bool = True,
    output_path: str | None = None,
) -> None:
    """
    Embed PPK GPS + RTK metadata into a JPEG file.

    Parameters
    ----------
    image_path   : path to the source JPEG
    lat          : latitude in decimal degrees (WGS-84)
    lon          : longitude in decimal degrees (WGS-84)
    alt          : ellipsoidal altitude in metres
    h_acc        : horizontal accuracy (m), written to RtkStdLon and RtkStdLat
    v_acc        : vertical accuracy (m), written to RtkStdHgt
    gps_status   : XMP GpsStatus value (default "RTKFix")
    overwrite    : replace source file when output_path is None
    output_path  : if given, write to this path instead
    """
    with open(image_path, "rb") as fh:
        data = fh.read()

    segments = _read_jpeg_segments(data)

    # --- EXIF GPS ---
    exif_idx = _find_exif_segment(segments)
    if exif_idx >= 0:
        try:
            exif_dict = piexif.load(segments[exif_idx][1])
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    else:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    exif_dict["GPS"] = _build_gps_ifd(lat, lon, alt)

    new_exif_bytes = piexif.dump(exif_dict)
    # piexif.dump includes the "Exif\x00\x00" header already
    if exif_idx >= 0:
        segments[exif_idx] = (0xFFE1, new_exif_bytes)
    else:
        # Insert after SOI
        segments.insert(1, (0xFFE1, new_exif_bytes))
        exif_idx = 1  # keep offsets consistent

    # --- XMP drone-dji ---
    xmp_attrs = {
        "AbsoluteAltitude": _format_signed(alt, 2),
        "AltitudeType": "RtkAlt",
        "GpsStatus": gps_status,
        "RtkFlag": "50",
        "RtkStdLon": f"{h_acc:.3f}",
        "RtkStdLat": f"{h_acc:.3f}",
        "RtkStdHgt": f"{v_acc:.3f}",
    }

    xmp_idx = _find_xmp_segment(segments)
    if xmp_idx >= 0:
        xmp_payload = segments[xmp_idx][1]
        raw_xmp = xmp_payload[len(XMP_HEADER):]
        new_raw_xmp = _update_xmp_attrs(raw_xmp, xmp_attrs)
        segments[xmp_idx] = (0xFFE1, XMP_HEADER + new_raw_xmp)
    else:
        # Build a minimal XMP packet
        attr_str = " ".join(
            f'drone-dji:{k}="{v}"' for k, v in xmp_attrs.items()
        )
        minimal_xmp = (
            '<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            f'<rdf:Description rdf:about="" {_DJI_NS_DECL} {attr_str}/>'
            '</rdf:RDF>'
            '</x:xmpmeta>'
            '<?xpacket end="w"?>'
        ).encode("utf-8")
        # Insert XMP segment right after EXIF (or after SOI if no EXIF)
        insert_pos = (exif_idx + 1) if exif_idx >= 0 else 1
        segments.insert(insert_pos, (0xFFE1, XMP_HEADER + minimal_xmp))

    dest = output_path if output_path else (image_path if overwrite else image_path)
    with open(dest, "wb") as fh:
        fh.write(_segments_to_bytes(segments))


# ---------------------------------------------------------------------------
# CSV processing
# ---------------------------------------------------------------------------

def resolve_image_path(csv_photo_name: str, search_dir: str | None) -> str | None:
    """
    Resolve a potentially Windows-style CSV path to a real file.

    Strategy:
    1. Try the path as-is (works on Linux if the drive is mounted).
    2. Try just the filename in search_dir.
    3. Try the relative sub-path (everything after the drive letter) in search_dir.
    """
    # As-is
    if os.path.isfile(csv_photo_name):
        return csv_photo_name

    filename = os.path.basename(csv_photo_name.replace("\\", "/"))

    if search_dir:
        # Flat lookup
        candidate = os.path.join(search_dir, filename)
        if os.path.isfile(candidate):
            return candidate

        # Walk subdirectories
        for root, _dirs, files in os.walk(search_dir):
            if filename in files:
                return os.path.join(root, filename)

    return None


def process_csv(
    csv_path: str,
    image_dir: str | None = None,
    output_dir: str | None = None,
    gps_status: str = "RTK",
    dry_run: bool = False,
) -> None:
    """
    Read a DJI Terra PPK CSV and embed metadata into each referenced JPEG.

    Parameters
    ----------
    csv_path   : path to the Terra PPK CSV
    image_dir  : directory to search for images (overrides CSV paths)
    output_dir : write updated images here instead of overwriting originals
    gps_status : value written to drone-dji:GpsStatus
    dry_run    : print actions without modifying files
    """
    if output_dir and not dry_run:
        os.makedirs(output_dir, exist_ok=True)

    ok = skipped = errors = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    total = len(rows)
    print(f"Processing {total} entries from {csv_path}")

    for row in rows:
        photo_name = row["Photo Name"].strip()
        try:
            lat = float(row["Latitude"])
            lon = float(row["Longitude"])
            alt = float(row["Altitude"])
            h_acc = float(row["Horizontal Accuracy"])
            v_acc = float(row["Vertical Accuracy"])
        except (KeyError, ValueError) as exc:
            print(f"  [SKIP] Bad data for {photo_name}: {exc}")
            skipped += 1
            continue

        image_path = resolve_image_path(photo_name, image_dir)
        if image_path is None:
            print(f"  [MISS] Not found: {photo_name}")
            skipped += 1
            continue

        if output_dir:
            out_path = os.path.join(output_dir, os.path.basename(image_path))
        else:
            out_path = None  # overwrite in place

        if dry_run:
            print(
                f"  [DRY]  {os.path.basename(image_path)} → "
                f"lat={lat:.7f} lon={lon:.7f} alt={alt:.3f}m "
                f"h_acc={h_acc} v_acc={v_acc}"
            )
            ok += 1
            continue

        try:
            embed_metadata(
                image_path=image_path,
                lat=lat,
                lon=lon,
                alt=alt,
                h_acc=h_acc,
                v_acc=v_acc,
                gps_status=gps_status,
                output_path=out_path,
            )
            dest = out_path or image_path
            print(f"  [OK]   {os.path.basename(image_path)} → {dest}")
            ok += 1
        except Exception as exc:
            print(f"  [ERR]  {os.path.basename(image_path)}: {exc}")
            errors += 1

    print(f"\nDone: {ok} updated, {skipped} skipped, {errors} errors  (total {total})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Embed DJI Terra PPK CSV metadata into JPEG images."
    )
    parser.add_argument("csv", help="Path to the Terra PPK CSV file")
    parser.add_argument(
        "-i", "--image-dir",
        help="Directory containing the images (searched recursively). "
             "Required when CSV paths are Windows-style or the files have been moved.",
    )
    parser.add_argument(
        "-o", "--output-dir",
        help="Write updated images to this directory instead of overwriting originals.",
    )
    parser.add_argument(
        "--gps-status",
        default="RTKFix",
        help="Value written to drone-dji:GpsStatus (default: RTKFix)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without modifying any files.",
    )
    args = parser.parse_args()

    process_csv(
        csv_path=args.csv,
        image_dir=args.image_dir,
        output_dir=args.output_dir,
        gps_status=args.gps_status,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
