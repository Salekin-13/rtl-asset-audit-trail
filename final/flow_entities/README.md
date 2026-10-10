# The flow graph per entity (new input version, 2026-10-10)

**Short version.** The FLOW GRAPH section of the traced inputs (the part a program computes from the map, shown to
the model after the map) mixed up elements that have the same name in different entities of one file. It kept only
one of them. `final/flow_entities.py` computes the flow graph for each entity separately. In the stored inputs this
removes every "record whose partner record is missing" that the MAP CHECK line reported: all of them were false
alarms. It also corrects some paths. Nothing earlier is overwritten.

Words used here:
- **entity**: one VHDL design unit. A file can declare several: `neorv32_cache.vhd` declares `neorv32_cache` and
  `neorv32_cache_memory`, and each has its own `clk_i`.
- **flow graph**: the program-computed summary after the map. It lists the clocks and resets, the paths a value
  takes from an input to stored elements and outputs ("stores", "exits"), the elements that steer those paths
  ("influence"), and a MAP CHECK line.
- **MAP CHECK**: counts of records, and any record whose mirror record is missing (each link is written from both
  ends, `clk_i SEQUENCES ctrl` and `ctrl CLOCKED_BY clk_i`).

## What was wrong

`assetgen_meta/traced_inputs.flow_graph` (line 200) stores the map's elements in a table keyed by name only. When two
entities of one file share a name, the second one overwrites the first. Then:

- **False alarms.** The dropped element's records are never read, so its links look one-sided. Example:
  `neorv32_cache.vhd` 125-126 (`elsif rising_edge(clk_i) then` / `ctrl <= ctrl_nxt;`, entity `neorv32_cache`). The
  table kept the `clk_i` of `neorv32_cache_memory`, so "ctrl CLOCKED_BY clk_i" was listed as missing its partner.
- **Wrong paths.** Lookups by name reach the other entity's element. Examples, each checked against the RTL:
  - `neorv32_bus.vhd` 55 and 77 (`state <= state_nxt;`, `state_nxt <= state;`, entity `neorv32_bus_switch`): the
    table held another entity's `state`, missed line 77, and showed `state_nxt` as a constant-valued source with its
    own path. It is not one.
  - `neorv32_bus.vhd` 734-740: the record `arbiter` exists only in `neorv32_bus_amo_rmw`. Its field `arbiter.state`
    was listed as an influence on paths of `neorv32_bus_amo_rvs`, because both entities have fields named
    `core_rsp_o.*` and `sys_req_o.*`.
  - `neorv32_pwm.vhd` 95-103: the top entity wires its input `clkgen_i` into its channel sub-unit. The table kept the
    channel's own `clkgen_i`, so this path was missing.

7 of the 41 scored modules declare more than one entity, and all 7 have shared names: `neorv32_bus` (6 entities),
`neorv32_cache`, `neorv32_sys`, `neorv32_trng` (tuning), `neorv32_cpu_counters`, `neorv32_cpu_cp_fpu`, `neorv32_pwm`
(held-out). The other 34 have one entity and were never affected.

## The fix

No relationship record in the stored maps points to an element of another entity (0 such records). So the flow graph
of a file is exactly the flow graph of each entity's own part of the map. The fix runs the unchanged
`traced_inputs.flow_graph` once per entity.

- A file with one entity: nothing changes, byte for byte.
- A file with several entities: every FLOW GRAPH line carries an `"entity"` key, as the map's element lines already do.
  Names stay bare, as the final prompt asks (line 141). The flow JSON becomes
  `{"entities": {<entity>: <the usual flow graph>}}`.
- Everything outside the FLOW GRAPH section is the stored input, byte for byte.

Two versions are written:
- `stored/`: the maps every earlier run read. It differs from `assetgen_meta/traced_inputs_v2` only in the flow graph.
- `record_fields_all/`: the record-field maps of `final/record_fields/all` (2026-10-09), so both fixes together.

## Run

```bash
%USERPROFILE%\miniconda3\python.exe final/flow_entities.py
```

The self-tests run first; nothing is written if one fails. Run `final/record_fields.py` first, or the
`record_fields_all` version is skipped. The rebuilt `<version>/tuning/` and `<version>/heldout/` folders are not
committed (`.gitignore`); `measure.json` is.

## Self-tests (all passed on 2026-10-10)

- The stored inputs are what `traced_inputs.text` gives here: 44 of 44 files.
- The new line writer, without an entity key, gives the stored lines for every flow graph it is given: 99 of 99 (the
  whole-file graphs and every entity's graph).
- Hand-read: cache 125-126 and 396-398 (each entity's `clk_i` is a clock of its own entity; `ctrl CLOCKED_BY clk_i`
  and `tag_mem CLOCKED_BY clk_i` are matched); cache 412-425 (`wdata_i` reaches the stored `data_mem_b0` and the
  output `rdata_o`); pwm 95-103; bus 55 and 77; bus 734-740 (the three examples above, each also checked to be wrong
  in the stored graph).
- Single-entity modules byte for byte, text and flow JSON: 36 of 36 for `stored`, 34 of 34 for `record_fields_all`.
  Longest line 1,496 characters (limit 1,500).

## What changed (exact counts)

"Missing partner" records the MAP CHECK line reported, and how many remain:

| module | entities | stored: driving / receiving | after the fix |
|---|---|---|---|
| neorv32_bus | 6 | 42 / 59 | 0 |
| neorv32_cache | 2 | 0 / 9 | 0 |
| neorv32_sys | 2 | 0 / 6 | 0 |
| neorv32_trng | 3 | 0 / 18 | 0 |
| neorv32_cpu_counters | 2 | 4 / 4 | 0 |
| neorv32_cpu_cp_fpu | 3 | 1 / 84 | 0 |
| neorv32_pwm | 2 | 2 / 4 | 0 |

Totals: tuning 42 + 92 of 42 + 92 were false; held-out 7 + 92 of 7 + 92 were false. None were real. With the
record-field maps the stored counts are 42 + 134 and 7 + 95, also all false, also 0 after the fix.

Paths (stored maps; a path counts when it reaches a stored element, an output or a sub-unit):
- Tuning, 15 modules: 110 paths before, 129 after. 20 added (their source had been dropped), 1 removed
  (`state_nxt` above), 9 with a changed influence list (5 entries added, 4 removed), 100 unchanged. No path's
  "stores", "exits" or "sub-block" list changed.
- Held-out, 26 modules: 175 before, 181 after. 6 added, 3 with a changed influence list (1 added, 10 removed), 172
  unchanged.
- Clock or reset lists gain the elements of the dropped entities: 15 (tuning), 8 (held-out).
- `neorv32_cpu_cp_fpu`: 8 more elements in "no local use" and 9 more control-only sources. The 8 are fields of the
  normalizer entity's `ctrl` and `sreg` that its RTL never declares (`ctrl.over`, `sreg.mant`, ...). That is a
  separate closed-set bug: the file declares two different record types named `ctrl_t` (lines 1577 and 2015) and two
  named `sreg_t` (1594 and 2031), and the parser gives both entities the second one. The old flow graph hid it. It is
  flagged as its own task, not fixed here.

The full lists per module: `stored/measure.json` and `record_fields_all/measure.json` (`rows`).

## What it breaks, and what needs a labelled re-run

- **What the model reads** changes for 7 of 41 modules: 4 of 15 tuning (+9,539 characters, 1.0%) and 3 of 26
  held-out (+6,235 characters, 0.3%). Together with the record-field fix: 10 of 15 tuning (+9.8%) and 12 of 26
  held-out (+1.9%).
- **The flow JSON has a new shape** for those 7 modules. Code that reads the old shape must not be pointed at these
  folders without an update: `final/module_insights.py` (line 354 reads `no_local_use` and `control_only_inputs`),
  `assetgen_meta/trace_digest.py`, `ist_levers.py`, `hand_arms/counterfactuals_ist.py`.
- **Trace-check results do not move.** `trace_check` reads only the map section and the numbered RTL; it stops at the
  FLOW GRAPH heading (`trace_check.py` line 89). So every citation status of every earlier run is unchanged.
- **Module portraits** (`final/module_insights.py`) counted "unused elements" and "control-only sources" from the
  stored flow graph. For `neorv32_bus` the control-only count goes 12 -> 13, for `neorv32_cpu_cp_fpu` 14 -> 23 and
  unused 37 -> 45. The other five modules keep both counts.
- **Asset precision and recall** of every run that read the traced inputs (`ist2`, the final prompt on gpt-5.4, the
  Claude-executed loop) stay correct for the inputs they read. In the final gpt-5.4 runs, 203 of 725 tuning listings
  and 212 of 1,472 held-out listings (3 runs each) come from the 7 changed modules. Whether the fix changes the asset
  lists needs a new, labelled run.
- **Held-out:** the pre-registrations pin `assetgen_meta/traced_inputs_v2/heldout` by hash. A new held-out reading
  needs its own pre-registration first.
