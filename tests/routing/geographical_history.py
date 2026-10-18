"""Independent set audit of synthetic campaign relational certificate history.

Input query (one consistent PostgreSQL statement, no application projection):
SELECT json_build_object(
 'reservations',(SELECT json_agg(json_build_array(reservation_id,region_version))
                 FROM regional_capacity_reservation),
 'members',(SELECT json_agg(json_build_array(certificate_id,from_revision,to_revision))
            FROM admission_route_membership),
 'links',(SELECT json_agg(json_build_array(p.certificate_id,o.reservation_id))
          FROM admission_proof_obligation p JOIN admission_obligation o USING(obligation_id)))

Assumption: one synthetic region, no expired/cancelled/handed-over campaign obligations.
This complements full route validation and never creates or modifies a certificate.
"""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path


def audit(data):
    links = defaultdict(set)
    for certificate, reservation in data["links"]:
        if reservation is not None:
            links[certificate].add(reservation)
    expected, revisions = set(), []
    for identity, revision in sorted(data["reservations"], key=lambda row: row[1]):
        expected.add(identity)
        active = {
            certificate
            for certificate, start, end in data["members"]
            if start <= revision and (end is None or end > revision)
        }
        represented = set().union(*(links[c] for c in active))
        assert represented == expected, dict(
            revision=revision,
            missing=len(expected - represented),
            unexpected=len(represented - expected),
        )
        revisions.append(dict(revision=revision, required=len(expected), covered=len(represented)))
    assert revisions, "No actual certificates to verify"
    return dict(
        reservations=len(data["reservations"]),
        memberships=len(data["members"]),
        proof_obligation_links=len(data["links"]),
        certificates=len(links),
        checked_revisions=len(revisions),
        checked_obligation_pairs=sum(r["required"] for r in revisions),
        missing_pairs=0,
        unexpected_pairs=0,
        revisions=revisions,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    result = audit(json.loads(raw.decode("utf-8-sig")))
    result["source_json_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "revisions"}, sort_keys=True))
