# -*- coding: utf-8 -*-
"""
kgctl.py — DSH 侧的命令行接口（不需要写任何 agent 循环）

DSH 的工作流就三条命令：

    python tools/kgctl.py status                 # 有没有新请求
    python tools/kgctl.py context                # 打印设计上下文（读它来出 patch）
    python tools/kgctl.py write-patch p.json     # 把 patch 放回 inbox

可选：
    python tools/kgctl.py validate p.json        # 只校验不写入
    python tools/kgctl.py clear                  # 清空 inbox/outbox
"""
import os, sys, json, glob, datetime, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DSH = os.path.join(ROOT, ".dsh")
OUT = os.path.join(DSH, "outbox")
IN = os.path.join(DSH, "inbox")

VALID_REL = {"improves", "replaces", "combines", "requires", "fails_when", "uses"}
KINDS = {"stat", "near", "recon", "prob", "sup", "misc", "design"}


def p(*a):
    return os.path.join(*a)


def load_json(fp):
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def cmd_status():
    ctx = p(OUT, "context.json")
    pat = p(IN, "patch.json")
    print("root:", ROOT)
    for label, fp in (("context", ctx), ("patch", pat)):
        if os.path.exists(fp):
            t = datetime.datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {label:8s} {os.path.getsize(fp):7d} B  {t}")
        else:
            print(f"  {label:8s} (none)")
    if os.path.exists(ctx):
        d = load_json(ctx)
        print("\npending request:", d.get("ts"), d.get("request", ""))
        print("  L nodes:", len(d.get("L", {}).get("nodes", [])),
              " R nodes:", len(d.get("R", {}).get("nodes", [])))
    log = p(DSH, "request.log")
    if os.path.exists(log):
        print("\nlast requests:")
        with open(log, encoding="utf-8") as f:
            for line in f.readlines()[-5:]:
                print("  " + line.rstrip())
    return 0


def cmd_context():
    fp = p(OUT, "context.md")
    if not os.path.exists(fp):
        fj = p(OUT, "context.json")
        if not os.path.exists(fj):
            print("no context. 先在页面点『发送上下文给 DSH』。")
            return 2
        d = load_json(fj)
        print(json.dumps(d, ensure_ascii=False, indent=2)[:20000])
        return 0
    with open(fp, encoding="utf-8") as f:
        sys.stdout.write(f.read())
    return 0


def validate(patch):
    ops = patch.get("ops", [])
    gaps = patch.get("gaps", {})
    errs, warns = [], []
    for i, op in enumerate(ops):
        k = op.get("op")
        if k == "add_node":
            if not op.get("id") or not op.get("title"):
                errs.append(f"ops[{i}] add_node 缺 id/title")
            if (op.get("pane") or "L") == "L" and not (op.get("fails") or []):
                errs.append(f"ops[{i}] add_node {op.get('id')} 缺 fails：不允许「不会坏」的技术")
            if op.get("kind") and op["kind"] not in KINDS:
                warns.append(f"ops[{i}] kind 未知：{op['kind']}")
        elif k == "add_edge":
            if op.get("rel") not in VALID_REL:
                errs.append(f"ops[{i}] rel 非法：{op.get('rel')}")
            if op.get("rel") == "replaces" and not str(op.get("note", "")).strip():
                errs.append(f"ops[{i}] replaces 缺 note（替代条件）")
        elif k == "annotate":
            if op.get("field") not in ("fails", "note", "params"):
                errs.append(f"ops[{i}] field 非法：{op.get('field')}")
        elif k == "link":
            if not op.get("design") or not op.get("tech"):
                errs.append(f"ops[{i}] link 缺 design/tech")
        else:
            errs.append(f"ops[{i}] 未知 op：{k}")
    n_need = len(gaps.get("need_from_you") or [])
    if n_need < 2:
        warns.append(f"gaps.need_from_you 只有 {n_need} 条（要求 ≥2）："
                     "完备答案通常意味着判断被代做")
    return errs, warns


def cmd_validate(fp):
    patch = load_json(fp)
    errs, warns = validate(patch)
    for e in errs:
        print("ERROR", e)
    for w in warns:
        print("WARN ", w)
    print(f"\nops={len(patch.get('ops', []))} errors={len(errs)} warnings={len(warns)}")
    return 1 if errs else 0


def cmd_write(fp, partial=False):
    patch = load_json(fp)
    errs, warns = validate(patch)
    if errs and not partial:
        print("拒绝写入，先修这些（或用 --partial 只写入合规的 op）：")
        for e in errs:
            print("  ERROR", e)
        return 1
    if errs and partial:
        keep = []
        for i, op in enumerate(patch.get("ops", [])):
            e2, _ = validate({"ops": [op], "gaps": {}})
            if e2:
                print("DROP  ops[%d] %s" % (i, e2[0]))
            else:
                keep.append(op)
        patch["ops"] = keep
        print("partial: kept %d ops" % len(keep))
    for w in warns:
        print("WARN ", w)
    os.makedirs(IN, exist_ok=True)
    dst = p(IN, "patch.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(patch, f, ensure_ascii=False, indent=2)
    with open(p(DSH, "request.log"), "a", encoding="utf-8") as f:
        f.write("%s  patch written ops=%d\n" % (
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(patch.get("ops", []))))
    print("written:", dst)
    print("页面点『② 拉取 patch』即可逐条裁决。")
    return 0


def cmd_clear():
    for d in (OUT, IN):
        if os.path.isdir(d):
            for f in glob.glob(os.path.join(d, "*")):
                os.remove(f)
    print("cleared outbox & inbox")
    return 0


def main():
    os.makedirs(DSH, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(IN, exist_ok=True)
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 0
    c = argv[0]
    if c == "status":
        return cmd_status()
    if c == "context":
        return cmd_context()
    if c == "validate" and len(argv) > 1:
        return cmd_validate(argv[1])
    if c == "write-patch" and len(argv) > 1:
        return cmd_write(argv[1], "--partial" in argv)
    if c == "clear":
        return cmd_clear()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
