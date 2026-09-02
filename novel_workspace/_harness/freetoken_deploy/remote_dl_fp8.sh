#!/bin/bash
# 下载 Qwen/Qwen3.6-35B-A3B-FP8（官方 fp8_block 格式，FreeToken 完整支持 MoE offload）
set -x
exec > /root/autodl-tmp/installers/dl_fp8.log 2>&1
export PATH=/root/miniconda3/bin:/usr/local/bin:$PATH
cd /root/autodl-tmp/models
# 排除非权重杂项（保留 config/tokenizer/chat_template/generation_config）
/root/miniconda3/bin/modelscope download \
  --model Qwen/Qwen3.6-35B-A3B-FP8 \
  --local_dir /root/autodl-tmp/models/Qwen3.6-35B-A3B-FP8 \
  --exclude "*.md" "*.jsonl" 2>&1 | tail -5
echo "DL_FP8_EXIT=$?"
ls -lh /root/autodl-tmp/models/Qwen3.6-35B-A3B-FP8/ 2>/dev/null | head -15
du -sh /root/autodl-tmp/models/Qwen3.6-35B-A3B-FP8 2>/dev/null
echo "DL_FP8_DONE"
