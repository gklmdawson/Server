"""NAS helper (coordinator.probe): EPSG H/V map, shapefile point-in-polygon,
ECEF csv parsing, and graceful degradation without assets."""
import struct

import pytest

from coordinator import probe


def test_vertical_pulled_from_table_not_defaulted():
    # Most zones pair with 6360; the AZ/MI/MT/ND/OR/SC group uses 8228. The
    # vertical must come from the same table as the horizontal.
    assert probe.STATEPLANE_HV["6341"] == "6360" if "6341" in probe.STATEPLANE_HV else True
    assert probe.STATEPLANE_HV["6627"] == "6360"   # Utah South
    assert probe.STATEPLANE_HV["6405"] == "8228"   # Arizona Central
    assert probe.STATEPLANE_HV["6559"] == "8228"   # Oregon North
    assert set(probe.STATEPLANE_HV.values()) == {"6360", "8228"}


def test_ray_cast_inside_and_outside():
    square = [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0), (0.0, 0.0)]
    assert probe._ray_cast(5.0, 5.0, square) is True
    assert probe._ray_cast(15.0, 5.0, square) is False


def _write_min_shapefile(dirpath, epsg="6627"):
    """A one-record polygon shapefile (unit square) + matching dbf."""
    ring = [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0), (0.0, 0.0)]
    n_pts = len(ring)
    body = struct.pack("<i", 5)                     # shape type: polygon
    body += struct.pack("<4d", 0, 0, 10, 10)        # bbox
    body += struct.pack("<ii", 1, n_pts)            # n_parts, n_points
    body += struct.pack("<i", 0)                     # parts[0]
    for x, y in ring:
        body += struct.pack("<2d", x, y)
    shp = dirpath / "zones.shp"
    with open(shp, "wb") as fh:
        fh.write(b"\x00" * 100)                      # header (ignored)
        fh.write(struct.pack(">ii", 1, len(body) // 2))
        fh.write(body)

    # Minimal dbf with EPSG + ZONENAME fields, one record.
    fields = [("EPSG", 10), ("ZONENAME", 20)]
    rec_sz = 1 + sum(l for _, l in fields)
    hdr_sz = 32 + 32 * len(fields) + 1
    dbf = dirpath / "zones.dbf"
    with open(dbf, "wb") as fh:
        fh.write(struct.pack("<B3B", 3, 25, 1, 1))
        fh.write(struct.pack("<IHH", 1, hdr_sz, rec_sz))
        fh.write(b"\x00" * 20)
        for name, length in fields:
            fh.write(name.encode("ascii").ljust(11, b"\x00"))
            fh.write(b"C")                            # type: character
            fh.write(b"\x00" * 4)
            fh.write(bytes([length]))
            fh.write(b"\x00" * 15)
        fh.write(b"\x0D")
        fh.write(b" ")                                # not-deleted flag
        fh.write(epsg.encode("ascii").ljust(10))
        fh.write(b"Test Zone".ljust(20))
    return str(shp)


def test_epsg_from_shapefile_returns_h_and_v(tmp_path):
    shp = _write_min_shapefile(tmp_path, epsg="6627")
    hv = probe.epsg_from_latlon(5.0, 5.0, shp)          # lat=5, lon=5 -> inside
    assert hv == ("6627", "6360")
    # Outside the ring -> no match.
    assert probe.epsg_from_latlon(50.0, 50.0, shp) is None


def test_epsg_missing_shapefile_degrades():
    assert probe.epsg_from_latlon(5.0, 5.0, "/nope/missing.shp") is None


def test_parse_base_ecef_csv(tmp_path):
    csv = tmp_path / "base.csv"
    csv.write_text("Point ID,X (ECEF),Y (ECEF),Z (ECEF)\nBASE1,-1878522.21,-4599428.34,4001432.17\n")
    assert probe.parse_base_ecef_csv(str(csv)) == (-1878522.21, -4599428.34, 4001432.17)


def test_parse_base_ecef_csv_bad_header(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text("a,b,c,d\n1,2,3,4\n")
    with pytest.raises(ValueError):
        probe.parse_base_ecef_csv(str(csv))


def test_probe_folder_no_images_defaults_sensor(tmp_path):
    (tmp_path / "notes.txt").write_text("no images here")
    result = probe.probe_folder(str(tmp_path))
    assert result["sensor"] == probe.DEFAULT_SENSOR_IF_NO_IMAGES
    assert result["image_count"] == 0


def test_probe_folder_not_a_dir():
    result = probe.probe_folder("/does/not/exist")
    assert "error" in result
