#!/bin/bash
# 固化 CUDA13 环境 + nvcc 编译冒烟测试
set -x
exec > /root/autodl-tmp/installers/env_cuda13.log 2>&1
CUDA_ROOT=/root/miniconda3/lib/python3.12/site-packages/nvidia/cu13

# 1) 补 .so 符号链接（libcudart.so -> libcudart.so.13 等）
cd $CUDA_ROOT/lib
for so in libcudart.so.13 libnvvm.so.4; do
  base=${so%.so.*}.so
  [ -e "$base" ] || ln -sf "$so" "$base"
done
ls -la $CUDA_ROOT/lib/

# 2) 固化到 /etc/profile.d（每次登录自动生效）
cat > /etc/profile.d/cuda13.sh <<EOF
export CUDA_HOME=$CUDA_ROOT
export CUDA_PATH=$CUDA_ROOT
export PATH=$CUDA_ROOT/bin:/usr/local/bin:\$PATH
export LD_LIBRARY_PATH=$CUDA_ROOT/lib:$CUDA_ROOT/nvvm/libdevice:\$LD_LIBRARY_PATH
EOF
chmod +x /etc/profile.d/cuda13.sh
source /etc/profile.d/cuda13.sh

# 3) nvcc 编译冒烟测试：写个最小 kernel 编出 cubin
cat > /tmp/smoke.cu <<'EOF'
__global__ void k(float *a, float v) { a[threadIdx.x] = v; }
int main() { return 0; }
EOF
cd /tmp
nvcc -arch=sm_86 -cubin smoke.cu -o smoke.cubin 2>&1
ls -la smoke.cubin 2>/dev/null && echo "SMOKE_COMPILE_OK" || echo "SMOKE_COMPILE_FAIL"
echo "ENV_CUDA13_DONE"
