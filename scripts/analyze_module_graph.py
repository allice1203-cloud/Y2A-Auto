"""只读静态分析：模块依赖图与从 app.py 出发的可达性。不修改任何文件。"""
import ast
import pathlib
import json
import sys

ROOT = pathlib.Path("/Users/allice/Documents/系统开发/视频搬运通道")
MODULES = sorted(p.name for p in (ROOT / "modules").glob("*.py")
                 if p.name != "__init__.py")
# import 语句里用的是模块 stem（不含 .py），统一映射到文件名
STEM2FILE = {p.stem: p.name for p in (ROOT / "modules").glob("*.py")
             if p.name != "__init__.py"}
APP = ROOT / "app.py"
TESTS = sorted(p.name for p in (ROOT / "tests").glob("*.py"))


def parse(path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except Exception as exc:  # noqa: BLE001
        print(f"  [parse-fail] {path.name}: {exc}", file=sys.stderr)
        return None


def imports_of(path):
    """返回该文件中 import 到的 modules.xxx 名称集合（含相对导入、函数内延迟导入）"""
    tree = parse(path)
    if tree is None:
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # 处理相对导入: from .foo import bar
            if node.level and node.level >= 1:
                if mod:
                    parts = mod.split(".")
                    found.add(parts[0])
            elif mod.startswith("modules"):
                parts = mod.split(".")
                if len(parts) >= 2:
                    found.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("modules."):
                    parts = alias.name.split(".")
                    if len(parts) >= 2:
                        found.add(parts[1])
    return found


# 统一以 stem（import 名）作为图的节点
STEMS = sorted(STEM2FILE)

sizes = {s: (ROOT / "modules" / STEM2FILE[s]).stat().st_size for s in STEMS}
deps = {s: imports_of(ROOT / "modules" / STEM2FILE[s]) for s in STEMS}

# 谁引用了某模块（反向边）
ref_app = imports_of(APP)
rev = {s: {"app.py": s in ref_app, "modules": set(), "tests": set()} for s in STEMS}

for s in STEMS:
    for target in deps[s]:
        if target in rev:
            rev[target]["modules"].add(s)

for t in TESTS:
    for target in imports_of(ROOT / "tests" / t):
        if target in rev:
            rev[target]["tests"].add(t)

# 从 app.py 出发做可达性 DFS
reachable = set()
stack = [x for x in ref_app if x in deps]
while stack:
    cur = stack.pop()
    if cur in reachable:
        continue
    reachable.add(cur)
    for nxt in deps.get(cur, ()):
        if nxt not in reachable:
            stack.append(nxt)

orphan = [m for m in STEMS if m not in reachable]

print("=" * 78)
print("一、app.py 顶层/延迟导入的模块（主线入口）")
print("=" * 78)
for m in sorted(x for x in ref_app if x in deps):
    print(f"  {m:<38} {sizes[m] / 1024:>8.1f} KB")

print()
print("=" * 78)
print(f"二、从 app.py 不可达的模块（疑似死代码）共 {len(orphan)} 个")
print("=" * 78)
print(f"{'模块':<38}{'大小':>10}{'被模块引用':>12}{'被测试引用':>12}")
for m in sorted(orphan, key=lambda x: -sizes[x]):
    nm = len(rev[m]["modules"])
    nt = len(rev[m]["tests"])
    print(f"  {m:<36}{sizes[m] / 1024:>8.1f} KB{nm:>12}{nt:>12}")

print()
print("=" * 78)
print("三、仅被测试引用、主程序完全没用到的模块")
print("=" * 78)
only_test = [m for m in STEMS if not rev[m]["app.py"] and not rev[m]["modules"] and rev[m]["tests"]]
for m in sorted(only_test, key=lambda x: -sizes[x]):
    print(f"  {m:<36}{sizes[m] / 1024:>8.1f} KB   测试文件 {len(rev[m]['tests'])} 个")

print()
print("=" * 78)
print("四、完全无人引用的模块（app/modules/tests 都没引用）")
print("=" * 78)
dead = [m for m in STEMS if not rev[m]["app.py"] and not rev[m]["modules"] and not rev[m]["tests"]]
for m in sorted(dead, key=lambda x: -sizes[x]):
    print(f"  {m:<36}{sizes[m] / 1024:>8.1f} KB")

print()
print("=" * 78)
print("五、被最多模块依赖的枢纽模块（删除风险高）")
print("=" * 78)
hubs = sorted(STEMS, key=lambda m: -len(rev[m]["modules"]))[:12]
for m in hubs:
    if len(rev[m]["modules"]) > 0:
        print(f"  {m:<36} 被 {len(rev[m]['modules'])} 个模块依赖")

out = {
    "total_modules": len(STEMS),
    "app_entry_modules": sorted(x for x in ref_app if x in deps),
    "unreachable_from_app": sorted(orphan),
    "only_test_referenced": sorted(only_test),
    "fully_dead": sorted(dead),
    "reverse_deps": {m: {"modules": sorted(rev[m]["modules"]), "tests": sorted(rev[m]["tests"]),
                         "app": rev[m]["app.py"], "size_kb": round(sizes[m] / 1024, 1)} for m in STEMS},
}
(ROOT / "docs" / "receipts" / "module-graph-20260906.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print()
print(f"详细结果已写入 docs/receipts/module-graph-20260906.json")
