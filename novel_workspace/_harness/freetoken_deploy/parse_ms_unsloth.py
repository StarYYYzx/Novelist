# -*- coding: utf-8 -*-
"""远端解析脚本：ModelScope unsloth/Qwen3.8-27B-GGUF 目标档位大小。"""
import json

RAW = "/tmp/ms_unsloth.json"
try:
    d = json.load(open(RAW, encoding="utf-8"))
except Exception as ex:
    print("JSON_LOAD_FAIL", ex)
    raise SystemExit(1)

files = d.get("Data", {}).get("Files", [])
want = ("IQ3_S", "IQ3_XXS", "Q3_K_XL", "IQ4_XS", "Q4_K_M", "Q4_K_S", "UD-Q4_K_XL", "Q2_K_XL")
print("%-44s %12s %10s" % ("FILE", "GiB", "GB"))
for f in files:
    if f.get("Type") == "blob" and any(w in f.get("Name", "") for w in want):
        print("%-44s %8.2f %8.2f" % (f["Name"], f["Size"] / 2**30, f["Size"] / 1e9))
print("total_blob:", sum(1 for f in files if f.get("Type") == "blob"))
