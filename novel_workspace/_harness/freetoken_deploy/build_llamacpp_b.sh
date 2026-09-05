#!/bin/bash
# B 服务器：下载 llama.cpp v0.4.0 源码 + CUDA 编译 llama-server（输出到 data 盘）
# 用法: bash build_llamacpp_b.sh
set -x
SRC=/root/autodl-tmp/lc-src
BLD=/root/autodl-tmp/lc-build
mkdir -p "$SRC" "$BLD"

# 1. 下载源码（GitHub tag tarball）
cd /root/autodl-tmp
if [ ! -f llama.cpp-v0.4.0.tar.gz ]; then
  curl -sL -m 300 -o llama.cpp-v0.4.0.tar.gz \
    "https://github.com/ggml-org/llama.cpp/archive/refs/tags/v0.4.0.tar.gz" || exit 10
fi
ls -la llama.cpp-v0.4.0.tar.gz

# 2. 解压
tar xzf llama.cpp-v0.4.0.tar.gz -C "$SRC" --strip-components=1 2>/dev/null || exit 11

# 3. configure（CUDA 13, T4 sm_75, Release）
export PATH=/usr/local/cuda/bin:$PATH
cmake -S "$SRC" -B "$BLD" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DGGML_CUDA_ARCHITECTURES="75" \
  -DGGML_NATIVE=OFF \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc > /root/autodl-tmp/lc-cmake.log 2>&1
echo "CMAKE_RC=$?"

# 4. 后台编译（前台太长，直接 setsid；日志落盘）
setsid nohup cmake --build "$BLD" -j 24 --target llama-server llama-cli \
  > /root/autodl-tmp/lc-build.log 2>&1 < /dev/null &
echo "BUILD_PID=$!"
echo BUILD_STARTED
