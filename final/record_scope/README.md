# Record types read in their own scope (new closed-set version, 2026-10-10)

**Short version.** One held-out file, `neorv32_cpu_cp_fpu.vhd`, declares two different record types with the same
name, once in each of two entities. The parser that builds the closed set (the fixed list of every port, signal and
record field) gave both entities the second declaration. So one entity, the normalizer, was listed with 8 fields it
does not have, without 12 fields it does have, and with the wrong width for 2 more. `final/record_scope.py` builds a
new closed set that reads each record type where it is declared, and rebuilds the maps and traced inputs from it.
Only this one file changes. No earlier run listed any of the affected elements. Nothing earlier is overwritten.

Words used here:
- **record type**: a template for a box with named compartments (`type ctrl_t is record ... end record`). A signal of
  that type (`signal ctrl : ctrl_t;`) gets one closed-set element per compartment (`ctrl.state`, `ctrl.cnt`, ...).
- **scope**: where a name can be seen. A type declared inside an entity's architecture can be seen only there.
- **closed set**: the fixed list of elements per file. The maps, the run validator and the prompt leak audit all read
  it.

## What was wrong

`src/rtl_parse.parse_rtl_file` puts every record type of a file into one table keyed by type name. A second type with
the same name replaces the first, for every entity in the file. In `neorv32_cpu_cp_fpu.vhd`:

| type | normalizer (entity from line 1546) | f2i (entity from line 1986) |
|---|---|---|
| `ctrl_t` | lines 1577-1590: state, norm_r, cnt (8 downto 0), cnt_pre, cnt_of, cnt_uf, rounded, res_sgn, res_exp, res_man, class, flags | lines 2015-2027: state, unsign, cnt (7 downto 0), sign, class, rounded, over, under, result_tmp, result, flags |
| `sreg_t` | 1594-1603: done, dir, zero, upper, lower, ext_g, ext_r, ext_s | 2031-2037: int, mant, ext_g, ext_r, ext_s |
| `round_t` | 1607-1611: en, sub, output (24 downto 0) | 2041-2045: en, sub, output (32 downto 0) |

The f2i declarations come later in the file, so the normalizer got them:
- 8 elements listed that the normalizer does not have: `ctrl.unsign`, `ctrl.sign`, `ctrl.over`, `ctrl.under`,
  `ctrl.result_tmp`, `ctrl.result`, `sreg.int`, `sreg.mant`. In the map each had only its declaration line and no record.
- 12 elements missing: `ctrl.norm_r`, `ctrl.cnt_pre`, `ctrl.cnt_of`, `ctrl.cnt_uf`, `ctrl.res_sgn`, `ctrl.res_exp`,
  `ctrl.res_man`, `sreg.done`, `sreg.dir`, `sreg.zero`, `sreg.upper`, `sreg.lower`.
- 2 elements with the wrong type: `ctrl.cnt` (7 downto 0 instead of 8 downto 0) and `round.output` (32 downto 0
  instead of 24 downto 0).

A correction to an earlier statement: on 2026-10-10 I first said `round_t` was declared twice "with the same fields
(harmless)". The field names are the same, but `round.output` has a different width, so it was not harmless.

Across all 44 RTL files, this is the only file that declares a record type name twice.

## The fix

A record type is looked up first in the architecture that declares the signal, then in the file outside its
architectures (its packages), then in other files' packages. Ports use the last two only, since an entity's ports
are declared before any architecture. This is how VHDL decides which declaration a name means. The code is a new
parse function in `final/record_scope.py`; `src/rtl_parse.py` and `step1/build_heldout_code_map.py` are not edited.

Two versions are written:
- `scoped/`: this fix alone, built by the same pipeline as every earlier run (`final_pipeline.build_all`).
- `all_three_fixes/`: this fix, the record-field fix (`final/record_fields.py`, scope all) and the flow graph per
  entity (`final/flow_entities.py`) together.

## Run

```bash
%USERPROFILE%\miniconda3\python.exe final/record_scope.py
```

It runs two full builds. The self-tests run first; nothing is reported if one fails. Run `final/record_fields.py`
and `final/flow_entities.py` first: the `all_three_fixes` check compares against their outputs. The rebuilt
`closed_sets/`, `maps/`, `occurrence_profiles/` and `traced_inputs/` folders are not committed (`.gitignore`).

## Self-tests (all passed on 2026-10-10)

- The old parse reproduces the stored closed sets, row for row (entity, name, direction, type, in order): 44 of 44
  files. (`data/parsed_tuning18` also stores a `"function"` text on its 18 tuning files; no map reads it.)
- Hand-read, lines 1577-1612 and 2015-2046: the new closed set gives the normalizer exactly its own `ctrl`, `sreg` and
  `round` fields with their own types, and f2i exactly its own. The stored set matches the hand-read list for f2i,
  not for the normalizer.
- Must not change: only the fpu file's closed set changes. The other 43 files, including the single-entity
  `neorv32_gpio`, are identical.
- New fpu map: each of the 23 normalizer fields has an occurrence on exactly the normalizer lines that write it, plus
  its record's declaration line. No f2i-only field is left in the normalizer.
- The existing checks on the new files pass: `traced_inputs.selftest`, and every occurrence sits on a numbered line
  that names it (tuning 5,760 of 5,760; held-out 12,089 of 12,089; for `all_three_fixes` 6,380 and 12,284).
- `all_three_fixes` also passes the record-field hand-read tests, and it differs from the two-fix version only in the
  fpu map and the fpu traced input.

## What changed (exact counts, `scoped`)

- Closed set: 1 of 44 files. In it, the normalizer loses 8 elements, gains 12, and 2 change type. The order of the
  fields both versions share also changes (the normalizer's own declaration order).
- Maps: 17 of 17 tuning and design maps and 25 of 26 held-out maps are byte-identical. `neorv32_cpu_cp_fpu`: 241 -> 245
  elements, 8 removed, 12 added, 17 changed. All 37 are in the normalizer entity; the top entity and f2i are
  unchanged. The 17 changed elements only gain links, each to one of the 12 new fields; no old link is lost.
- One consequence was a real misreading. The normalizer's output `result_o` was marked "tied" (driven only by
  constants). Lines 1884-1886 (`result_o(31) <= ctrl.res_sgn;`, `... <= ctrl.res_exp;`, `... <= ctrl.res_man;`) read
  fields that were missing from the closed set, so the map took those right-hand sides for constant values. Now it
  is "driven". The same happened to `round.en` at line 1910 and `sreg.ext_g` at line 1764 (`<= sreg.lower(0);`).
- Traced inputs: only `neorv32_cpu_cp_fpu` changes, 412,495 -> 459,614 characters (+11.4%). The held-out total grows
  2.5% (1,915,993 -> 1,963,112). No tuning input changes.

With all three fixes: 10 of 15 tuning inputs change (943,179 -> 1,035,852 characters, +9.8%) and 12 of 26 held-out
inputs (1,915,993 -> 1,998,191, +4.3%). The full lists are in `<version>/changes.json` and `listings.json`.

## What it breaks for comparability

- **Nothing pinned is touched.** `HELDOUT_PREREG.md` pins `parsed_heldout26/` and `step1/build_heldout_code_map.py`
  by hash; `lasset_layer/PREREG.md` pins `data/parsed_tuning18`. The new closed sets live in this folder only.
- **Earlier outputs validate the same way.** The run validator checks each listed (entity, element) against the closed
  set. In the held-out runs, none of the 6 runs `assets_heldout26_*` (the gpt-5-mini `ism` runs and the final gpt-5.4
  runs) lists any normalizer element. The two Claude-executed held-out loop runs list 13 and 17. Every one of them is
  still in the new closed set. So no earlier output would gain a validation issue.
- **Scoring does not move.** The reference lists 12 fpu entries, all of them in the top entity (`ctrl_i`, `res_o`,
  `csr_frm`, ...). No normalizer element is in the reference.
- **The prompt leak audit** (`meta_tools.corpus_names`) reads `data/parsed_tuning18`. If the new fpu closed set were
  put there, the 12 new names would join the audit's list. None of them is written in the final prompt.
- **`verify_against_cache`** (`src/parse_v3.py`) compares a re-annotated file with `data/parsed_tuning18` and raises on
  any difference. Re-annotating the fpu file with the new closed set would raise there, by design. Such a run needs
  its own cache folder.
- **What the model reads** changes only for the held-out fpu module. A held-out reading with the fixed inputs needs its
  own pre-registration first, as for the other two fixes. A tuning re-run is not affected by this fix: the tuning
  inputs of `all_three_fixes` equal those of the two-fix version.
