"""把 cloud-logs.cjs 的输出整理成"时间 容器 内容"，可按关键词过滤；文件 ID 一律遮掉。"""
import json, re, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
keys = sys.argv[2:]
if "error" in d:
    print(d); sys.exit(1)
print(f"# 共 {d['count']} 行，listOver={d.get('listOver')}，pages={d.get('pages')}")
for r in d["results"]:
    try:
        j = json.loads(r["Content"])
    except Exception:
        j = {"__CONTENT__": r.get("Content", "")}
    msg = str(j.get("__CONTENT__", ""))
    if keys and not any(k in msg for k in keys):
        continue
    msg = re.sub(r"cloud://\S+", "cloud://…", msg)
    print(r["Timestamp"], j.get("__TAG__", {}).get("container_name", "?"), msg[:300].replace("\n", " "))
