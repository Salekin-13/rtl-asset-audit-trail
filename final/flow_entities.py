"""The flow graph per entity: a NEW version of the FLOW GRAPH section of the traced inputs. No model call.

The defect (found 2026-10-09 while building final/record_fields.py). assetgen_meta/traced_inputs.flow_graph(d) indexes
the map's elements by name only: `els = {e["name"]: e ...}`. A file can declare several entities, and they can share
names (clk_i, rstn_i, a port name a sub-unit repeats). Then the last entity's element replaces the others:
  - their relationship records are never read, so the MAP CHECK reports a record as having no partner although the
    map holds both ends (neorv32_cache.vhd:125-126: clk_i of entity neorv32_cache SEQUENCES ctrl; the flow graph kept
    the clk_i of neorv32_cache_memory, so "ctrl CLOCKED_BY clk_i" was listed as unmatched);
  - the lists (clock or reset, paths, stores, exits, ...) mix entities: a path can call an element stored or an
    output because another entity's element of the same name is.

The fix. Every record of the map targets an element of its own entity (checked: 0 targets outside their entity in the
stored maps). So the flow graph of a file is the flow graph of each entity's part of the map, computed by the
unchanged traced_inputs.flow_graph. In a file with one entity nothing changes, byte for byte. In a file with several
entities, every FLOW GRAPH line carries an "entity" key, as the map's element lines already do, and the flow JSON is
{"entities": {entity: <the one-entity flow graph>}}. Names stay bare (final prompt, line 141). Everything outside the
FLOW GRAPH section is the stored traced input, byte for byte.

Outputs  final/flow_entities/<maps>/<split>/<module>.txt and <split>/_flow/<module>.json, for
           maps "stored"              the maps every earlier run read (as assetgen_meta/traced_inputs_v2)
           maps "record_fields_all"   final/record_fields/all/maps (the record-field fix of 2026-10-09)
         final/flow_entities/<maps>/measure.json    old vs new flow graph, per module and in total
Run with the conda Python:  %USERPROFILE%\\miniconda3\\python.exe final/flow_entities.py
The self-tests run first; nothing is written if one fails.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import final_pipeline as FP          # noqa: E402

OUT = HERE / "flow_entities"
HEAD = "\n\n=== RELATIONSHIP MAP: FLOW GRAPH (computed by a program from the records above) ===\n"
TAIL = "\n\nIdentify the primary security assets for"
LISTS = ("clock_reset", "records", "self_updating", "no_local_use", "not_on_an_input_path")
ENDS = ("stores", "exits", "sub-block", "influence")


# ------------------------------------------------------------------------------------------------ the fix ---
def entities(d: dict) -> list[str]:
    return list(dict.fromkeys(e.get("entity", "") for a in ("ports", "signals") for e in d.get(a, [])))


def part(d: dict, ent: str) -> dict:
    return {a: [e for e in d.get(a, []) if e.get("entity", "") == ent] for a in ("ports", "signals")}


def flow_graph(TI, d: dict) -> dict:
    ents = entities(d)
    if len(ents) <= 1:
        return TI.flow_graph(d)
    return {"entities": {ent: TI.flow_graph(part(d, ent)) for ent in ents}}


def entity_lines(TI, fg: dict, ent: str | None) -> list[str]:
    """traced_inputs.flow_lines, with {"entity": ent} first in every JSON object when ent is given (ent None gives the
    stored lines exactly; selftest checks it on every flow graph)."""
    pre = {"entity": ent} if ent is not None else {}
    s = fg["symmetry"]
    out = [f"MAP CHECK {json.dumps({**pre, 'elements': fg['elements'], 'driving records': s['driving'], 'receiving records': s['receiving'], 'driving without receiving': len(s['driving_without_receiving']), 'receiving without driving': len(s['receiving_without_driving'])}, separators=(',', ':'))}"]
    for k in ("driving_without_receiving", "receiving_without_driving"):
        if s[k]:
            out += TI._chunks(pre, k.replace("_", " "), s[k])
    out += TI._chunks(pre, "clock or reset (drive SEQUENCES / RESETS)", fg["clock_reset"])
    out += TI._chunks(pre, "whole records (their fields carry the records)", fg["records"])
    out += TI._chunks(pre, "self-updating (X is computed from X: counters, state registers)", fg["self_updating"])
    for src, p in sorted(fg["paths"].items()):
        if not any(p[k] for k in ("stores", "exits", "sub-block")):
            continue
        out.append(f"PATH {json.dumps({**pre, 'from': src, 'source': p['kind']}, separators=(',', ':'))}")
        for k in ENDS:
            if p[k]:
                out += TI._chunks(pre, k, p[k])
    for src, xs in sorted(fg["control_only_inputs"].items()):
        out.append("CONTROL-ONLY " + json.dumps({**pre, "from": src, "source": fg["paths"][src]["kind"],
                                                 "gates or selects": xs[:40] + (["..."] if len(xs) > 40 else [])}, separators=(",", ":")))
    out += TI._chunks(pre, "no local use (no relationship, connection or constant driver)", fg["no_local_use"])
    out += TI._chunks(pre, "not reached from an input or constant-valued element", fg["not_on_an_input_path"])
    return out


def flow_lines(TI, fg: dict) -> list[str]:
    if "entities" not in fg:
        return entity_lines(TI, fg, None)
    return [l for ent, f in fg["entities"].items() for l in entity_lines(TI, f, ent)]


def text(TI, split: str, m: str) -> tuple[str, dict | None]:
    """The traced input with its FLOW GRAPH section recomputed per entity; every other byte as traced_inputs.text."""
    body, _old = TI.text(split, m)
    d = TI.load_map(split, m)
    if d is None:
        return body, None
    head, rest = body.split(HEAD, 1)
    _flow, tail = rest.split(TAIL, 1)
    fg = flow_graph(TI, d)
    return head + HEAD + "\n".join(flow_lines(TI, fg)) + TAIL + tail, fg


# ------------------------------------------------------------------------------------------------ measurement ---
def _per_entity(fg: dict, ent_of_single: str | None = None) -> dict:
    return fg["entities"] if "entities" in fg else {ent_of_single: fg}


def compare(old: dict, new: dict, d: dict) -> dict:
    """Old flow graph (name-keyed) vs the per-entity one, keyed (entity, name). In the old graph each name stands for
    the element that survived in its dict: old["entity_of"][name]."""
    ents = entities(d)
    newe = _per_entity(new, ents[0] if ents else None)
    surv = old["entity_of"]
    res = Counter()
    rows = defaultdict(list)
    for kind in ("driving_without_receiving", "receiving_without_driving"):
        o = set(old["symmetry"][kind])
        n = {(e, s) for e, f in newe.items() for s in f["symmetry"][kind]}
        n_names = {s for _e, s in n}
        res[f"{kind}: stored"] += len(o)
        res[f"{kind}: false (both ends in the map; name collision)"] += len(o - n_names)
        res[f"{kind}: still unmatched"] += len(o & n_names)
        res[f"{kind}: hidden before (an element the name lookup dropped)"] += len({x for x in n if x[1] not in o})
        rows[kind] += [{"record": s, "verdict": "false"} for s in sorted(o - n_names)]
        rows[kind] += [{"record": s, "entity": e, "verdict": "still unmatched" if s in o else "hidden before"}
                       for e, s in sorted(n)]
    op = {(surv.get(s, ""), s): p for s, p in old["paths"].items()}
    np_ = {(e, s): p for e, f in newe.items() for s, p in f["paths"].items()}
    shown = lambda p: any(p[k] for k in ("stores", "exits", "sub-block"))
    res["paths: stored"] += sum(shown(p) for p in op.values())
    res["paths: new"] += sum(shown(p) for p in np_.values())
    for k in set(op) | set(np_):
        a, b = op.get(k), np_.get(k)
        if a is None or not shown(a):
            if b is not None and shown(b):
                res["paths: added (its source was dropped by the name lookup)"] += 1
                rows["paths"].append({"entity": k[0], "from": k[1], "change": "added"})
            continue
        if b is None or not shown(b):
            res["paths: removed"] += 1
            rows["paths"].append({"entity": k[0], "from": k[1], "change": "removed"})
            continue
        diff = {e: {"added": sorted(set(b[e]) - set(a[e])), "removed": sorted(set(a[e]) - set(b[e]))} for e in ENDS
                if set(a[e]) != set(b[e])}
        if diff or a["kind"] != b["kind"]:
            res["paths: content changed"] += 1
            rows["paths"].append({"entity": k[0], "from": k[1], "change": "content", "diff": diff})
            for e, v in diff.items():
                res[f"path {e}: entries added"] += len(v["added"])
                res[f"path {e}: entries removed"] += len(v["removed"])
        else:
            res["paths: unchanged"] += 1
    for k in LISTS:
        o = {(surv.get(n, ""), n) for n in old[k]}
        n = {(e, x) for e, f in newe.items() for x in f[k]}
        res[f"{k}: entered"] += len(n - o)
        res[f"{k}: left"] += len(o - n)
        if n != o:
            rows[k] += [{"entity": e, "element": x, "change": "entered"} for e, x in sorted(n - o)] + \
                       [{"entity": e, "element": x, "change": "left"} for e, x in sorted(o - n)]
    return {"counts": dict(res), "rows": dict(rows)}


# ------------------------------------------------------------------------------------------------ self-tests ---
def _fe(fg, ent):
    return fg["entities"][ent] if "entities" in fg else fg


def selftest(TI, log=print) -> bool:
    ok = True
    # (1) the copy of flow_lines: with no entity it gives the stored lines, for every flow graph of every map
    n = same = 0
    for split in ("tuning", "heldout"):
        for m in TI.modules(split):
            d = TI.load_map(split, m)
            if d is None:
                continue
            fgs = [TI.flow_graph(d)] + [TI.flow_graph(part(d, e)) for e in entities(d)]
            for fg in fgs:
                n += 1
                same += entity_lines(TI, fg, None) == TI.flow_lines(fg)
    ok &= same == n
    log(f"   flow_lines copy, no entity key: {same}/{n} flow graphs give the stored lines: {'ok' if same == n else 'FAIL'}")
    # (2) hand-read, data/RTL_data/neorv32_cache.vhd 125 `elsif rising_edge(clk_i) then`, 126 `ctrl <= ctrl_nxt;` in
    #     entity neorv32_cache (lines 21-294); entity neorv32_cache_memory (314-) has its own clk_i (line 396
    #     `if rising_edge(clk_i) then`, 398 `tag_mem(...) <= acc_tag;`).
    d = TI.load_map("tuning", "neorv32_cache")
    old, new = TI.flow_graph(d), flow_graph(TI, d)
    c, cm = _fe(new, "neorv32_cache"), _fe(new, "neorv32_cache_memory")
    got = {"stored graph lists ctrl CLOCKED_BY clk_i as unmatched": "ctrl CLOCKED_BY clk_i" in old["symmetry"]["receiving_without_driving"],
           "stored graph keeps the clk_i of neorv32_cache_memory": old["entity_of"].get("clk_i") == "neorv32_cache_memory",
           "new: not unmatched in neorv32_cache": "ctrl CLOCKED_BY clk_i" not in c["symmetry"]["receiving_without_driving"],
           "new: clk_i is a clock of neorv32_cache": "clk_i" in c["clock_reset"],
           "new: clk_i is a clock of neorv32_cache_memory": "clk_i" in cm["clock_reset"],
           "new: tag_mem CLOCKED_BY clk_i matched in neorv32_cache_memory":
               "tag_mem CLOCKED_BY clk_i" not in cm["symmetry"]["receiving_without_driving"]}
    good = all(got.values())
    ok &= good
    log(f"   neorv32_cache:125-126 and 396-398: {'ok' if good else 'FAIL ' + str([k for k, v in got.items() if not v])}")
    # (3) hand-read, neorv32_cache.vhd 412 `if rising_edge(clk_i) then`, 414 `data_mem_b0(...) <= wdata_i(7 downto 0);`,
    #     425 `rdata_o( 7 downto  0) <= data_mem_b0(...);`: in neorv32_cache_memory the input wdata_i reaches the stored
    #     data_mem_b0 and the output rdata_o.
    p = cm["paths"].get("wdata_i", {})
    good = "data_mem_b0" in p.get("stores", []) and "rdata_o" in p.get("exits", [])
    ok &= good
    log(f"   neorv32_cache:412-425 wdata_i -> stores data_mem_b0, exits rdata_o: {'ok' if good else 'FAIL'}")
    # (4) hand-read, data/RTL_heldout/neorv32_pwm.vhd: entity neorv32_pwm (22-) declares the input clkgen_i (32) and
    #     wires it into its sub-unit, 95 `neorv32_pwm_channel_inst: neorv32_pwm_channel`, 103 `clkgen_i => clkgen_i,`.
    #     Entity neorv32_pwm_channel (142-) has its own input clkgen_i (150), read in a condition (214).
    d = TI.load_map("heldout", "neorv32_pwm")
    old, new = TI.flow_graph(d), flow_graph(TI, d)
    node = "neorv32_pwm_channel_inst.clkgen_i"
    got = {"stored graph keeps the channel's clkgen_i": old["entity_of"].get("clkgen_i") == "neorv32_pwm_channel",
           "stored graph: no path reaches the sub-unit input": not any(node in q["sub-block"] for q in old["paths"].values()),
           "new: neorv32_pwm clkgen_i reaches it": node in _fe(new, "neorv32_pwm")["paths"].get("clkgen_i", {}).get("sub-block", [])}
    good = all(got.values())
    ok &= good
    log(f"   neorv32_pwm:95-103 clkgen_i -> {node}: {'ok' if good else 'FAIL ' + str([k for k, v in got.items() if not v])}")
    # (5) hand-read, data/RTL_data/neorv32_bus.vhd, entity neorv32_bus_switch (17-): 38 `signal state, state_nxt :
    #     state_t;`, 55 `state <= state_nxt;`, 77 `state_nxt <= state;`. state_nxt is driven by state, so it is not a
    #     constant-valued source. The stored graph read state from another entity, missed line 77 and made it one.
    d = TI.load_map("tuning", "neorv32_bus")
    old, new = TI.flow_graph(d), flow_graph(TI, d)
    sw = _fe(new, "neorv32_bus_switch")
    got = {"stored graph: state_nxt is a constant-valued source": old["paths"].get("state_nxt", {}).get("kind") == "constant-valued",
           "stored graph keeps another entity's state": old["entity_of"].get("state") != "neorv32_bus_switch",
           "new: state_nxt is not a source": "state_nxt" not in sw["paths"]}
    good = all(got.values())
    ok &= good
    log(f"   neorv32_bus:55/77 state_nxt not constant-valued in neorv32_bus_switch: "
        f"{'ok' if good else 'FAIL ' + str([k for k, v in got.items() if not v])}")
    # (6) hand-read, neorv32_bus.vhd 734-740: the record arbiter is declared only in entity neorv32_bus_amo_rmw
    #     (architecture from 730). The stored graph listed arbiter.state as an influence on paths of
    #     neorv32_bus_amo_rvs, whose own sys_req_o / core_rsp_o fields share names with amo_rmw's.
    rvs = _fe(new, "neorv32_bus_amo_rvs")
    got = {"stored graph: an influence arbiter.state on a path": any("arbiter.state" in q["influence"] for q in old["paths"].values()),
           "new: no amo_rvs path lists arbiter.state": not any("arbiter.state" in q["influence"] for q in rvs["paths"].values()),
           "new: arbiter.state is an influence in amo_rmw": any("arbiter.state" in q["influence"]
                                                               for q in _fe(new, "neorv32_bus_amo_rmw")["paths"].values())}
    good = all(got.values())
    ok &= good
    log(f"   neorv32_bus:734-740 arbiter.state influences only amo_rmw paths: "
        f"{'ok' if good else 'FAIL ' + str([k for k, v in got.items() if not v])}")
    return ok


# ------------------------------------------------------------------------------------------------ build ---
def build(TI, tag: str, maps: dict | None, log=print) -> dict:
    """Write the new traced inputs for every module traced_inputs builds (18 tuning files, 26 held-out), with the maps
    given ({split: [map dirs]}; None = the stored maps). Returns the measurement."""
    keep = TI.MAPS
    if maps is not None:
        TI.MAPS = maps
    out, meas = OUT / tag, {}
    try:
        for split in ("tuning", "heldout"):
            (out / split / "_flow").mkdir(parents=True, exist_ok=True)
            for m in TI.modules(split):
                t, fg = text(TI, split, m)
                (out / split / f"{m}.txt").write_text(t, encoding="utf-8")
                if fg is not None:
                    (out / split / "_flow" / f"{m}.json").write_text(json.dumps(fg, indent=1), encoding="utf-8")
                    d = TI.load_map(split, m)
                    meas[f"{split}/{m}"] = {"entities": len(entities(d)), "text": t, **compare(TI.flow_graph(d), fg, d)}
                    meas[f"{split}/{m}"]["old_text"] = TI.text(split, m)[0]
    finally:
        TI.MAPS = keep
    return meas


def summarize(meas: dict, scored: dict) -> dict:
    """Totals over the 41 scored modules per split, and over the 2 design modules apart."""
    out = {}
    groups = {"tuning": [f"tuning/{m}" for m in scored["tuning"]], "heldout": [f"heldout/{m}" for m in scored["heldout"]],
              "design": [f"tuning/{m}" for m in FP.DESIGN]}
    for g, keys in groups.items():
        c = Counter()
        for k in keys:
            c.update(meas[k]["counts"])
        multi = [k.split("/")[1] for k in keys if meas[k]["entities"] > 1]
        changed = [k.split("/")[1] for k in keys if meas[k]["text"] != meas[k]["old_text"]]
        a = sum(len(meas[k]["old_text"]) for k in keys)
        b = sum(len(meas[k]["text"]) for k in keys)
        out[g] = {"modules": len(keys), "multi-entity modules": multi, "traced input text changed": changed,
                  "characters": [a, b], **{k: v for k, v in sorted(c.items())}}
    return out


def main() -> dict:
    sys.stdout.reconfigure(encoding="utf-8")
    ns = FP.setup()
    TI = ns.TI
    TI.MAPS = {"tuning": [FP.STORED_MAPS["tuning"], FP.STORED_MAPS["design"]], "heldout": [FP.STORED_MAPS["heldout"]]}
    TI.PARSED = {"tuning": ROOT / "data/parsed_tuning18", "heldout": ROOT / "data/parsed_heldout26"}
    TI._PORT_DIRS = None
    ok = selftest(TI)
    # (5) the stored traced inputs are what traced_inputs.text gives here (so the splice starts from them)
    n = same = 0
    for split in ("tuning", "heldout"):
        for m in TI.modules(split):
            n += 1
            same += TI.text(split, m)[0] == (FP.TRACED_V2 / split / f"{m}.txt").read_text(encoding="utf-8")
    ok &= same == n
    print(f"   traced_inputs.text equals assetgen_meta/traced_inputs_v2: {same}/{n}: {'ok' if same == n else 'FAIL'}")
    if not ok:
        print("SELF-TEST FAILED: nothing written")
        return {"ok": False}
    scored = FP.modules(ns.ea)
    res = {}
    for tag, maps in (("stored", None),
                      ("record_fields_all", {"tuning": [HERE / "record_fields/all/maps/tuning"],
                                             "heldout": [HERE / "record_fields/all/maps/heldout"]})):
        if maps is not None and not maps["tuning"][0].exists():
            print(f"{tag}: {maps['tuning'][0]} is missing; run final/record_fields.py first")
            continue
        meas = build(TI, tag, maps)
        # (6) single-entity modules: text and flow JSON byte for byte as the input they replace
        ref = FP.TRACED_V2 if maps is None else HERE / "record_fields/all/traced_inputs"
        n = same = 0
        for k, v in meas.items():
            split, m = k.split("/")
            if v["entities"] > 1 or not (ref / split / f"{m}.txt").exists():
                continue
            n += 1
            same += (OUT / tag / split / f"{m}.txt").read_bytes() == (ref / split / f"{m}.txt").read_bytes() and \
                (OUT / tag / split / "_flow" / f"{m}.json").read_bytes() == (ref / split / "_flow" / f"{m}.json").read_bytes()
        good = same == n
        longest = max(len(l) for v in meas.values() for l in v["text"].split("\n"))
        good &= longest <= TI.MAXLEN
        print(f"   {tag}: single-entity modules byte for byte: {same}/{n}; longest line {longest} (limit {TI.MAXLEN}): "
              f"{'ok' if good else 'FAIL'}")
        s = summarize(meas, scored)
        rows = {k: v["rows"] for k, v in meas.items() if v["entities"] > 1}
        (OUT / tag / "measure.json").write_text(json.dumps({"summary": s, "rows": rows}, indent=1), encoding="utf-8")
        res[tag] = {"ok": good, "summary": s}
        print(tag, json.dumps(s, indent=1))
    return res


if __name__ == "__main__":
    main()
