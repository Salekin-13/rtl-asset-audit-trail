"""Record-field expansion of the code-built relationship map: a NEW map version. No model call.

The stored maps (step1/lasset_step1/relation_map_code_*/b0e767000ec2_codetags), the traced inputs built from them
(assetgen_meta/traced_inputs_v2, final/traced_inputs) and every pinned file stay as they are. This module rebuilds
the 41 module maps (+ the 2 design maps) with one added rule and writes them to final/record_fields/<scope>/.

The defect (found 2026-10-09, final/fp_error_analysis/out/a1_concepts/FINDINGS.md). An assignment to a whole record,
for example data/RTL_data/neorv32_cache.vhd:126 `ctrl <= ctrl_nxt;` on the clock edge, gave the record's fields
(ctrl.state, ctrl.tag, ...) no storage "edge", no CLOCKED_BY record and no COPIES record from ctrl_nxt.<field>. Three
steps combine; each one does what it was written to do:
  1. occurrence profile   a field occurs only where its own name is written, plus once on the line that declares its
                          base (build_occurrence_notebook_v3b.occurrence_lines). Line 126 writes only `ctrl` and
                          `ctrl_nxt`, so ctrl.state has no occurrence there.
  2. code_pairs_v2        a written name resolves to the longest declared element (_CPWriter._resolve). `ctrl` is the
                          record element ctrl, so the pairs of line 126 end on ctrl and start from ctrl_nxt only.
  3. relation_stage.merge storage is read from the element's own assignment occurrences. ctrl.state's only one is the
                          reset at line 117, which storage does not count, so ctrl.state gets "none".

The rule added here (VHDL: assigning a record assigns each of its elements). A statement whose target is a whole
record R, written as its bare name (no index, field or attribute), and whose fields the closed set lists, is also an
assignment to each listed field R.f:
  - R.f gets an occurrence on the statement's line: a copy of R's profile row there (line text, Context, Path, SITE
    tags), numbered after R.f's existing occurrences, so every existing Occurrence ID keeps its line;
  - its pairs are code_pairs_v2's own routine (_controls, _values) run for the statement with R.f as the target: the
    control pairs (SEQUENCES, RESETS, GATES, SELECTS, CONSTRAINS) are those of R; a bare process variable value is
    read as its matching field (bus:395 `int_rsp <= tmp_v;` gives int_rsp.ack only the definitions of tmp_v.ack, line
    391, and of tmp_v as a whole); an indexed array element (`a_req_o <= port_req(0)`) stays the source of each field;
  - when a value of the statement is a bare whole record S whose field S.f the closed set lists (`ctrl <= ctrl_nxt`),
    S.f also gets an occurrence on that line and the pair S -> R.f becomes S.f -> R.f (CARRIES, or SOURCES under a
    condition). A constant value gives no pair, as before.
Storage, kind, handling, constant drivers and boundary drive then follow from the unchanged merge.

Scopes: "all" (the default) applies the rule to every whole-record assignment; "clocked" only to those on a clock edge
(a SEQUENCES pair ends on R there: the case the analysis reported); "none" applies nothing and must rebuild the stored
maps and traced inputs byte for byte (self-test).

Outputs  final/record_fields/<scope>/{closed_sets, maps, occurrence_profiles, traced_inputs}/   (final_pipeline layout)
         final/record_fields/<scope>/expansions.json   every expanded statement, by module
Run with the conda Python (tree_sitter_language_pack):
         %USERPROFILE%\\miniconda3\\python.exe final/record_fields.py [--scope all|clocked]
The self-tests run first; nothing is reported if one fails.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import final_pipeline as FP          # noqa: E402

OUT = HERE / "record_fields"
SCOPES = ("none", "clocked", "all")
EXPANSIONS: dict[str, list[dict]] = {}


# --------------------------------------------------------------------------------------------- the expansion ---
def _fields(name: str, names) -> list[str]:
    low = name.lower() + "."
    return sorted(n for n in names if n.lower().startswith(low))


def _match(name: str, names) -> str | None:
    return next((n for n in names if n.lower() == name.lower()), None)


def _row(src: dict) -> dict:
    """A copy of a record's profile row, for one of its fields (the ID is set later)."""
    r = {k: (list(v) if isinstance(v, list) else v) for k, v in src.items()}
    r["Name As Written"] = f"{src['Name As Written']} (whole record)"
    return r


_SLOT = -1          # the field's new occurrence, before it has an ID


def _field_pairs(w, st: dict, f: str, suffix: str) -> set:
    """code_pairs_v2's own routine (_controls, then _values) for statement st with the target read as field f: the
    pairs (Y, y_id, f, _SLOT, type). A bare process-variable value is read as its matching field (`int_rsp <= tmp_v`
    for int_rsp.ack follows the definitions of tmp_v.ack and of tmp_v as a whole, not those of tmp_v.data)."""
    root = st["root"]
    target = {"root": {**root, "occ": {"el": f, "id": _SLOT}, "type": w.types.get(f.lower(), ""), "bit_index": False}}
    keep, w.best = w.best, {}
    moved = []
    try:
        for ex in st["values"]:
            b = w._bare(ex)
            if b is not None and b["kind"] == "variable" and b["exact"]:
                moved.append((b, b["path"]))
                b["path"] = b["path"] + suffix.lower()
        w._controls(st, target, frozenset())
        w._values(st, target)
        return {k + (t,) for k, t in w.best.items()}
    finally:
        for b, p in moved:
            b["path"] = p
        w.best = keep


def expand_entity(E: dict, scope: str) -> tuple[set, set, list[dict]]:
    """-> (pairs of code_pairs_v2 on the profile as built, added pairs, one log row per expanded statement).
    Appends the new profile rows to E["profile"] in place. Pairs are (Y, y_id, X, x_id, driving type)."""
    V = sys.modules["code_pairs_v2"]
    w = V._CPWriter(E)
    w.scan()
    w.bind()
    base = w.write()
    if scope == "none":
        return base, set(), []
    names = list(E["profile"])
    rows = {(n, r["Occurrence ID"]): r for n, rs in E["profile"].items() for r in rs}
    new, extra, log = {}, [], []                    # new: (field, origin element, origin id) -> row
    for st in w.statements:
        root = st["root"]
        if root["kind"] != "element" or not root["exact"] or root["occ"] is None:
            continue
        R, ri = root["occ"]["el"], root["occ"]["id"]
        fR = _fields(R, names)
        if not fR:
            continue
        on = sorted(p for p in base if p[2] == R and p[3] == ri)
        clocked = any(p[4] == "SEQUENCES" for p in on)
        if scope == "clocked" and not clocked:
            continue
        bare, var = set(), False
        for ex in st["values"]:
            b = w._bare(ex)
            var |= b is not None and b["kind"] == "variable"
            if b is not None and b["kind"] == "element" and b["exact"] and b["occ"] is not None \
                    and _fields(b["occ"]["el"], names):
                bare.add((b["occ"]["el"], b["occ"]["id"]))
        unmatched, differ = [], 0
        for f in fR:
            suf = f[len(R):]
            tkey = (f, R, ri)
            new.setdefault(tkey, _row(rows[(R, ri)]))
            fp = sorted(_field_pairs(w, st, f, suf))
            differ += {(y, yi, t) for y, yi, _x, _xi, t in fp} != {(y, yi, t) for y, yi, _x, _xi, t in on}
            for y, yi, _x, _xi, t in fp:
                if (y, yi) in bare:
                    sf = _match(y + suf, names)
                    if sf is not None:
                        skey = (sf, y, yi)
                        new.setdefault(skey, _row(rows[(y, yi)]))
                        extra.append((("new",) + skey, ("new",) + tkey, t))
                        continue
                    unmatched.append(f"{y}{suf}")
                extra.append((("old", y, yi), ("new",) + tkey, t))
        log.append({"entity": E["entity"], "record": R, "occurrence": ri, "line": rows[(R, ri)]["Occurrence Lines"],
                    "text": rows[(R, ri)]["Line Text"], "clocked": clocked, "fields": len(fR),
                    "value records": sorted(f"{y}#{yi}" for y, yi in bare), "value is a process variable": var,
                    "pairs on the record": [f"{y}#{yi} {t}" for y, yi, _x, _xi, t in on],
                    "fields whose pairs differ from the record's": differ,
                    "source fields not in the closed set": sorted(set(unmatched))})
    idmap, by_el = {}, defaultdict(list)
    for key, r in new.items():
        by_el[key[0]].append((r["Occurrence Lines"], key[1], key[2], key))
    for el, xs in by_el.items():
        nxt = max((r["Occurrence ID"] for r in E["profile"][el]), default=0)
        for *_k, key in sorted(xs):
            nxt += 1
            new[key]["Occurrence ID"] = nxt
            E["profile"][el].append(new[key])
            idmap[key] = nxt

    def end(k):
        return (k[1], k[2]) if k[0] == "old" else (k[1], idmap[k[1:]])
    added = {end(y) + end(x) + (t,) for y, x, t in extra}
    clash = {p[:4] for p in added} & {p[:4] for p in base}
    assert not clash, f"{E['entity']}: an added pair repeats an existing occurrence pair: {sorted(clash)[:3]}"
    return base, added, log


def build_module(m: str, src, out_dir: Path, scope: str) -> dict:
    """build_heldout_code_map.build_module with the expansion between the profile and the merge."""
    CM, RS, V = sys.modules["build_heldout_code_map"], sys.modules["relation_stage"], sys.modules["code_pairs_v2"]
    ents = CM.entities(m, src)            # through the module attribute: final_pipeline.build_all keeps these rows
    recs, log, rebound = {}, [], 0
    for E in ents:
        base, added, lg = expand_entity(E, scope)
        rebound += V.code_pairs(E) != base     # the new rows must bind to no written name
        recs[E["entity"]] = sorted({r for y, yi, x, xi, t in base | added
                                    for r in ((y, yi, t, x, None), (x, xi, RS.MIRROR[t], y, None))}, key=str)
        log += lg
    out_dir = Path(out_dir)
    RS.merge(m, {"prompt_sha": f"{CM.CODE_PAIRS_SHA}_codetags", "records": recs, "functionality": {}},
             ents=ents, out_dir=out_dir, log=lambda *a: None)
    sites = {E["entity"]: {n: {str(r["Occurrence ID"]): r["SITE Tagged"] for r in rs} for n, rs in E["profile"].items()}
             for E in ents}
    (out_dir / CM.SITES_SUBDIR).mkdir(parents=True, exist_ok=True)
    (out_dir / CM.SITES_SUBDIR / f"{m}.json").write_text(json.dumps(sites, indent=1), encoding="utf-8")
    EXPANSIONS[m] = log
    assert rebound == 0, f"{m}: code_pairs_v2 bound a new field row to a written name ({rebound} entities)"
    return {"statements expanded": len(log)}


def build(scope: str, out: Path):
    """The whole final_pipeline build (closed sets, profiles, maps, traced inputs) with the expansion. -> ns, mods, built"""
    assert scope in SCOPES
    ns = FP.setup()
    mods = FP.modules(ns.ea)
    EXPANSIONS.clear()
    orig = ns.CM.build_module
    ns.CM.build_module = lambda m, src, out_dir: build_module(m, src, out_dir, scope)
    try:
        built = FP.build_all(ns, mods, out=out)
    finally:
        ns.CM.build_module = orig
    (out / "expansions.json").write_text(json.dumps(EXPANSIONS, indent=1), encoding="utf-8")
    return ns, mods, built


# ------------------------------------------------------------------------------------------------ comparison ---
def _load(p: Path) -> dict:
    d = json.loads(p.read_text(encoding="utf-8"))
    return {(e["entity"], e["name"]): (a, e) for a in ("ports", "signals") for e in d[a]}


def _triples(e: dict) -> set:
    return {(r["type"], x, a, r.get("guard"), json.dumps(r.get("bits"))) for r in e.get("relationship", [])
            for x in r["targets"] for a in r["at"]}


def _partners(e: dict) -> set:
    return {(r["type"], r.get("guard"), x, p) for r in e.get("relationship", []) for x, ps in r.get("partner_at", {}).items()
            for p in ps}


FOLLOW = ("storage", "kind", "handling", "constant_drivers", "boundary", "configuration")   # merge reads them from the occurrences


def compare_element(old: dict, new: dict, expanded: set) -> dict:
    """What changed in one element, and whether the change keeps the invariants. `expanded`: the names (same entity)
    that gained an occurrence. Invariants: existing occurrences unchanged; every added fact sits on an added occurrence
    or is the far end of a pair whose target gained one; a fact that disappears is only an old fact whose guard text
    was rewritten (the merge writes one guard per element pair, the union over all linked target occurrences); the
    fields merge reads from the occurrences change only on an element that gained one; nothing else changes."""
    oo = [(o["id"], o["line"], o["text"]) for o in old.get("occurrences", [])]
    no = [(o["id"], o["line"], o["text"]) for o in new.get("occurrences", [])]
    add_occ = no[len(oo):]
    added_ids = {i for i, _l, _t in add_occ}
    t_old, t_new = _triples(old), _triples(new)
    gained, lost = t_new - t_old, t_old - t_new
    other = sorted(k for k in set(old) | set(new) if k not in FOLLOW + ("occurrences", "relationship")
                   and old.get(k) != new.get(k))
    follow = {k: [old.get(k), new.get(k)] for k in FOLLOW if old.get(k) != new.get(k)}
    regarded = {(t, x, a, b) for t, x, a, _g, b in gained}
    rewritten = sorted((t, x, a, g_old, next(g for t2, x2, a2, g, b2 in gained if (t2, x2, a2, b2) == (t, x, a, b)))
                       for t, x, a, g_old, b in lost if (t, x, a, b) in regarded)
    unexplained = [f for f in gained if f[2] not in added_ids and f[1] not in expanded]
    return {"occurrences added": [{"id": i, "line": l, "text": t} for i, l, t in add_occ],
            "facts added": sorted(gained, key=str), "guards rewritten": rewritten, "changed": follow,
            "ok": {"old occurrences kept": no[:len(oo)] == oo,
                   "every added fact has a new end": not unexplained,
                   "a lost fact is only a rewritten guard": len(rewritten) == len(lost),
                   "occurrence-read fields change only with a new occurrence": bool(add_occ) or not follow,
                   "other fields unchanged": not other}}


def compare(mods: dict, built: dict) -> dict:
    """Element by element, stored map vs new map, for the 41 modules (+ the 2 design modules, reported apart)."""
    res = {}
    for split, ms in (("tuning", mods["tuning"]), ("heldout", mods["heldout"]), ("design", FP.DESIGN)):
        stored = FP.STORED_MAPS[split]
        new_dir = built["maps"]["tuning" if split == "design" else split]
        rows, n_el, n_field = [], 0, 0
        for m in ms:
            a, b = _load(stored / f"{m}.json"), _load(new_dir / f"{m}.json")
            assert list(a) == list(b), f"{m}: the element list changed"
            grew = defaultdict(set)
            for k in a:
                if len(b[k][1]["occurrences"]) > len(a[k][1]["occurrences"]):
                    grew[k[0]].add(k[1])
            for k in a:
                n_el += 1
                n_field += "." in k[1]
                if a[k][1] == b[k][1]:
                    continue
                c = compare_element(a[k][1], b[k][1], grew[k[0]])
                rows.append({"module": m, "entity": k[0], "element": k[1], "array": a[k][0], **c})
        res[split] = {"modules": len(ms), "elements": n_el, "fields": n_field, "changed": rows}
    return res


# ------------------------------------------------------------------------------------------------ self-tests ---
# Hand-read on 2026-10-09.
#   data/RTL_data/neorv32_cache.vhd 98-108: record ctrl_t (state, buf_req, buf_sync, buf_err, buf_dir, tag, idx, ofs);
#     `signal ctrl, ctrl_nxt : ctrl_t;`. 125 `elsif rising_edge(clk_i) then`, 126 `ctrl <= ctrl_nxt;`.
#   data/RTL_data/neorv32_bus.vhd 734-740 (entity neorv32_bus_amo_rmw): record arbiter_t (state, cmd, rdata, wdata);
#     `signal arbiter, arbiter_nxt : arbiter_t;`. 762 `elsif rising_edge(clk_i) then`, 763 `arbiter <= arbiter_nxt;`.
#   Must not change: neorv32_cache.vhd 76-84 record cache_o_t (cmd_clr, cmd_inv, cmd_new, addr, data, we),
#     `signal cache_o : cache_o_t;`. cache_o is never assigned or read as a whole: every other use names a field
#     (146-151, 194, 219, 225-226, 235-237, 255, 283-290). The record element ctrl itself already had its records.
#   Wider rule (scope "all"), neorv32_bus.vhd 214-234 (entity neorv32_bus_reg; bus_req_t, neorv32_package.vhd 121-135,
#     has field addr): 219 `device_req_o <= req_terminate_c;` in the reset arm (218 `if (rstn_i = '0') then`), 222
#     `device_req_o <= host_req_i;` on the clock edge under 221 `if (host_req_i.stb = '1') then`, 233
#     `device_req_o <= host_req_i;` as a concurrent statement in the other generate branch. So device_req_o.addr is
#     assigned on the edge and off it: storage "mixed", like the record.
CLOCKED_CASES = [("tuning", "neorv32_cache", "neorv32_cache", "ctrl", "ctrl_nxt", 125, 126, "ctrl <= ctrl_nxt;",
                  ["state", "buf_req", "buf_sync", "buf_err", "buf_dir", "tag", "idx", "ofs"]),
                 ("tuning", "neorv32_bus", "neorv32_bus_amo_rmw", "arbiter", "arbiter_nxt", 762, 763,
                  "arbiter <= arbiter_nxt;", ["state", "cmd", "rdata", "wdata"])]
UNCHANGED = ("neorv32_cache", "neorv32_cache", ["cache_o"] + [f"cache_o.{f}" for f in
                                                            ("cmd_clr", "cmd_inv", "cmd_new", "addr", "data", "we")] + ["ctrl"])


def _el(built, split, m, ent, name):
    return _load(built["maps"][split] / f"{m}.json")[(ent, name)][1]


def _at_line(e, line):
    return [o["id"] for o in e["occurrences"] if o["line"] == line]


def _has(e, t, target, ids):
    return any(r["type"] == t and target in r["targets"] and set(ids) & set(r["at"]) for r in e["relationship"])


def selftest_hand_read(built: dict, scope: str, log=print) -> bool:
    ok = True
    for split, m, ent, R, S, edge_ln, ln, text, fields in CLOCKED_CASES:
        stored = _load(FP.STORED_MAPS[split] / f"{m}.json")
        clk = _el(built, split, m, ent, "clk_i")
        clk_ids = _at_line(clk, edge_ln)
        for f in fields:
            e, s = _el(built, split, m, ent, f"{R}.{f}"), _el(built, split, m, ent, f"{S}.{f}")
            old_e, old_s = stored[(ent, f"{R}.{f}")][1], stored[(ent, f"{S}.{f}")][1]
            ids, sids = _at_line(e, ln), _at_line(s, ln)
            got = {"no occurrence on the line before": not _at_line(old_e, ln) and not _at_line(old_s, ln),
                   "one new occurrence each": len(ids) == 1 and len(sids) == 1,
                   "new ID after the old ones": bool(ids) and ids[0] > max(o["id"] for o in old_e["occurrences"]),
                   "line text": all(o["text"] == text for o in e["occurrences"] + s["occurrences"] if o["line"] == ln),
                   "old occurrences kept": e["occurrences"][:len(old_e["occurrences"])] == old_e["occurrences"],
                   "storage edge, kind register": e["storage"] == "edge" and e["kind"] == "register",
                   "CLOCKED_BY clk_i there": _has(e, "CLOCKED_BY", "clk_i", ids),
                   f"COPIES {S}.{f} there": _has(e, "COPIES", f"{S}.{f}", ids),
                   f"{S}.{f} CARRIES {R}.{f} there": _has(s, "CARRIES", f"{R}.{f}", sids),
                   f"clk_i SEQUENCES {R}.{f} at line {edge_ln}": _has(clk, "SEQUENCES", f"{R}.{f}", clk_ids)}
            good = scope != "none" and all(got.values())
            if scope == "none":     # the stored map: the defect is there
                good = e == old_e and not got["storage edge, kind register"] and not got["CLOCKED_BY clk_i there"]
            ok &= good
            bad = [k for k, v in got.items() if not v]
            log(f"   {m}:{ln} {R}.{f}: storage {old_e['storage']} -> {e['storage']}, new occurrence(s) {ids} / "
                f"{S}.{f} {sids}: {'ok' if good else 'FAIL ' + str(bad)}")
    m, ent, names = UNCHANGED
    a, b = _load(FP.STORED_MAPS["tuning"] / f"{m}.json"), _load(built["maps"]["tuning"] / f"{m}.json")
    same = [n for n in names if a[(ent, n)] == b[(ent, n)]]
    ok &= len(same) == len(names)
    log(f"   must not change, {m}: {len(same)}/{len(names)} elements identical ({', '.join(names)})")
    if scope == "all":
        e = _el(built, "tuning", "neorv32_bus", "neorv32_bus_reg", "device_req_o.addr")
        i219, i222, i233 = (_at_line(e, x) for x in (219, 222, 233))
        got = {"storage mixed": e["storage"] == "mixed",
               "RESET_BY rstn_i at 219": _has(e, "RESET_BY", "rstn_i", i219),
               "CLOCKED_BY clk_i at 222": _has(e, "CLOCKED_BY", "clk_i", i222),
               "GATED_BY host_req_i.stb at 222": _has(e, "GATED_BY", "host_req_i.stb", i222),
               "COPIES host_req_i.addr at 222 and 233": _has(e, "COPIES", "host_req_i.addr", i222)
               and _has(e, "COPIES", "host_req_i.addr", i233),
               "constant driver req_terminate_c at 219": any(c["at"] in i219 and c["value"] == "req_terminate_c"
                                                             for c in e["constant_drivers"])}
        good = all(got.values())
        ok &= good
        log(f"   neorv32_bus:219/222/233 device_req_o.addr: storage {e['storage']}: "
            f"{'ok' if good else 'FAIL ' + str([k for k, v in got.items() if not v])}")
        # neorv32_bus.vhd 384-395 (entity neorv32_bus_gateway): 390 `tmp_v.data := tmp_v.data or port_rsp(i).data;`,
        # 391 `tmp_v.ack := tmp_v.ack or port_rsp(i).ack;`, 392 `tmp_v.err := tmp_v.err or port_rsp(i).err;`,
        # 395 `int_rsp <= tmp_v;`. So int_rsp.<f> derives from port_rsp at the one line that builds tmp_v.<f>.
        p = _el(built, "tuning", "neorv32_bus", "neorv32_bus_gateway", "port_rsp")
        for f, ln in (("data", 390), ("ack", 391), ("err", 392)):
            e = _el(built, "tuning", "neorv32_bus", "neorv32_bus_gateway", f"int_rsp.{f}")
            src = sorted(o["line"] for o in p["occurrences"] for r in p["relationship"]
                         if r["type"] == "SOURCES" and f"int_rsp.{f}" in r["targets"] and o["id"] in r["at"])
            good = src == [ln] and _has(e, "DERIVES_FROM", "port_rsp", _at_line(e, 395))
            ok &= good
            log(f"   neorv32_bus:{ln}/395 port_rsp SOURCES int_rsp.{f} at lines {src} (hand-read: [{ln}]): "
                f"{'ok' if good else 'FAIL'}")
    elif scope == "clocked":                # information: only line 222 is expanded, so the field disagrees with its record
        e = _el(built, "tuning", "neorv32_bus", "neorv32_bus_reg", "device_req_o.addr")
        r = _el(built, "tuning", "neorv32_bus", "neorv32_bus_reg", "device_req_o")
        log(f"   (information) neorv32_bus:222 device_req_o.addr storage {e['storage']}, its record device_req_o {r['storage']}")
    return ok


def selftest_pipeline(ns, mods, built, log=print) -> bool:
    """The existing checks, run on the new maps and traced inputs: traced_inputs' own self-test (every occurrence on
    its numbered RTL line, hand-read flow facts) and final_pipeline.line_integrity."""
    ok = ns.TI.selftest(log=lambda *a: None)
    log(f"   traced_inputs.selftest on the new maps: {'ok' if ok else 'FAIL'}")
    li = FP.line_integrity(ns, built, mods)
    for split, v in li.items():
        n, d = map(int, v.split("/"))
        ok &= n == d
        log(f"   line integrity ({split}): {v} occurrences on a numbered line that names the element or its record")
    return ok


def selftest_none(log=print) -> bool:
    """scope "none" goes through this module's code path and must rebuild the stored maps and traced inputs exactly."""
    tmp = OUT / "_check_none"
    try:
        ns, mods, built = build("none", tmp)
        rep = FP.reproducibility(built, mods)
        good = all(a == b for a, b in (v.split("/") for v in rep.values()))
        good &= selftest_hand_read(built, "none", log=lambda *a: None)
        log(f"   scope none, byte for byte: {rep}: {'ok' if good else 'FAIL'}")
        return good
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------------------------ read-outs ---
def built_dirs(scope: str) -> dict:
    out = OUT / scope
    return {"maps": {"tuning": out / "maps" / "tuning", "heldout": out / "maps" / "heldout"},
            "traced_inputs": out / "traced_inputs", "closed_sets": out / "closed_sets"}


def summarize(cmp: dict, built: dict) -> dict:
    """Counts per split over the 41 modules: changed elements by kind of change, transitions, record types added."""
    out = {}
    for split in ("tuning", "heldout"):
        r = cmp[split]
        rows = r["changed"]
        sites = {}
        for m in {x["module"] for x in rows}:
            sites[m] = json.loads((built["maps"][split] / "_sites" / f"{m}.json").read_text(encoding="utf-8"))
        kind = Counter()
        for x in rows:
            ids = [o["id"] for o in x["occurrences added"]]
            tags = {t for i in ids for t in sites[x["module"]][x["entity"]][x["element"]][str(i)]}
            x["side"] = ("assigned (whole-record target)" if tags & {"LHS_PROC", "LHS_CONC"} else
                         "read (whole-record value)" if ids else "far end only (gains a driving record)")
            kind[x["side"]] += 1
        st = Counter(tuple(x["changed"]["storage"]) for x in rows if "storage" in x["changed"])
        bd = Counter((x["changed"]["boundary"][0].get("drive"), x["changed"]["boundary"][1].get("drive"))
                     for x in rows if "boundary" in x["changed"])
        hd = Counter(x["element"] for x in rows if "handling" in x["changed"])
        cd = sum("constant_drivers" in x["changed"] for x in rows)
        cf = sum("configuration" in x["changed"] for x in rows)
        types = Counter(f[0] for x in rows for f in x["facts added"])
        inv = Counter(k for x in rows for k, v in x["ok"].items() if not v)
        out[split] = {"modules": r["modules"], "elements": r["elements"], "fields": r["fields"],
                      "changed elements": len(rows), "modules with a change": len({x["module"] for x in rows}),
                      "by kind of change": dict(kind),
                      "storage (old -> new)": {f"{a} -> {b}": n for (a, b), n in sorted(st.items())},
                      "boundary drive (old -> new)": {f"{a} -> {b}": n for (a, b), n in sorted(bd.items())},
                      "handling changed": len(hd), "constant drivers added": cd, "configuration added": cf,
                      "record facts added (type, one per target x occurrence)": dict(types.most_common()),
                      "old facts whose guard text was rewritten": sum(len(x["guards rewritten"]) for x in rows),
                      "invariant failures": dict(inv)}
    return out


def flow_changes(mods: dict, built: dict) -> dict:
    """Traced inputs (what the generator reads): new vs assetgen_meta/traced_inputs_v2, per split."""
    out = {}
    for split in ("tuning", "heldout"):
        txt = flow = 0
        stores_add, exits_add, lists = Counter(), Counter(), Counter()
        for m in mods[split]:
            a = (FP.TRACED_V2 / split / f"{m}.txt").read_text(encoding="utf-8")
            b = (built["traced_inputs"] / split / f"{m}.txt").read_text(encoding="utf-8")
            txt += a != b
            fa = json.loads((FP.TRACED_V2 / split / "_flow" / f"{m}.json").read_text(encoding="utf-8"))
            fb = json.loads((built["traced_inputs"] / split / "_flow" / f"{m}.json").read_text(encoding="utf-8"))
            flow += fa != fb
            for s in set(fa["paths"]) | set(fb["paths"]):
                pa, pb = fa["paths"].get(s, {}), fb["paths"].get(s, {})
                stores_add["added"] += len(set(pb.get("stores", [])) - set(pa.get("stores", [])))
                stores_add["removed"] += len(set(pa.get("stores", [])) - set(pb.get("stores", [])))
                exits_add["added"] += len(set(pb.get("exits", [])) - set(pa.get("exits", [])))
                exits_add["removed"] += len(set(pa.get("exits", [])) - set(pb.get("exits", [])))
            for k in ("no_local_use", "not_on_an_input_path", "self_updating", "clock_reset", "records"):
                lists[f"{k}: left"] += len(set(fa[k]) - set(fb[k]))
                lists[f"{k}: entered"] += len(set(fb[k]) - set(fa[k]))
            for k in ("driving_without_receiving", "receiving_without_driving"):
                lists[f"unmatched {k.split('_')[0]} records (new map)"] += len(fb["symmetry"][k])
        out[split] = {"modules": len(mods[split]), "traced input text changed": txt, "flow graph changed": flow,
                      "(path source, stored element) pairs": dict(stores_add), "(path source, exit) pairs": dict(exits_add),
                      **{k: v for k, v in sorted(lists.items())}}
    return out


def recheck_runs(ns, mods: dict, built: dict, version: str = FP.FINAL_VERSION) -> dict:
    """The final runs' citations (gpt-5.4 output, unchanged) checked by trace_check against the stored map and against
    the new map. Code only: it says what the check would report, not what the model would cite with the new map."""
    import traced_inputs as TI
    tc = ns.tc
    keep = (TI.MAPS, TI.PARSED, TI._PORT_DIRS, tc.INPUTS)
    out = {}
    try:
        for split, stem in (("tuning", "assets_tuning18"), ("heldout", "assets_heldout26")):
            runs = {}
            for tag, maps, inputs in (("stored", [FP.STORED_MAPS[split]] + ([FP.STORED_MAPS["design"]] if split == "tuning" else []),
                                       FP.TRACED_V2),
                                      ("new", [built["maps"][split]], built["traced_inputs"])):
                TI.MAPS = {split: maps}
                TI.PARSED = {"tuning": built["closed_sets"] / "tuning", "heldout": built["closed_sets"] / "heldout"}
                TI._PORT_DIRS = None
                tc.INPUTS = inputs
                runs[tag] = tc.run(version, (0, 1, 2), split=split, stem=stem, write=False)
            trans, changed_rows, n = Counter(), [], 0
            for k in (0, 1, 2):
                for m in mods[split]:
                    for kind in ("refs", "influence"):
                        A = runs["stored"][k]["checks"][m][kind]
                        B = runs["new"][k]["checks"][m][kind]
                        assert len(A) == len(B)
                        for x, y in zip(A, B):
                            n += 1
                            if x["status"] != y["status"]:
                                trans[f"{kind}: {x['status']} -> {y['status']}"] += 1
                                changed_rows.append({"run": k, "module": m, "kind": kind, "element": x["element"],
                                                     "role": x["role"], "occurrence": x["occurrence"], "edge": x["edge"],
                                                     "stored": x["status"], "new": y["status"], "why new": y.get("why")})
            out[split] = {"citations checked (3 runs, refs + influence points)": n, "status changed": len(changed_rows),
                          "transitions": dict(trans), "rows": changed_rows}
    finally:
        TI.MAPS, TI.PARSED, TI._PORT_DIRS, tc.INPUTS = keep
    return out


def listings_on_changed(ns, mods: dict, cmp: dict, version: str = FP.FINAL_VERSION) -> dict:
    """How many listed elements (structural assets of the final runs) are elements this version changes."""
    out = {}
    for split in ("tuning", "heldout"):
        changed = {(x["module"], x["entity"], x["element"]) for x in cmp[split]["changed"]}
        storage = {(x["module"], x["entity"], x["element"]) for x in cmp[split]["changed"] if "storage" in x["changed"]}
        n = on = on_st = 0
        for d in FP.run_dirs(split):
            for m in mods[split]:
                obj = json.loads((d / "_nested" / f"{m}.json").read_text(encoding="utf-8"))
                for c in obj.get("conceptual assets", []) or []:
                    for s in c.get("related structural assets", []) or []:
                        if not isinstance(s, dict):
                            continue
                        key = (m, s.get("entity"), s.get("asset rtl"))
                        n += 1
                        on += key in changed
                        on_st += key in storage
        out[split] = {"listed structural assets (3 runs)": n, "on a changed element": on, "on an element whose storage changed": on_st}
    return out


def relation_accuracy_both(built: dict) -> dict:
    """Typed relationship accuracy against the gold sample (120 tuning occurrences), stored maps vs new maps."""
    return {"stored": FP.relation_accuracy({"maps": {"tuning": FP.STORED_MAPS["tuning"]}}),
            "new": FP.relation_accuracy(built)}


def readout(scope: str) -> dict:
    """All read-outs from a built folder (no rebuild). Writes final/record_fields/<scope>/readout.json."""
    sys.stdout.reconfigure(encoding="utf-8")
    ns = FP.setup()
    mods = FP.modules(ns.ea)
    built = built_dirs(scope)
    cmp = compare(mods, built)
    res = {"scope": scope, "summary": summarize(cmp, built), "design maps changed": len(cmp["design"]["changed"]),
           "flow": flow_changes(mods, built), "recheck": recheck_runs(ns, mods, built),
           "listings": listings_on_changed(ns, mods, cmp), "relation accuracy": relation_accuracy_both(built)}
    (OUT / scope / "changes.json").write_text(json.dumps(cmp, indent=1), encoding="utf-8")
    (OUT / scope / "readout.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    write_lists(scope, cmp)
    return res


def write_lists(scope: str, cmp: dict) -> None:
    """changes.csv (one row per changed element of the 41 modules) and statements.csv (one row per expanded statement)."""
    import csv
    other = OUT / ("clocked" if scope == "all" else "all") / "changes.json"
    also = None
    if other.exists():
        oc = json.loads(other.read_text(encoding="utf-8"))
        also = {(s, x["module"], x["entity"], x["element"]) for s in ("tuning", "heldout") for x in oc[s]["changed"]}
    with open(OUT / scope / "changes.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["split", "module", "entity", "element", "array", "change", "storage old", "storage new", "kind old",
                    "kind new", "drive old", "drive new", "handling old", "handling new", "occurrences added",
                    "facts added (type target @ occurrence)", "guards rewritten"]
                   + ([f"also changed in scope {'clocked' if scope == 'all' else 'all'}"] if also is not None else []))
        for split in ("tuning", "heldout"):
            for x in cmp[split]["changed"]:
                ch = x["changed"]
                g = lambda k, i: ch[k][i] if k in ch else ""
                drv = lambda i: (ch["boundary"][i] or {}).get("drive", "") if "boundary" in ch else ""
                hd = lambda i: " ".join(ch["handling"][i] or []) if "handling" in ch else ""
                w.writerow([split, x["module"], x["entity"], x["element"], x["array"], x.get("side", ""),
                            g("storage", 0), g("storage", 1), g("kind", 0), g("kind", 1), drv(0), drv(1), hd(0), hd(1),
                            "; ".join(f"{o['id']}@{o['line']}" for o in x["occurrences added"]),
                            "; ".join(f"{t} {tg} @{a}" for t, tg, a, _g, _b in x["facts added"]),
                            len(x["guards rewritten"])]
                           + (["yes" if (split, x["module"], x["entity"], x["element"]) in also else "no"] if also is not None else []))
    exp = json.loads((OUT / scope / "expansions.json").read_text(encoding="utf-8"))
    with open(OUT / scope / "statements.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["module", "entity", "record", "occurrence", "line", "text", "clocked", "fields", "value records",
                    "value is a process variable", "pairs on the record"])
        for m, rows in sorted(exp.items()):
            for r in rows:
                w.writerow([m, r["entity"], r["record"], r["occurrence"], r["line"], r["text"], r["clocked"], r["fields"],
                            " ".join(r["value records"]), r["value is a process variable"], "; ".join(r["pairs on the record"])])


# ------------------------------------------------------------------------------------------------ main ---
def main(scope: str = "all") -> dict:
    sys.stdout.reconfigure(encoding="utf-8")
    assert scope in ("clocked", "all"), "scope none is the self-test, not a version"
    t0 = time.time()
    ok = selftest_none()
    out = OUT / scope
    ns, mods, built = build(scope, out)
    ok &= ns.V.self_test()
    ok &= selftest_hand_read(built, scope)
    ok &= selftest_pipeline(ns, mods, built)
    if not ok:
        print("SELF-TEST FAILED: nothing reported from this run")
        return {"ok": False}
    res = readout(scope)
    print(json.dumps({k: res[k] for k in ("summary", "design maps changed", "flow", "listings", "relation accuracy")}, indent=1))
    print(json.dumps({s: {k: v for k, v in r.items() if k != "rows"} for s, r in res["recheck"].items()}, indent=1))
    print(f"done in {time.time() - t0:.0f} s; wrote {out.relative_to(ROOT)}")
    return {"ok": True, **res}


if __name__ == "__main__":
    sc = sys.argv[sys.argv.index("--scope") + 1] if "--scope" in sys.argv else "all"
    main(sc)
