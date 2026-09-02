#!/bin/bash
# 安装 FreeToken base（阿里云源）。accel 的 flashinfer cu13 阿里云缺，先 base 验证。
set -x
exec > /root/autodl-tmp/installers/install_freetoken.log 2>&1
export PATH=/usr/local/bin:/root/.local/bin:/root/miniconda3/bin:$PATH
PY=/root/miniconda3/bin/python3
IDX=https://mirrors.aliyun.com/pypi/simple
which nvcc && nvcc --version | tail -1

# 先尝试 accel；解析失败（flashinfer cu13 缺失）自动回退 base
/root/.local/bin/uv pip install --python $PY --index-url $IDX "freetoken[accel]" 2>&1 | tail -15
RC=$?
if [ $RC -ne 0 ]; then
  echo "=== accel 失败(rc=$RC)，回退 base ==="
  /root/.local/bin/uv pip install --python $PY --index-url $IDX "freetoken" 2>&1 | tail -15
fi

echo "=== 验证 ==="
$PY -c "import freetoken; print('freetoken OK', getattr(freetoken, '__version__', '?'))" 2>&1
$PY -m freetoken --version 2>&1 | head -3 || true
which ft 2>/dev/null
ls /root/miniconda3/bin/ft* 2>/dev/null
find /root/miniconda3 -maxdepth 4 -name 'ft*' -type f 2>/dev/null | grep -v python3.12/site-packages | head -5
echo "FREETOKEN_INSTALL_DONE"
