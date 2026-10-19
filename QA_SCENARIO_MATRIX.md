# QA Scenario Matrix

## Variables

### Regions
We will set up four logical operating regions as described:
- West Delhi (Clustered and sparse)
- North Delhi (Clustered)
- South Delhi (Sparse)
- East Delhi (Clustered)

### Threshold Boundaries (Accumulation)
We will test these boundaries implicitly by batching requests:
- 20 bookings
- 40 bookings
- 60 bookings

### Planning Horizons
We will set the `horizon_seconds` on the `FleetPolicy` mapping to:
- 7200 (2 hours)
- 10800 (3 hours)
- 14400 (4 hours)

### Fleet Vehicles
- Iteration sizes: 10, 15, 20, 25 vehicles per region.

## Objective

1. Drive requests into each region.
2. Wait for the respective threshold.
3. Assert that a run happens successfully via `run_region` and the assignment process activates.
4. Verify invariants:
   - No silent loss of commitments.
   - Genuine optimized route proposals are captured.
