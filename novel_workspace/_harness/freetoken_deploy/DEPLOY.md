# FreeToken 部署报告（AutoDL bjb2 / RTX 3080 Ti 12GB）

> 日期：2026-09-02 ｜ 服务器：`ssh -p 22214 root@connect.bjb2.seetacloud.com`（凭据见 `_harness/autodl_ssh.txt` C 段）
> 状态：**✅ 部署完成并验证——ft serve 运行于 6006，模型 Qwen/Qwen3.6-35B-A3B-FP8（MoE offload），
> `/v1/models` 与 `/v1/chat/completions` 均实测 200，SSH 隧道本机 18006→6006 连通**

---

## 1. 项目调研结论

**FreeToken**（`FlashML-org/FreeToken`，Apache-2.0，v0.1.2）是面向消费级/边缘 GPU 的 **MoE 推理引擎**。
核心卖点：MoE 模型 30B+ 参数在 12GB 显存上可跑——专家权重不常驻 GPU，而是按需换入。

| 维度 | 结论 |
|---|---|
| 定位 | MoE 专用推理服务，OpenAI/Anthropic 兼容 API（`ft serve`） |
| 硬件要求 | 消费级 GPU（12–24GB），NVIDIA 驱动 **≥ r580（CUDA 13）**；Linux x86_64，Python ≥3.10 |
| CUDA | 运行时可动态 JIT 编译算子，**必须能在 PATH 找到与驱动匹配的 nvcc**（CUDA 13） |
| MoE 后端 | `fused`（专家全驻 GPU）/ `offload`（专家驻主机内存 + GPU LRU 缓存）/ `cpu` / `hybrid`（按层重叠）|
| 量化格式 | dense：compressed-tensors NVFP4、fp8_block、modelopt NVFP4；**MoE 只支持 fp8_block（`quant_method=fp8` + `weight_block_size:[128,128]`）与 modelopt NVFP4** |
| 模型来源 | 官方 known-good 仓库：`Qwen/Qwen3.6-35B-A3B(-FP8)`、`nvidia/Qwen3.6-35B-A3B-NVFP4` |
| 适用性判断 | **适合本项目**：男频修仙长文需要长上下文连贯生成，35B-A3B（激活 3B）在 12GB 卡上以 offload 跑，吞吐远高于本地 9B，且支持长 ctx |

论文：arXiv 2608.16157（未细读，实现以源码/实测为准）。

---

## 2. 服务器环境

| 项 | 值 |
|---|---|
| GPU | RTX 3080 Ti 12GB（sm_86） |
| 显存 | 12GB（加载 offload 模型时实际只占 ~11.6GB 上限） |
| 主机内存 | 376GB（offload 专家权重 + KV 的承载主体） |
| 驱动 | 580.105.08（满足 ≥r580 / CUDA 13 要求） |
| 系统自带 CUDA | 12.8 toolkit（不满足 → 需另装 13） |
| Python | `/root/miniconda3/bin/python3`（3.12） |
| 数据盘 | `/root/autodl-tmp`（50GB，模型目录） |

---

## 3. 安装步骤（全部可复现）

### 3.1 CUDA 13 nvcc —— 通过 PyPI wheel（绕开 runfile）

AutoDL 系统只有 CUDA 12.8。FreeToken 需要 CUDA 13 的 nvcc 做 JIT。
runfile 下载全部失败（官方 0.6–13MB/s、华为云镜像返回 5KB HTML 假文件、SJTU/tuna 无 nvidia channel），
最终走 **PyPI 新版 NVIDIA wheel**（`remote_install_nvcc_pip.sh`，aliyun index）：

```
uv pip install --python /root/miniconda3/bin/python3 --index-url https://mirrors.aliyun.com/pypi/simple \
  nvidia-cuda-nvcc==13.0.88 nvidia-nvvm==13.0.88 \
  nvidia-cuda-runtime==13.0.88 nvidia-cuda-crt==13.0.88
```

关键发现：新版 NVIDIA PyPI 包**无 `-cu13` 后缀**（旧命名已废弃），统一装入
`/root/miniconda3/lib/python3.12/site-packages/nvidia/cu13/`，nvcc 在 `cu13/bin/nvcc`。

随后 `remote_env_cuda13.sh`：为 `cu13/lib` 与 `cu13/nvvm/libdevice` 补 `.so` 符号链接，
并把环境固化进 `/etc/profile.d/cuda13.sh`（CUDA_HOME / PATH / LD_LIBRARY_PATH）。
冒烟验证：`nvcc -arch=sm_86 -cubin` 编译通过。

### 3.2 FreeToken（`remote_install_freetoken.sh`）

```
uv pip install --python /root/miniconda3/bin/python3 --index-url https://mirrors.aliyun.com/pypi/simple \
  "freetoken[accel]"
```

- `[accel]` 依赖：torch 2.11.0、transformers 5.16.1、triton 3.6.0、sglang-kernel 0.4.5、
  flashinfer-python[cu13]（**aliyun 缺 cu13 wheel，仅 0.2.x sdist → accel 分支解析失败**，脚本回退装标准版 `freetoken`）。
- 安装结果：`freetoken 0.1.2`，`ft` CLI 位于 `/root/miniconda3/bin/ft`。
- 教训：uv **不读** pip.conf 镜像配置，必须显式 `--index-url`；官方 PyPI 在 AutoDL 上极慢。

---

## 4. 模型选择（关键决策，踩过坑）

| 候选 | 结论 | 原因 |
|---|---|---|
| `RedHatAI/Qwen3.6-35B-A3B-NVFP4`（CVT 格式） | ❌ **不兼容** | quant_config 是 compressed-tensors NVFP4 → FreeToken 0.1.2 把 MoE 误走 dense loader（该路径只服务 dense Qwen3.6-27B），所有权重强塞 GPU → 12GB OOM（实测 22GB 权重 / 11.63GB 显存）|
| `Qwen/Qwen3.6-35B-A3B-FP8`（官方） | ✅ **采用** | `quant_method=fp8` + `weight_block_size:[128,128]`（fp8_block）→ 正确走 `setup_offload_expert_banks` / MoE offload 路径 |
| `nvidia/Qwen3.6-35B-A3B-NVFP4`（modelopt） | ✅ known-good | ModelScope 上无此仓库，需 HF（AutoDL 不可达），弃 |

模型体积：37.5GB（40 个 `layers-*.safetensors` 分片 + outside + mtp + tokenizer）。
下载：ModelScope（HF 不可达，ModelScope 是唯一可用源），路径 `/root/autodl-tmp/models/Qwen3.6-35B-A3B-FP8/`。

---

## 5. 启动与验证

### 5.1 启动命令（`remote_start_fp8.sh` → 服务器 `/root/autodl-tmp/start_serve.sh`）

```bash
source /etc/profile.d/cuda13.sh
export PATH=/root/miniconda3/bin:/usr/local/bin:$PATH
export CUDA_HOME=/root/miniconda3/lib/python3.12/site-packages/nvidia/cu13
export LD_LIBRARY_PATH=$CUDA_HOME/lib:$CUDA_HOME/nvvm/libdevice:$LD_LIBRARY_PATH

/root/miniconda3/bin/ft serve \
  --model-path /root/autodl-tmp/models/Qwen3.6-35B-A3B-FP8 \
  --moe-backend offload --moe-cache-auto \
  --host 0.0.0.0 --port 6006 \
  --served-model-name qwen3.6-35b-a3b-fp8
```

### 5.2 前置清理（每个新开机都要做）

1. **杀 tensorboard**：AutoDL 镜像自带 `tensorboard --port 6007`，而 ft 的 scheduler 用 `6006+1=6007` →
   `pkill -9 -f tensorboard`（或 `ss -tlnp | grep 6007` 找 PID）。
2. **杀旧 ft**：`pkill -9 -f '[f]t serve'`（`[f]` 防 pkill 自匹配自杀）——
   **注意 ft 的 multiprocessing spawn 子进程命令行不含 "ft serve" 字样，pkill 杀不到**，
   会残留占用 6007 致下次启动 EADDRINUSE。彻底清理用
   `ps aux | grep -E 'freetoken|spawn_main|ft serve' | grep -v grep | awk '{print $2}' | xargs kill -9`
   （或按 `ss -tlnp` 的 PID 直杀）。一键流程见本地 `start_serve_final.py`。
3. **首请求会触发 flashinfer JIT 编译**（一次性，约 1–2 分钟 ninja），需 `cu13/lib64` 符号链接 +
   `-lcuda` stub（见 §6.1），否则首请求后 backend 崩溃。

### 5.3 验证结果（2026-09-02 实测）

| 项 | 结果 |
|---|---|
| `/v1/models` | 200：`qwen3.6-35b-a3b-fp8`, **max_model_len / context_length = 262144** |
| `/v1/chat/completions` | 200（reasoning_effort=none）；max_tokens=200 短请求 48 tok / ~1s |
| 小说场景长输出（376 字正文） | **10.7s**（none 模式，262 tok ≈ 24 tok/s） |
| 权重加载 | 42/42 fp8 分片 ~9s；expert banks 31.4G 并行装载 ~2min（256MB/s） |
| 显存 | 稳态 ~11.4GB/12GB（moe_cache_size=1772, num_pages=8263） |
| SSH 隧道 | 本机 18006→6006 direct-tcpip 连通，`/v1/models` 200 |

**⚠️ reasoning_effort 是必传参数**（Novelist 接入硬约束）：

| | `reasoning_effort:"none"` | `low`（默认思考） |
|---|---|---|
| 正文 | ✅ 直出（376 字 / 10.7s / finish=stop） | ❌ **空**（2213 字 reasoning 吃光预算, finish=length） |
| 对照 | LM-Studio qwen3.5-9b 思考**关不掉** | FreeToken 上 Qwen3.6 思考**可关**（supported 含 none） |

→ 正文/审校生成一律传 `reasoning_effort:"none"`；确需思考的环节（如重场戏设计）再调
`low/medium` 并同步放大 max_tokens。**不能信任模型默认**——默认思考会把预算全吃光。

---

## 6. 已知限制与后续

- **MoE + compressed-tensors NVFP4 不支持**（v0.1.2 源码判定：CT-NVFP4 仅 dense 路径）；MoE 需 fp8_block 或 modelopt 格式。
- offload 模式下专家换入换出走主机内存↔GPU，单请求延迟高于 fused；吞吐敏感场景可试 `--moe-backend hybrid`。
- 服务端口 6006 未映射公网，本机访问需 SSH 隧道：
  `ssh -p 22214 -L 18006:127.0.0.1:6006 root@connect.bjb2.seetacloud.com`
- 重启后恢复：`bash /root/autodl-tmp/start_serve.sh`（profile.d 固化仍在，无需重装；
  每次重启前仍要清 tensorboard + spawn 残留，见 §5.2）。
- 服务器 C 与既有 A/B 服务器的关系：C 跑 FreeToken/Qwen3.6-FP8 用于长上下文高质量生成，
  A（3080ti llama-server）仍是主力，B（T4）备用——详见 `autodl_ssh.txt`。

### 6.1 关键排障记录（踩坑即文档）

1. **CVT-NVFP4 MoE → OOM**：`compressed-tensors` NVFP4 在 FT 0.1.2 只走 dense 路径
   （`_iter_weights_compressed_tensors` 无专家处理），MoE 权重全塞 GPU → 12GB OOM。
   必须用 fp8_block（`quant_method=fp8`+`weight_block_size:[128,128]`，官方 Qwen FP8 仓库）或 modelopt 格式。
2. **flashinfer JIT 链接失败**：`/usr/bin/ld: cannot find -lcudart`——PyPI cu13 布局是 `cu13/lib`，
   但 flashinfer ninja 用 `-L cu13/lib64 -L cu13/lib64/stubs` 找库。修复（已固化在服务器）：
   ```bash
   CU13=/root/miniconda3/lib/python3.12/site-packages/nvidia/cu13
   ln -sfn lib $CU13/lib64 && mkdir -p $CU13/lib/stubs
   cp /usr/local/cuda/lib64/stubs/libcuda.so $CU13/lib/stubs/libcuda.so
   ```
   修完首请求 JIT（batch_prefill kernel，head_dim 256）编译通过，缓存于 `~/.cache/flashinfer/`。
3. **6007 EADDRINUSE 两种来源**：AutoDL tensorboard（易见）+ ft 自身 spawn 残留（隐蔽，
   命令行不含 "ft serve"，pkill 杀不到）。都按 PID 杀。
4. **uv 不读 pip.conf**：必须显式 `--index-url https://mirrors.aliyun.com/pypi/simple`；
   官方 PyPI 在 AutoDL 极慢。
5. **思考型模型预算陷阱**：Qwen3.6 默认思考（reasoning_content 计入 max_tokens），
   预算不足时 content 空 + finish=length，与 LM-Studio qwen3.5-9b 同源；**FT 上可关**
   （reasoning_effort:none），是相对本地链路的关键升级。

---

## 7. 可复用脚本（本地 `_harness/freetoken_deploy/`）

| 脚本 | 用途 |
|---|---|
| `remote_install_nvcc_pip.sh` | PyPI 装 CUDA 13.0.88 nvcc 组件 |
| `remote_env_cuda13.sh` | 补 .so 符号链接 + 固化 /etc/profile.d/cuda13.sh |
| `remote_install_freetoken.sh` | uv 装 freetoken[accel]（aliyun index，失败回退标准版） |
| `remote_start_fp8.sh` | ft serve 启动脚本（offload + 6006，服务器 `/root/autodl-tmp/start_serve.sh`） |
| `remote_dl_fp8.sh` | ModelScope 下载 Qwen3.6-35B-A3B-FP8 |
| `start_serve_final.py` | **一键启动**：上传脚本 → 按 PID 彻底清场 → 单实例启动 → curl 探测就绪 |
| `wait_model_loaded.py` | 等 expert banks 装载完 + 首请求 chat 200 探测 |
| `test_serve_fp8.py` | /v1/models + chat completion 验证 |
| `e2e_novel_test.py` | 小说场景端到端（reasoning none vs low 对比 + 计时） |
| `verify_cfg_fp8.py` | 校验模型 config 量化字段（fp8_block） |
| `cleanup_ft_procs.py` / `check_dl_state.py` | 清残留进程 / 下载状态探测 |
