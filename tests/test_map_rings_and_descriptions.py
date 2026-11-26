"""GeoJSON rings stay closed with one closing position, and route and line descriptions survive (#2)."""
import csv
import io
import json
import re

from latheport.converters.maps import (
    Line,
    Map,
    Point,
    read_geojson,
    read_gpx,
    write_csv,
    write_geojson,
    write_gpx,
    write_kml,
)

SQUARE = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
TRIANGLE = [[5.0, 5.0], [6.0, 5.0], [5.0, 6.0], [5.0, 5.0]]


def _collection(geometry: dict) -> bytes:
    feature = {"type": "Feature", "properties": {"name": "lot"}, "geometry": geometry}
    return json.dumps({"type": "FeatureCollection", "features": [feature]}).encode()


def _rings(data: bytes) -> list[list]:
    geometry = json.loads(data)["features"][0]["geometry"]
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    return [ring for polygon in polygons for ring in polygon]


def test_a_polygon_keeps_four_positions_over_three_geojson_passes():
    data = _collection({"type": "Polygon", "coordinates": [SQUARE]})
    for _ in range(3):
        data = write_geojson(read_geojson(data))
        assert [len(ring) for ring in _rings(data)] == [4]
    assert _rings(data)[0] == SQUARE


def test_a_two_ring_multipolygon_keeps_four_positions_per_ring_over_three_passes():
    data = _collection({"type": "MultiPolygon", "coordinates": [[SQUARE], [TRIANGLE]]})
    for _ in range(3):
        data = write_geojson(read_geojson(data))
        assert [len(ring) for ring in _rings(data)] == [4, 4]


def test_an_open_ring_in_the_model_comes_out_closed_once():
    corners = [Point(0.0, 0.0), Point(0.0, 1.0), Point(1.0, 1.0)]
    m = Map(lines=[Line("lot", "area", [corners])])
    ring = _rings(write_geojson(m))[0]
    assert ring == [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
    kml_ring = re.search(r"<coordinates>(.*?)</coordinates>", write_kml(m).decode(), re.DOTALL).group(1).split()
    assert kml_ring == ["0.0000000,0.0000000", "1.0000000,0.0000000", "1.0000000,1.0000000", "0.0000000,0.0000000"]


def test_a_ring_whose_ends_differ_only_in_name_or_time_is_already_closed():
    corners = [Point(0.0, 0.0, None, "t0", "start"), Point(0.0, 1.0), Point(1.0, 1.0), Point(0.0, 0.0, None, "t3")]
    assert len(_rings(write_geojson(Map(lines=[Line("lot", "area", [corners])])))[0]) == 4


def test_a_route_description_survives_gpx_to_gpx():
    gpx = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
 <rte><name>Ridge</name><desc>Take water</desc>
  <rtept lat="1.0" lon="2.0"/><rtept lat="1.1" lon="2.1"/>
 </rte>
</gpx>"""
    once = write_gpx(read_gpx(gpx))
    assert read_gpx(once).lines[0].desc == "Take water"
    text = once.decode()
    assert text.index("<name>Ridge</name>") < text.index("<desc>Take water</desc>") < text.index("<rtept")


def test_csv_rows_of_a_track_and_a_route_carry_their_descriptions():
    m = Map(lines=[Line("Loop", "track", [[Point(1.0, 2.0), Point(1.1, 2.1)]], "Muddy after rain"),
                   Line("Ridge", "route", [[Point(3.0, 4.0)]], "Take water")])
    rows = list(csv.DictReader(io.StringIO(write_csv(m).decode())))
    assert [(r["type"], r["description"]) for r in rows] == [
        ("track", "Muddy after rain"), ("track", "Muddy after rain"), ("route", "Take water")]
