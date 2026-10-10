# Record fields in the relationship map (new map version, 2026-10-09)

**Short version.** The code-built relationship map missed one VHDL fact: when you assign a whole record, you assign
every field in it. So `ctrl <= ctrl_nxt;` on the clock edge (`data/RTL_data/neorv32_cache.vhd:126`) told the map
nothing about `ctrl.state`, `ctrl.tag` or the other six fields. The map showed them with storage "none", no
CLOCKED_BY record and no COPIES record. `final/record_fields.py` builds a **new** map version that adds this fact.
The stored maps, the traced inputs that earlier runs read, and every pinned file are unchanged
(`python src/verify_layout_move.py`: 40 rows, 0 failures, run 2026-10-09).

Words used here:
- **record**: a box with named compartments. `ctrl` is the box. `ctrl.state` is one compartment, called a **field**.
- **element**: one port, signal or field in the map.
- **occurrence**: one place in the RTL where the map says an element appears. It has a number (occurrence ID) and a
  line number.
- **relationship record**: a typed link between two elements at an occurrence. `CLOCKED_BY clk_i` means "this element
  is loaded on the edge of clk_i". `COPIES x` means "this element takes x's value unchanged". Each link is written
  from both ends (`x CARRIES y` and `y COPIES x`).
- **storage "edge"**: the element keeps its value between clock edges (a register). "none": it is assigned, but not
  on a clock edge. "not assigned": nothing assigns it.

## Why it happened

Three steps work together. Each one does what it was written to do.

1. **Occurrence profile.** A field gets an occurrence only where its own name is written, plus once on the line that
   declares its record (`occurrence_lines` in `build_occurrence_notebook_v3b.py`). Line 126 writes only `ctrl` and
   `ctrl_nxt`. So `ctrl.state` has no occurrence there.
2. **Pair finder** (`step1/code_pairs_v2.py`). A written name is matched to the longest declared element
   (`_CPWriter._resolve`). `ctrl` is itself an element, so the links of line 126 go to `ctrl` and `ctrl_nxt` only.
   The record element `ctrl` was always right: storage "edge", CLOCKED_BY clk_i, COPIES ctrl_nxt.
3. **Merge** (`step1/relation_stage.merge`). Storage is read from the element's own assignment occurrences.
   `ctrl.state` has only one, the reset at line 117, and a reset does not count for storage. So it got "none".

The same thing happens at `neorv32_bus.vhd:763` (`arbiter <= arbiter_nxt;`).

## The fix

**Rule.** A statement whose target is a whole record R (written as its bare name: no index, no field) is also an
assignment to each field of R that the closed set lists. For each field R.f:

- R.f gets a new occurrence on that line. It copies R's row there (line text, enclosing structure, conditions, SITE
  tags). Its ID comes after R.f's existing IDs, so **every existing occurrence ID keeps its line**.
- Its links come from the pair finder's own routine, run once more with R.f as the target. So the clock, reset and
  condition links are the same as R's.
- If the value is another whole record S (as in `ctrl <= ctrl_nxt`), S.f also gets an occurrence on that line, and
  the link becomes S.f -> R.f.
- If the value is a process variable (`neorv32_bus.vhd:395`, `int_rsp <= tmp_v;`), each field follows only the
  lines that build that field of the variable. Hand-read: line 391 builds `tmp_v.ack`, so `int_rsp.ack` comes from
  `port_rsp` at line 391 only.
- Storage, kind, constant drivers and port drive then come from the merge, unchanged.

**Two scopes**, chosen with `--scope`:
- `all` (default, recommended): every whole-record assignment, 70 statements.
- `clocked`: only those on a clock edge, 6 statements. This is exactly the case the FP analysis reported.

Why `all` is the default: `clocked` alone gives a wrong answer in at least one place. At `neorv32_bus.vhd` lines
219-233, `device_req_o` is loaded on the clock edge in one `generate` branch (line 222) and copied without a clock in
the other (line 233). With `clocked`, the field `device_req_o.addr` becomes "edge" while its own record says "mixed".
With `all` both say "mixed". `all` also fixes 465 output-port fields in tuning that the map called "undriven" (462
become "driven", 3 "tied"), for example the 32 `dev_XX_req_o` ports at `neorv32_bus.vhd:617-648`. Choosing the
scope is your call; the default is `all`.

## Run

```bash
%USERPROFILE%\miniconda3\python.exe final/record_fields.py --scope all
```

About two minutes. It runs the self-tests first and reports nothing if one fails.

## Self-tests (all passed on 2026-10-09, both scopes)

- **Same code, rule switched off** (scope "none"): rebuilds the stored maps byte for byte (43 of 43) and the traced
  inputs byte for byte (41 of 41). So the only difference is the rule.
- **Hand-read, cache:126**: each of the 8 fields of `ctrl_t` (lines 98-107) now has one new occurrence at line 126,
  storage "edge", CLOCKED_BY clk_i and COPIES ctrl_nxt.<field> there. `ctrl_nxt.<field>` CARRIES ctrl.<field> there.
  clk_i (line 125) SEQUENCES each field. Old occurrences unchanged.
- **Hand-read, bus:763**: the same for the 4 fields of `arbiter_t` (lines 734-739).
- **Must not change**: `cache_o` and its 6 fields (cache lines 76-84; never assigned or read as a whole), and the
  record element `ctrl` itself: 8 of 8 identical.
- **Wider rule** (scope all): `device_req_o.addr` at bus 219/222/233 is "mixed", with RESET_BY rstn_i at 219,
  CLOCKED_BY clk_i and GATED_BY host_req_i.stb at 222, COPIES host_req_i.addr at 222 and 233.
- **Process variable** (scope all): `port_rsp` sources `int_rsp.data`, `.ack`, `.err` only at lines 390, 391, 392.
- The existing checks on the new files: `traced_inputs.selftest` passes; every occurrence sits on a numbered line that
  names the element or its record (tuning 6,380 of 6,380; held-out 12,192 of 12,192).
- Invariants on every changed element: old occurrences kept; every added link has a new occurrence at one end; no old
  link lost except 2 guard texts rewritten (below); nothing else changed. 0 failures.

## What changed (exact counts; 41 modules: 15 tuning, 26 held-out)

| | `clocked` | `all` |
|---|---|---|
| whole-record statements expanded | 6 (3 modules) | 70 (19 modules) |
| tuning elements changed | 57 of 1,641 (2 of 15 modules) | 581 of 1,641 (9 of 15 modules) |
| held-out elements changed | 91 of 1,713 (1 of 26 modules) | 128 of 1,713 (10 of 26 modules) |
| all 41 modules | 148 of 3,354 | 709 of 3,354 |
| of these, storage changed | 69 | 555 |
| design maps (boot_rom, fifo; not scored) | 0 of 48 | 0 of 48 |

The 148 `clocked` changes are all among the 709 `all` changes. Denominators are exact: every element of every map.

Storage changes, tuning (`clocked` / `all`): none -> edge 12 / 12; not assigned -> edge 12 / 0; not assigned ->
mixed 0 / 12; edge -> mixed 0 / 3; not assigned -> none 0 / 459. Held-out: none -> edge 6 / 6; not assigned -> edge
39 / 39; not assigned -> none 0 / 24.

Where the 709 come from, by statement type: 36 copies from an array slot (`dev_00_req_o <= dev_req(0);`), 18 reset
assignments (`bus_rsp_o <= rsp_terminate_c;`), 6 clocked copies, 6 other whole-record copies, 2 process variables,
2 constants. One entity, `neorv32_bus_io_switch`, holds 389 of the 581 tuning changes.

The full lists: `all/changes.csv` and `clocked/changes.csv` (one row per changed element: old and new storage, kind,
drive, handling, added occurrences, added links) and `*/statements.csv` (one row per expanded statement).

## What it breaks, and what needs a labelled re-run

**Kept the same (checked):**
- Nothing earlier is overwritten. Earlier runs keep pointing at the inputs they read.
- Every old occurrence ID keeps its line. So earlier outputs still point at the right lines.
- The final gpt-5.4 runs' citations, checked again by `trace_check` against the new map: 0 of 725 (tuning, 3 runs)
  and 0 of 1,472 (held-out, 3 runs) change status. The reason: old outputs cite old IDs, and the new facts sit on new
  IDs. So trace-check readings of earlier runs do not move.

**Changed:**
- **What the model reads.** With `all`, the traced input changes for 9 of 15 tuning modules and 10 of 26 held-out
  modules. It grows by 8.8% (tuning) and 1.6% (held-out) in characters. With `clocked`: 2 of 15 and 1 of 26; +1.1%
  and +0.8%.
- **Occurrence order.** New IDs are added at the end, so for changed fields the IDs no longer follow line order
  (`ctrl.state`: occurrence 4 is line 164, occurrence 5 is line 126). The final prompt (line 22) says occurrences are
  "every place the element's name appears". The new ones sit where only the record's name appears.
- **Two old guard texts** (scope all, cache): `ctrl.state` SELECTS `host_rsp_o.ack` and `host_rsp_o.err` now also
  name `S_DIRECT_RSP`. That is correct: line 209 `host_rsp_o <= bus_rsp_i;` sits under `when S_DIRECT_RSP` (line 207).
- **Map accuracy against the gold sample** (120 tuning occurrences, LLM-written and adjudicated): typed precision of
  the map's links 0.988 -> 0.982 with `all` (210 correct, 3 wrong, 2 missed; was 2 wrong). Unchanged with `clocked`.
  Recall unchanged at 0.988. The one new "wrong" is `port_rsp` at bus line 390 sourcing `int_rsp.data`. The RTL
  supports it (line 390 builds `tmp_v.data`; line 395 copies it into `int_rsp`). The gold was written before fields
  were expanded. This is link precision of the map, not asset precision.
- **The MAP CHECK line** shows 42 more (tuning) and 3 more (held-out) "receiving without driving" links. The map
  holds both ends. The flow-graph code looks elements up by name only, so in a file with several entities that each
  have a `clk_i` it keeps only one of them. The stored version already showed 92 such links in tuning and 92 in
  held-out. Fixed 2026-10-10 in `final/flow_entities/` (flow graph per entity): with both fixes, 0 remain.

**Earlier metrics:**
- **Asset precision and recall** of every run that read the map (`ist2` on gpt-5-mini and gpt-5.4; the final prompt
  on gpt-5.4, tuning and held-out; the Claude-executed prompt loop) were made with the old map. They stay correct
  for that map. They say nothing about the fixed map. Runs on RTL only (`ism`) are not affected.
- In the final gpt-5.4 runs, 46 of 725 tuning listings and 25 of 1,472 held-out listings name an element this
  version changes (16 and 5 name one whose storage changed). These counts are the same for both scopes.
- **Any claim about the fix's effect on assets needs a new, labelled generation run**, kept in its own folder and
  never mixed with r0-r2 in one metric. Suggested: the final prompt on gpt-5.4 with the new inputs, tuning only, the
  9 changed modules x 3 runs = 27 calls, compared module by module with r0-r2 on the same 9 modules. Read two things:
  (1) the "map gap claimed" count on changed elements, which should fall (in r1 and r2 all 8 `ctrl.*` fields are
  listed as "stores" with their reset line and no edge; the new map gives each a CLOCKED_BY at line 126); if it does
  not fall, the fix does not reach the model; (2) strict precision and recall on those 9 modules.
- **Held-out:** the three pre-registrations pin `assetgen_meta/traced_inputs_v2/heldout` by hash. Reading the
  held-out set again with the new map is a new look at held-out data and needs its own pre-registration first.
- Code-only diagnostics that read the map records of listed elements (fp_diagnosis, fault reporter, the FP error
  analysis) were not recomputed here. They can move only through the 46 + 25 listings above. Recomputing them with old
  outputs and the new map mixes two versions; label it so if it is done.

## Still not expanded

- A whole record used as a value into one slot of an array (`dev_req(i) <= main_req;`, bus line 660): 38 links in
  `neorv32_bus`, none in held-out. So the 32 `dev_XX_req_o` ports still have no traced path from an input. Their
  fields moved from the flow graph's "no local use" list to its "not reached from an input" list.
- Whole-record port connections to sub-blocks: 19 (tuning) and 10 (held-out). That is a different mechanism
  (connections, not assignments).

## Files

- `final/record_fields.py`: the builder, self-tests and read-outs. It imports `final/final_pipeline.py` and swaps in
  its own `build_module` while it runs; no existing file is edited.
- `final/record_fields/<scope>/`: `maps/`, `traced_inputs/`, `occurrence_profiles/`, `closed_sets/` (the
  `final_pipeline` layout), `expansions.json`, `changes.json`, `changes.csv`, `statements.csv`, `readout.json`.
  The four rebuilt folders are not committed (`.gitignore`); running the script rebuilds them, after its self-tests.
