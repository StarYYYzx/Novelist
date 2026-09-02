#!/bin/bash
# 用 PyPI 官方新命名包安装 CUDA 13.0 nvcc 全家桶（避免下载 3.2GB runfile）
set -x
exec > /root/autodl-tmp/installers/install_nvcc_pip.log 2>&1
PY=/root/miniconda3/bin/python3
echo "=== 尝试默认源(阿里云镜像) ==="
$PY -m pip install --no-input \
  nvidia-cuda-nvcc==13.0.88 nvidia-nvvm==13.0.88 \
  nvidia-cuda-runtime==13.0.88 nvidia-cuda-crt==13.0.88 2>&1 | tail -5
RC=$?
if [ $RC -ne 0 ]; then
  echo "=== 默认源失败(rc=$RC)，回退官方 PyPI ==="
  $PY -m pip install --no-input -i https://pypi.org/simple \
    nvidia-cuda-nvcc==13.0.88 nvidia-nvvm==13.0.88 \
    nvidia-cuda-runtime==13.0.88 nvidia-cuda-crt==13.0.88 2>&1 | tail -5
fi
echo "=== 定位 nvcc ==="
NVCC_PATH=$(find /root/miniconda3/lib -path '*nvidia/cuda_nvcc/bin/nvcc' -type f 2>/dev/null | head -1)
echo "nvcc 实际位置: $NVCC_PATH"
if [ -n "$NVCC_PATH" ]; then
  ln -sf "$NVCC_PATH" /usr/local/bin/nvcc
  ln -sf "$(dirname "$NVCC_PATH")/nvcc.profile" /usr/local/bin/nvcc.profile 2>/dev/null
  echo "已软链到 /usr/local/bin/nvcc"
fi
# nvvm/crt/runtime 的 lib 也要能被找到
for lib in nvvm cuda_runtime cuda_crt; do
  find /root/miniconda3/lib -path "*nvidia/$lib/lib*" -name '*.so*' -type f 2>/dev/null | head -3
done
echo "NVCC_PIP_DONE"
