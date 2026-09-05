#!/bin/bash
# B 服务器模型续传下载（wget -c，避开 curl 多层转义）
set -u
OUT=/root/autodl-tmp/models/Qwen3.8-27B-UD-IQ3_S.gguf
URL="https://modelscope.cn/models/unsloth/Qwen3.8-27B-GGUF/resolve/master/Qwen3.8-27B-UD-IQ3_S.gguf"
rm -f /root/autodl-tmp/models/iq3s.done
echo "wget start $(date +%H:%M:%S) size=$(stat -c%s $OUT 2>/dev/null || echo 0)"
wget -c -q --tries=20 --timeout=60 --waitretry=5 -O "$OUT" "$URL"
RC=$?
echo "wget rc=$RC $(date +%H:%M:%S) final=$(stat -c%s $OUT 2>/dev/null)"
if [ $RC -eq 0 ]; then
  echo DL_OK > /root/autodl-tmp/models/iq3s.done
fi
echo WGET_END
