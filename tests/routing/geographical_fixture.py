"""QA-only deterministic public OSM PBF reader and road-site sampler.

Reads the standard protobuf wire format without introducing a runtime dependency.
No application/customer data or estimated travel distances enter this fixture.
"""

import argparse
import hashlib
import json
import random
import struct
import zlib
from pathlib import Path

DATASET_SHA256 = "4f36f7e749aef6f21128288ab8ca6447666a506a440d74db778a079974a71b49"
SEED = 20261018


def varints(data):
    value = shift = 0
    for byte in data:
        value |= (byte & 127) << shift
        if byte < 128:
            yield value
            value = shift = 0
        else:
            shift += 7
    if shift:
        raise ValueError("truncated protobuf varint")


def fields(data):
    offset = 0
    while offset < len(data):

        def integer():
            nonlocal offset
            start = offset
            while data[offset] >= 128:
                offset += 1
            offset += 1
            return next(varints(data[start:offset]))

        key = integer()
        wire = key & 7
        if wire == 0:
            value = integer()
        elif wire == 2:
            size = integer()
            value = data[offset : offset + size]
            offset += size
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield key >> 3, value


def zigzag(value):
    return (value >> 1) ^ -(value & 1)


def packed(message, field):
    return [n for key, value in fields(message) if key == field for n in varints(value)]


def read_map(path):
    assert hashlib.sha256(path.read_bytes()).hexdigest() == DATASET_SHA256
    nodes, ways = {}, []
    with path.open("rb") as stream:
        while size := stream.read(4):
            header = dict(fields(stream.read(struct.unpack(">I", size)[0])))
            blob = dict(fields(stream.read(header[3])))
            if header[1] != b"OSMData":
                continue
            raw = blob.get(1) or zlib.decompress(blob[3])
            block = list(fields(raw))
            strings = [v.decode() for k, v in fields(next(v for k, v in block if k == 1))]
            settings = {k: v for k, v in block if k in (17, 19, 20)}
            granularity = settings.get(17, 100)
            lat_offset, lon_offset = settings.get(19, 0), settings.get(20, 0)
            for _, group in ((k, v) for k, v in block if k == 2):
                for kind, message in fields(group):
                    if kind == 2:  # DenseNodes: delta-coded sint64 vectors
                        identity = lat = lon = 0
                        for i, a, b in zip(
                            packed(message, 1), packed(message, 8), packed(message, 9), strict=True
                        ):
                            identity += zigzag(i)
                            lat += zigzag(a)
                            lon += zigzag(b)
                            nodes[identity] = (
                                (lon_offset + granularity * lon) * 1e-9,
                                (lat_offset + granularity * lat) * 1e-9,
                            )
                    elif kind == 1:
                        row = dict(fields(message))
                        nodes[zigzag(row[1])] = (
                            (lon_offset + granularity * zigzag(row[9])) * 1e-9,
                            (lat_offset + granularity * zigzag(row[8])) * 1e-9,
                        )
                    elif kind == 3:
                        tags = {
                            strings[k]: strings[v]
                            for k, v in zip(packed(message, 2), packed(message, 3), strict=True)
                        }
                        refs, identity = [], 0
                        for value in packed(message, 8):
                            identity += zigzag(value)
                            refs.append(identity)
                        ways.append((dict(fields(message))[1], tags, refs))
    return nodes, ways


def candidates(path):
    nodes, ways = read_map(path)
    eligible = {}
    roads = {
        "residential",
        "service",
        "tertiary",
        "secondary",
        "primary",
        "living_street",
        "unclassified",
        "primary_link",
        "secondary_link",
        "tertiary_link",
    }
    for way, tags, refs in ways:
        if tags.get("highway") not in roads or tags.get("access") in ("private", "no"):
            continue
        for identity in refs:
            if identity not in nodes:
                continue
            lon, lat = nodes[identity]
            if not (77.20 < lon < 77.22 and 28.62 < lat < 28.64):
                continue
            row = eligible.setdefault(
                identity,
                dict(
                    osm_node_id=identity,
                    longitude=round(lon, 7),
                    latitude=round(lat, 7),
                    ways=[],
                    one_way=False,
                    bridge=False,
                ),
            )
            row["ways"].append(way)
            row["one_way"] |= tags.get("oneway") in ("yes", "1", "-1")
            row["bridge"] |= tags.get("bridge") not in (None, "no")
    return sorted(eligible.values(), key=lambda r: r["osm_node_id"]), ways


def sample(rows, count, mode="spread"):
    """Farthest-first coverage or seeded clustered/shared access, never routing."""
    assert len(rows) >= count
    remaining = list(rows)
    if mode == "mixed":
        rng = random.Random(SEED)
        cluster = sorted(
            rows, key=lambda r: (r["longitude"] - 77.209) ** 2 + (r["latitude"] - 28.632) ** 2
        )[: count // 2]
        others = [r for r in rows if r not in cluster]
        rng.shuffle(others)
        selected = cluster + sorted(others, key=lambda r: not r["one_way"])[: count // 2]
        return selected
    selected = [remaining.pop(0)]
    distances = [float("inf")] * len(remaining)
    while len(selected) < count:
        last = selected[-1]
        distances = [
            min(
                old,
                ((r["longitude"] - last["longitude"]) * 0.88) ** 2
                + (r["latitude"] - last["latitude"]) ** 2,
            )
            for r, old in zip(remaining, distances, strict=True)
        ]
        position = max(range(len(remaining)), key=lambda i: distances[i])
        selected.append(remaining.pop(position))
        distances.pop(position)
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = Path(__file__).with_name("data") / "delhi-road.osm.pbf"
    rows, ways = candidates(path)
    args.output.write_text(
        json.dumps(
            dict(
                seed=SEED,
                dataset_sha256=DATASET_SHA256,
                candidates=rows,
                topology=dict(
                    ways=len(ways),
                    tagged_bridges=sum(t.get("bridge") not in (None, "no") for _, t, _ in ways),
                    tagged_restrictions=sum(
                        t.get("access") in ("private", "no") for _, t, _ in ways
                    ),
                ),
            ),
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
