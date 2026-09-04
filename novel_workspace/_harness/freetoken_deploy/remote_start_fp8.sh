#!/bin/bash
# 启动 FreeToken serve（FP8 版）——MoE offload：专家驻留主机内存，GPU LRU 缓存
# 模型：Qwen/Qwen3.6-35B-A3B-FP8（官方 fp8_block 格式，quant_method=fp8, weight_block_size=[128,128]）
# 硬件：RTX 3080 Ti 12GB + 376GB RAM
# 注意：本文件必须保持 LF 换行（SFTP 上传后远端直接 bash 执行，CRLF 会启动即死）
set -x
exec > /root/autodl-tmp/serve_ft.log 2>&1
source /etc/profile.d/cuda13.sh
export PATH=/root/miniconda3/bin:/usr/local/bin:$PATH
export CUDA_HOME=/root/miniconda3/lib/python3.12/site-packages/nvidia/cu13
export LD_LIBRARY_PATH=$CUDA_HOME/lib:$CUDA_HOME/nvvm/libdevice:$LD_LIBRARY_PATH

MODEL=/root/autodl-tmp/models/Qwen3.6-35B-A3B-FP8
cd /root/autodl-tmp
/root/miniconda3/bin/ft serve --model-path "$MODEL" --moe-backend offload --moe-cache-auto --host 0.0.0.0 --port 6006 --served-model-name qwen3.6-35b-a3b-fp8 2>&1
echo "FT_SERVE_EXITED rc=$?"
