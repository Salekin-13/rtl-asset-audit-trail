"""Record types resolved in their own scope: a NEW closed-set version, and the maps and traced inputs built from it.
No model call. src/rtl_parse.py, step1/build_heldout_code_map.py and every stored closed set stay as they are.

The defect (found 2026-10-10 while building final/flow_entities.py). rtl_parse.parse_rtl_file reads every record type
declared anywhere in a file into one table keyed by type name (`reg.update(collect_records(masked))`), and expands
every record-typed signal of every entity from that table. When one file declares two different record types with
the same name in two architectures, the later declaration wins for both entities. In this corpus only
data/RTL_heldout/neorv32_cpu_cp_fpu.vhd does that: `ctrl_t` at 1577 (entity neorv32_cpu_cp_fpu_normalizer) and 2015
(neorv32_cpu_cp_fpu_f2i), `sreg_t` at 1594 and 2031, `round_t` at 1607 and 2041. So the normalizer's closed set
lists f2i's fields (ctrl.over, sreg.mant, ...), lacks its own (ctrl.norm_r, sreg.upper, ...), and gives the fields
both share f2i's types (ctrl.cnt (7 downto 0) instead of (8 downto 0); round.output (32 downto 0) instead of
(24 downto 0)).

The fix (VHDL visibility: a type declared in an architecture is visible in that architecture only). A record type is
looked up first in the architecture that declares the signal, then in the file outside its architectures (its
packages), then in the other files' packages. Ports, which an entity declares before any architecture, use the last
two only. The cross-file registry is read from outside the architectures too.

Outputs  final/record_scope/<version>/{closed_sets, maps, occurrence_profiles, traced_inputs}  (final_pipeline layout)
           version "scoped"           this fix alone, on the code path every earlier run used
           version "all_three_fixes"  this fix + final/record_fields.py (scope all) + final/flow_entities.py
         final/record_scope/<version>/changes.json   closed set, map and traced-input differences from the stored ones
Run with the conda Python:  %USERPROFILE%\\miniconda3\\python.exe final/record_scope.py
The self-tests run first; nothing is reported if one fails.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import final_pipeline as FP          # noqa: E402

OUT = HERE / "record_scope"
_ARCH = re.compile(r"\barchitecture\s+\w+\s+of\s+\w+\s+is\b", re.IGNORECASE)


# ------------------------------------------------------------------------------------------------ the fix ---
def _rp():
    import rtl_parse
    return rtl_parse


def outside_architectures(masked: str) -> str:
    """The file with every architecture region (as rtl_parse.arch_region bounds it) blanked."""
    RP = _rp()
    out, pos = [], 0
    for m in _ARCH.finditer(masked):
        if m.start() < pos:
            continue
        nxt = RP._UNIT_START.search(masked, m.end())
        end = nxt.start() if nxt else len(masked)
        out += [masked[pos:m.end()], " " * (end - m.end())]
        pos = end
    out.append(masked[pos:])
    return "".join(out)


def registry_scoped(paths) -> dict:
    """rtl_parse.build_record_registry, reading each file outside its architectures only."""
    RP, reg = _rp(), {}
    for p in paths:
        reg.update(RP.collect_records(outside_architectures(
            RP._mask_comments(Path(p).read_text(encoding="utf-8", errors="ignore"), vhdl=True))))
    return reg


def parse_rtl_file_scoped(path, records=None) -> dict:
    """rtl_parse.parse_rtl_file with record types resolved in scope (module docstring)."""
    RP = _rp()
    raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    masked = RP._mask_comments(raw, vhdl=True)
    file_reg = dict(records or {})
    file_reg.update(RP.collect_records(outside_architectures(masked)))
    entities = []
    for em in RP._ENTITY.finditer(masked):
        ename = em.group(1)
        ports = RP.parse_ports(em.group(0), file_reg)
        region = RP.arch_region(masked, ename)
        signals = []
        if region is not None:
            reg = dict(file_reg)
            reg.update(RP.collect_records(region))
            signals = RP.parse_signals(region, reg)
        entities.append({"entity": ename, "ports": ports, "signals": signals})
    return {"file": Path(path).stem, "entities": entities}


_REG = None


def parse_closed_scoped(stem: str, rtl_dir: Path) -> dict:
    """build_heldout_code_map.parse_closed with the scoped parse and the scoped registry."""
    global _REG
    CM = sys.modules["build_heldout_code_map"]
    if _REG is None:
        _REG = registry_scoped(sorted(CM.TUNE_RTL.glob("*.vhd")) + sorted(CM.HELD_RTL.glob("*.vhd")))
    return CM._flatten(stem, parse_rtl_file_scoped(str(Path(rtl_dir) / f"{stem}.vhd"), _REG))


# ------------------------------------------------------------------------------------------------ comparison ---
def _rows(d: dict) -> list[tuple]:
    return [(e["entity"], e["name"], e.get("dir"), e.get("type")) for e in d["ports"] + d["signals"]]


def compare_closed(CM) -> dict:
    """Old route (build_heldout_code_map.parse_closed) vs the scoped route, every file of RTL_data and RTL_heldout."""
    out = {}
    for rtl in (CM.TUNE_RTL, CM.HELD_RTL):
        for p in sorted(rtl.glob("*.vhd")):
            a, b = _rows(CM.parse_closed(p.stem, rtl)), _rows(parse_closed_scoped(p.stem, rtl))
            if a == b:
                continue
            ka, kb = {r[:2]: r for r in a}, {r[:2]: r for r in b}
            out[f"{rtl.name}/{p.stem}"] = {
                "removed": [list(ka[k]) for k in ka if k not in kb],
                "added": [list(kb[k]) for k in kb if k not in ka],
                "type changed": [[*k, ka[k][3], kb[k][3]] for k in ka if k in kb and ka[k] != kb[k]],
                "order changed": [k for k in ka if k in kb] != [k for k in kb if k in ka]}
    return out


def compare_maps(new_maps: dict, mods: dict) -> dict:
    """Element by element, stored maps vs new maps (41 scored + 2 design modules)."""
    res = {}
    for split, ms in (("tuning", mods["tuning"] + FP.DESIGN), ("heldout", mods["heldout"])):
        same, rows = 0, []
        for m in ms:
            stored = FP.STORED_MAPS["design" if m in FP.DESIGN else split] / f"{m}.json"
            a, b = stored.read_bytes(), (new_maps[split] / f"{m}.json").read_bytes()
            if a == b:
                same += 1
                continue
            da = {(e["entity"], e["name"]): e for x in ("ports", "signals") for e in json.loads(a)[x]}
            db = {(e["entity"], e["name"]): e for x in ("ports", "signals") for e in json.loads(b)[x]}
            rows.append({"module": m, "elements": [len(da), len(db)],
                         "removed": sorted(map(list, set(da) - set(db))), "added": sorted(map(list, set(db) - set(da))),
                         "changed": sorted([list(k) for k in set(da) & set(db) if da[k] != db[k]])})
        res[split] = {"modules": len(ms), "byte-identical": same, "differ": rows}
    return res


# ------------------------------------------------------------------------------------------------ self-tests ---
# Hand-read on 2026-10-10, data/RTL_heldout/neorv32_cpu_cp_fpu.vhd.
#   1546 `entity neorv32_cpu_cp_fpu_normalizer is`; its architecture declares 1577-1590 `type ctrl_t is record` state
#   (ctrl_engine_state_t), norm_r, cnt (std_ulogic_vector(8 downto 0)), cnt_pre (8 downto 0), cnt_of, cnt_uf, rounded,
#   res_sgn, res_exp (7 downto 0), res_man (22 downto 0), class (9 downto 0), flags (4 downto 0); 1591
#   `signal ctrl : ctrl_t;`; 1594-1603 `type sreg_t is record` done, dir, zero, upper (31 downto 0), lower
#   (22 downto 0), ext_g, ext_r, ext_s; 1604 `signal sreg : sreg_t;`; 1607-1611 `type round_t is record` en, sub,
#   output (24 downto 0); 1612 `signal round : round_t;`.
#   1986 `entity neorv32_cpu_cp_fpu_f2i is`; 2015-2027 ctrl_t: state, unsign, cnt (7 downto 0), sign, class, rounded,
#   over, under, result_tmp (31 downto 0), result (31 downto 0), flags; 2031-2037 sreg_t: int (31 downto 0), mant
#   (22 downto 0), ext_g, ext_r, ext_s; 2041-2045 round_t: en, sub, output (32 downto 0).
NORMALIZER = {"ctrl": [("state", "ctrl_engine_state_t"), ("norm_r", "std_ulogic"), ("cnt", "std_ulogic_vector(8 downto 0)"),
                       ("cnt_pre", "std_ulogic_vector(8 downto 0)"), ("cnt_of", "std_ulogic"), ("cnt_uf", "std_ulogic"),
                       ("rounded", "std_ulogic"), ("res_sgn", "std_ulogic"), ("res_exp", "std_ulogic_vector(7 downto 0)"),
                       ("res_man", "std_ulogic_vector(22 downto 0)"), ("class", "std_ulogic_vector(9 downto 0)"),
                       ("flags", "std_ulogic_vector(4 downto 0)")],
              "sreg": [("done", "std_ulogic"), ("dir", "std_ulogic"), ("zero", "std_ulogic"),
                       ("upper", "std_ulogic_vector(31 downto 0)"), ("lower", "std_ulogic_vector(22 downto 0)"),
                       ("ext_g", "std_ulogic"), ("ext_r", "std_ulogic"), ("ext_s", "std_ulogic")],
              "round": [("en", "std_ulogic"), ("sub", "std_ulogic"), ("output", "std_ulogic_vector(24 downto 0)")]}
F2I = {"ctrl": [("state", "ctrl_engine_state_t"), ("unsign", "std_ulogic"), ("cnt", "std_ulogic_vector(7 downto 0)"),
                ("sign", "std_ulogic"), ("class", "std_ulogic_vector(9 downto 0)"), ("rounded", "std_ulogic"),
                ("over", "std_ulogic"), ("under", "std_ulogic"), ("result_tmp", "std_ulogic_vector(31 downto 0)"),
                ("result", "std_ulogic_vector(31 downto 0)"), ("flags", "std_ulogic_vector(4 downto 0)")],
       "sreg": [("int", "std_ulogic_vector(31 downto 0)"), ("mant", "std_ulogic_vector(22 downto 0)"),
                ("ext_g", "std_ulogic"), ("ext_r", "std_ulogic"), ("ext_s", "std_ulogic")],
       "round": [("en", "std_ulogic"), ("sub", "std_ulogic"), ("output", "std_ulogic_vector(32 downto 0)")]}


def _fields_of(d: dict, ent: str, rec: str) -> list[tuple]:
    return [(e["name"].split(".", 1)[1], e["type"]) for e in d["signals"] if e["entity"] == ent and e["name"].startswith(rec + ".")]


def selftest_closed(CM, log=print) -> bool:
    ok = True
    # (1) the old route gives the stored closed sets (so the comparison starts from what every run used): the rows
    #     (entity, name, dir, type), in order. data/parsed_tuning18 also carries a "function" text on its 18 RTL_data
    #     files, which is not part of the regex closed set and which no map reads.
    n = same = 0
    for rtl, stored in ((CM.TUNE_RTL, ROOT / "data/parsed_tuning18"), (CM.HELD_RTL, ROOT / "data/parsed_heldout26")):
        for p in sorted(rtl.glob("*.vhd")):
            f = stored / f"{p.stem}.json"
            if not f.exists():
                continue
            n += 1
            same += _rows(CM.parse_closed(p.stem, rtl)) == _rows(json.loads(f.read_text(encoding="utf-8")))
    ok &= same == n
    log(f"   old route reproduces the stored closed-set rows: {same}/{n}: {'ok' if same == n else 'FAIL'}")
    # (2) hand-read, fpu 1577-1612 and 2015-2046: each entity gets its own fields, with their own types
    old = CM.parse_closed("neorv32_cpu_cp_fpu", CM.HELD_RTL)
    new = parse_closed_scoped("neorv32_cpu_cp_fpu", CM.HELD_RTL)
    for ent, want in (("neorv32_cpu_cp_fpu_normalizer", NORMALIZER), ("neorv32_cpu_cp_fpu_f2i", F2I)):
        for rec, fields in want.items():
            got_new, got_old = _fields_of(new, ent, rec), _fields_of(old, ent, rec)
            good = got_new == fields
            ok &= good
            log(f"   {ent} {rec}: new {'= hand-read' if good else 'FAIL ' + str(got_new)}; "
                f"stored {'= hand-read' if got_old == fields else 'differs from hand-read'}")
    # (3) must not change: a single-entity file, and every file but this one
    diff = compare_closed(CM)
    good = list(diff) == ["RTL_heldout/neorv32_cpu_cp_fpu"] and "RTL_heldout/neorv32_gpio" not in diff
    ok &= good
    log(f"   files whose closed set changes: {sorted(diff)} (expected only the fpu file; neorv32_gpio unchanged): "
        f"{'ok' if good else 'FAIL'}")
    return ok


# The occurrence lines of each normalizer field in the new map: the lines of the normalizer (1546-1985) that write
# `<record>.<field>` (comments cut), plus the line that declares the record signal (1591 ctrl, 1604 sreg, 1612 round),
# the occurrence profiler's rule for a field. The f2i-only fields must be gone from the normalizer.
DECL_LINE = {"ctrl": 1591, "sreg": 1604, "round": 1612}


def selftest_map(new_maps: dict, log=print) -> bool:
    ok = True
    d = json.loads((new_maps["heldout"] / "neorv32_cpu_cp_fpu.json").read_text(encoding="utf-8"))
    els = {(e["entity"], e["name"]): e for x in ("ports", "signals") for e in d[x]}
    lines = (ROOT / "data/RTL_heldout/neorv32_cpu_cp_fpu.vhd").read_text(encoding="utf-8").splitlines()
    n = good_n = 0
    for rec, fields in NORMALIZER.items():
        for f, _t in fields:
            rx = re.compile(rf"(?<![\w.]){re.escape(rec)}\s*\.\s*{re.escape(f)}(?!\w)", re.I)
            want = {i for i in range(1546, 1986) if rx.search(lines[i - 1].split("--")[0])} | {DECL_LINE[rec]}
            e = els.get(("neorv32_cpu_cp_fpu_normalizer", f"{rec}.{f}"))
            got = {o["line"] for o in e["occurrences"]} if e else None
            n += 1
            good_n += got == want
            if got != want:
                log(f"   normalizer {rec}.{f}: occurrence lines {sorted(got or [])} vs written at {sorted(want)}: FAIL")
    only_f2i = {f"{r}.{f}" for r, fs in F2I.items() for f, _ in fs} - {f"{r}.{f}" for r, fs in NORMALIZER.items() for f, _ in fs}
    left = sorted(x for x in only_f2i if ("neorv32_cpu_cp_fpu_normalizer", x) in els)
    ok = good_n == n and not left
    log(f"   new fpu map: {good_n}/{n} normalizer fields on exactly the lines that write them (+ declaration); "
        f"f2i-only fields still in the normalizer: {left or 'none'}: {'ok' if ok else 'FAIL'}")
    return ok


# ------------------------------------------------------------------------------------------------ build ---
def build(version: str):
    """version "scoped": final_pipeline.build_all with the scoped closed sets. version "all_three_fixes": the same
    with record_fields.build_module (scope all), then the FLOW GRAPH section per entity (flow_entities.text)."""
    import record_fields as RF
    import flow_entities as FE
    out = OUT / version
    ns = FP.setup()
    mods = FP.modules(ns.ea)
    CM = ns.CM
    keep = CM.parse_closed
    CM.parse_closed = parse_closed_scoped
    try:
        if version == "scoped":
            built = FP.build_all(ns, mods, out=out)
        else:
            _ns, _m, built = RF.build("all", out)
            TI = ns.TI
            for split in ("tuning", "heldout"):
                for m in mods[split]:
                    t, fg = FE.text(TI, split, m)
                    (out / "traced_inputs" / split / f"{m}.txt").write_text(t, encoding="utf-8")
                    (out / "traced_inputs" / split / "_flow" / f"{m}.json").write_text(json.dumps(fg, indent=1), encoding="utf-8")
    finally:
        CM.parse_closed = keep
    return ns, mods, built


def listings(closed_diff: dict) -> dict:
    """Listed structural assets in the held-out runs that name an (entity, element) the scoped closed set no longer
    has: the validator (meta_tools.validate_nested) accepted them and would now report an entity mismatch."""
    gone = {tuple(r[:2]) for r in closed_diff["RTL_heldout/neorv32_cpu_cp_fpu"]["removed"]}
    out = {}
    for d in sorted(set(ROOT.glob("runs/assets_heldout26_*")) | set(ROOT.glob("runs/assets_opt_heldout_*"))):
        f = d / "_nested" / "neorv32_cpu_cp_fpu.json"
        if not f.exists():
            continue
        obj = json.loads(f.read_text(encoding="utf-8"))
        refs = [(s.get("entity"), s.get("asset rtl")) for c in obj.get("conceptual assets", []) or []
                for s in c.get("related structural assets", []) or [] if isinstance(s, dict)]
        out[d.name] = {"fpu listings": len(refs),
                       "normalizer listings": sum(1 for e, _n in refs if e == "neorv32_cpu_cp_fpu_normalizer"),
                       "on an element the fix removes": sorted({f"{e}/{n}" for e, n in refs if (e, n) in gone})}
    return out


def _differ(a_dir: Path, b_dir: Path, names: list[str], suffix: str) -> list[str]:
    return [m for m in names if (a_dir / f"{m}{suffix}").read_bytes() != (b_dir / f"{m}{suffix}").read_bytes()]


def main() -> dict:
    sys.stdout.reconfigure(encoding="utf-8")
    ns = FP.setup()
    CM = ns.CM
    ok = selftest_closed(CM)
    if not ok:
        print("SELF-TEST FAILED: nothing written")
        return {"ok": False}
    import record_fields as RF
    closed = compare_closed(CM)
    res = {}
    for version in ("scoped", "all_three_fixes"):
        ns, mods, built = build(version)
        good = selftest_map(built["maps"]) and RF.selftest_pipeline(ns, mods, built)
        if version == "all_three_fixes":
            good &= RF.selftest_hand_read(built, "all")
            # the fixes compose: against the two-fix version (record fields + flow per entity) only the fpu file differs
            two = {"maps": {s: HERE / "record_fields/all/maps" / s for s in ("tuning", "heldout")},
                   "traced_inputs": HERE / "flow_entities/record_fields_all"}
            comp = {s: {"maps": _differ(two["maps"][s], built["maps"][s], mods[s], ".json"),
                        "traced inputs": _differ(two["traced_inputs"] / s, built["traced_inputs"] / s, mods[s], ".txt")}
                    for s in ("tuning", "heldout")}
            fine = comp == {"tuning": {"maps": [], "traced inputs": []},
                            "heldout": {"maps": ["neorv32_cpu_cp_fpu"], "traced inputs": ["neorv32_cpu_cp_fpu"]}}
            good &= fine
            print(f"   against the two-fix version, files that differ: {comp}: {'ok' if fine else 'FAIL'}")
        if not good:
            print(f"SELF-TEST FAILED ({version}): nothing reported")
            return {"ok": False}
        cm = compare_maps(built["maps"], mods)
        tin = {}
        for split in ("tuning", "heldout"):
            ref = FP.TRACED_V2 / split
            a = sum(len((ref / f"{m}.txt").read_text(encoding="utf-8")) for m in mods[split])
            b = sum(len((built["traced_inputs"] / split / f"{m}.txt").read_text(encoding="utf-8")) for m in mods[split])
            tin[split] = {"modules": len(mods[split]), "changed": _differ(ref, built["traced_inputs"] / split, mods[split], ".txt"),
                          "characters": [a, b]}
        res[version] = {"closed sets": closed, "maps": cm, "traced inputs": tin}
        (OUT / version / "changes.json").write_text(json.dumps(res[version], indent=1), encoding="utf-8")
        print(version, "maps (identical, modules, [module, elements old/new, removed, added, changed]):",
              {s: (v["byte-identical"], v["modules"], [(r["module"], r["elements"], len(r["removed"]), len(r["added"]),
                                                        len(r["changed"])) for r in v["differ"]]) for s, v in cm.items()})
        print(version, "traced inputs (changed, modules, characters):",
              {s: (v["changed"], v["modules"], v["characters"]) for s, v in tin.items()})
    res["listings"] = listings(closed)
    (OUT / "listings.json").write_text(json.dumps(res["listings"], indent=1), encoding="utf-8")
    print("held-out runs, fpu listings:", json.dumps(res["listings"], indent=1))
    return {"ok": True, **res}


if __name__ == "__main__":
    main()
