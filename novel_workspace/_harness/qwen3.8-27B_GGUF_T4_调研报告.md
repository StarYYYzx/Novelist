# Qwen3.8-27B GGUF 16G 显卡部署调研报告

> 日期：2026-09-05 ｜ 目标卡：**16G（Tesla T4, 服务器 B region-9）** ｜ 调研对象：`Qwen3.8-27B-UD-IQ3_S.gguf` 及同族量化档
> 状态：调研 + 配置实测完成，模型已下载，试加载验证中（结果回填于 §6）

---

## 0. 结论先行

1. **模型可得**：`Qwen3.8-27B-UD-IQ3_S.gguf` 属 `unsloth/Qwen3.8-27B-GGUF`（HF 主仓，ModelScope 有官方同步镜像），**11.21 GB（10.44 GiB）**，实测直链可达。
2. **16G 卡可跑，但只适合 ≤8-12K 短上下文文本任务**：IQ3_S 全层 offload 后显存账目 ≈ 11.5GB，余 ~3GB 只能喂 KV cache（本模型 **KV ≈ 256KB/token**，1GB 仅 ≈4K token）。
3. **服务器 B 基本满足，唯一硬缺口是 llama.cpp 版本**：现装 2026-03-18 build（ggml 0.9.7），早于 Qwen3.8 发布（08-14）约 5 个月；Qwen3.8 GGUF 需 release-week 后 build（llama.cpp b104xx）方能稳定加载/处理新 chat template。**须升级**（GitHub 可达，源码编译或下载 release 二进制）。
4. **建议**：若目标是「16G 卡上的小说写作模型」，IQ3_S（3-bit）质量损失可感知（top-1 同源基准 ~92-93%）；**IQ4_XS 13.27GB** 是 16G 卡质量上限档但 context 更紧；写作向仍建议优先 C 服务器 FreeToken 35B-A3B（质量档），T4 上跑 3.8-27B 定位为「独立次质量档/测试并发档」。

---

## 1. 模型档案：Qwen3.8-27B

| 项 | 值 | 来源 |
|---|---|---|
| 发布 | 2026-08-14（Alibaba Qwen 官方） | HF `Qwen/Qwen3.8-27B` rev 1d4bf0f |
| 架构 | **Dense VLM**，27.78B 全激活，64 语言层 + 视觉编码器 | 官方 model card |
| 原生上下文 | 262,144 token（可扩 1M） | 官方 model card |
| 权重大小 | safetensors 55.56 GB（18 shards） | HF manifest |
| 许可 | Apache-2.0 | 官方 |
| 特性 | 思考可控（thinking on/off）、MTP 多头预测、多模态（文本+图像+视频） | 官方/GGUF 仓库 |
| GGUF arch | `qwen3_5`（复用 Qwen3.5 系列注册） | unsloth README tags |
| llama.cpp 要求 | **b10419+（GGUF 转换用）；官方建议 release-week 后 build** | kingy.ai source-audited / atomic.chat |

> 注：本报告**不涉及** Qwen3.8-Flash/Max（MoE/2.4T），两者非 dense 27B、不可在此卡落地。

---

## 2. GGUF 供给生态与「UD-」前缀归属

主要仓库（HF 同步至 ModelScope，国内直连）：

| 仓库 | 风格 | 备注 |
|---|---|---|
| **unsloth/Qwen3.8-27B-GGUF** | `UD-` = **Unsloth Dynamic V3.0**（imatrix 校准动态量化，同 bpw 质量优于普通 K-quant） | 30 档全梯，**含用户指定的 UD-IQ3_S**；ModelScope 镜像实测可达 |
| peculiar-ragdoll/Dirk-...-GGUF | `GSQ-RCO-`（IST-DASLab，Gumbel-Softmax 量化）+ 部分 UD 档 | <3bpw 档质量宣称更优；Sharp 模板改造 |
| AtomicChat/Qwen3.8-27B-GGUF | `AD-`（architecture-aware） | kingy.ai 16G 档首选来源（AD-IQ3_S 13.84GB） |

ModelScope 上 unsloth 镜像**实测文件齐全**（含 config.json / imatrix / mmproj-BF16 与全部 30 GGUF 档）。

---

## 3. 量化档位适配分析（16G = 14912 MiB ≈ 14.56 GiB）

文件大小（HF manifest / ModelScope 实测；GB=十进制，GiB=二进制）：

| 档位 | 大小 (GB / GiB) | 16G 全 offload? | 余显存喂 KV | 定位 |
|---|---|---|---|---|
| UD-IQ2_XXS | 7.80 / 7.27 | ✅ 宽松 | ~5.5G → ~20K ctx | 应急档，质量差 |
| **UD-IQ3_S**（用户指定） | **11.21 / 10.44** | ✅ | ~3.4G → **~10-12K ctx** | **16G 稳妥档（本次实测对象）** |
| UD-IQ3_XXS | 10.18 / 9.48 | ✅ | ~3.9G | 略低质量换 context |
| UD-Q3_K_XL | 12.24 / 11.40 | ✅ 临界 | ~2.6G → ~8K | 3-bit 非 imatrix，不推 |
| UD-IQ4_XS | 13.27 / 12.36 | ⚠️ 紧 | ~1.7G → ~6K | 16G 质量上限档（kingy.ai 同档推荐） |
| UD-Q4_K_M | 15.33 / 14.28 | ❌ 超限 | — | 需 CPU offload 部分层，dense 全激活降速明显 |
| UD-Q4_K_XL | 16.35 / 15.23 | ❌ | — | **24G 卡默认档**（kingy.ai 首选） |
| Q8_0 / BF16 | 27.05 / 51 | ❌ | — | ≥48G |

**KV 内存模型**：Qwen3.8-27B（64 层 GQA）KV ≈ **256KB/token**——每释放 1GB 显存仅换 ~4K token 上下文。这是 27B dense + 16G 的根本矛盾：**模型越小档 + 短 ctx** 是唯一解。实测回填见 §6。

---

## 4. 16G 部署方案（llama.cpp）

### 4.1 前置条件

- llama.cpp ≥ b104xx（Qwen3.8 release-week 后）；GGUF 由 unsloth 用 b10430 转换
- 若用 vision 需 mmproj（本次纯文本小说任务**不需要**，省 931MB）

### 4.2 启动命令（llama-server，OpenAI 兼容）

```bash
# 下载（ModelScope 国内直连，实测 28.6 MB/s）
curl -L -C - -o /root/autodl-tmp/models/Qwen3.8-27B-UD-IQ3_S.gguf \
  "https://modelscope.cn/models/unsloth/Qwen3.8-27B-GGUF/resolve/master/Qwen3.8-27B-UD-IQ3_S.gguf"

# 启动：全层 GPU + 显式小 ctx（KV 大头，勿开大）
llama-server -m /root/autodl-tmp/models/Qwen3.8-27B-UD-IQ3_S.gguf \
  --host 0.0.0.0 --port 6012 \
  -ngl 99 -c 8192 --cache-type-k q8_0 --cache-type-v q8_0 \
  --chat-template qwen3.8 --jinja
```

- `-ngl 99`：全层 offload（IQ3_S 10.44GiB 放得下）
- `-c 8192`：起步 ctx；KV 用 q8_0 压缩可再省一半 KV 显存 → 实测按 nvidia-smi 回填再放大
- 模板：Qwen3.8 需新 build 的 jinja 模板（旧 build 的静态模板表无此条目）

### 4.3 与小说写作任务的适配判断

- 质量档：正文生成用 C 服务器 FreeToken Qwen3.6-35B-A3B（reasoning_effort:none），已是「质量×速度」甜点
- **T4/3.8-27B 的价值场景**：①测试/回归的多路并发（不占 35B 服务）；②长上下文研究（需放弃 3-bit 质量换 ctx，仍 ≤16K）
- 思考开关：Qwen3.8 支持 enable_thinking:false / reasoning_effort——需**新 build** 模板传递才生效（fresh release 老 build 曾坏过此开关）

---

## 5. 云服务器实测（2026-09-05 11:2x）

### 5.1 在线状态

| 服务器 | SSH | GPU | 状态 |
|---|---|---|---|
| A：3080ti (nmb2:46295) | connect.nmb2.seetacloud.com | 12G | ❌ 端口不可达（已关机/实例释放） |
| **B：T4 (region-9:46118)** | region-9.autodl.pro | **16G（本次目标）** | ✅ 在线 |
| C：3080Ti (bjb2:22214) | connect.bjb2.seetacloud.com | 12G | ❌ 端口不可达（关机） |

### 5.2 服务器 B 配置实测（满足度逐项）

| 检测项 | 实测值 | 是否满足 |
|---|---|---|
| GPU | Tesla T4 15360 MiB（14912 MiB usable），驱动 580.65.06 | ✅ |
| data 盘 `/root/autodl-tmp` | **50G，空（仅 56K 用）** | ✅ IQ3_S 11.2GB 富余 |
| 系统盘 `/` | 30G 剩 13G | ⚠️ llama.cpp 编译需注意（输出放 data 盘或删除旧 build） |
| 内存 | 251G total / 238G available | ✅（无关紧要，dense 全 GPU） |
| CPU | 32 核 x86_64 Ubuntu | ✅ |
| CUDA 工具链 | **nvcc 13.0**（cuda_13.0.r13.0）、gcc/g++/cmake/make | ✅（可源码编译 llama.cpp） |
| llama.cpp | `/root/llama.cpp/build/bin/llama-server`，**2026-03-18 build，ggml 0.9.7** | ❌ **过老**（详见 §5.3） |
| 端口 6006-6019 | 全空闲 | ✅ |
| 网络 | **HF 000 不可达**；**ModelScope 302 可达（实测 28.6MB/s）**；GitHub 200 可达 | ✅ |
| python | 无 python3（纯 llama.cpp 服务器） | ⚠️ 下载/校验须 curl（已实测可用） |

### 5.3 llama.cpp 兼容性缺口（唯一硬阻塞）

- B 现 build 2026-03-18（ggml 0.9.7）→ 已含 `QWEN3_5` arch 基础支持（B 现役 Qwen3.5-27B.Q4_K_M 即此 arch），**但早于 Qwen3.8 发布 5 个月**
- Qwen3.8-27B GGUF 转换用 llama.cpp **b10430**；社区明确要求 release-week 后 build（模板/新张量处理）
- 3 月 build ≈ b3xxx 量级 → **缺 Qwen3.8 的 chat template 与可能的张量兼容层**，直接加载大概率失败或思考开关失效
- 修复路径（GitHub 200 可达，已具备 nvcc13+cmake）：
  - 快：下载官方 release 二进制（cu12.x，兼容驱动 580）
  - 稳：`git clone https://github.com/ggml-org/llama.cpp && cmake -DGGML_CUDA=ON` 编译（T4 CC 7.5，预计 20-40 min）
  - 输出目录放 data 盘 `/root/autodl-tmp/llama.cpp`（系统盘仅剩 13G）

---

## 6. 实测验证（模型已下载，试加载结果回填）

- [x] ModelScope 直链可达（HTTP 200）
- [x] 下载速度实测 28.6 MB/s → 11.2GB ≈ 6.5 min
- [x] SHA256 校验源：`d847e2c1e4aa276e4b7b8e9ad7628050e61e165d49ab995407bc36677a6f3864`（ModelScope manifest）
- [ ] 旧 llama.cpp 试加载 → （待回填）
- [ ] 升级后加载 → （待回填）
- [ ] 实测显存占用 / 可用 ctx / tok/s → （待回填）

---

## 7. 结论与建议

| 问题 | 结论 |
|---|---|
| Qwen3.8-27B-UD-IQ3_S 可得吗？ | ✅ ModelScope unsloth 镜像，11.21GB，SHA256 已知 |
| 16G T4 跑得动吗？ | ✅ 账目上可行（IQ3_S 全 offload ~11.5G，≤10K ctx） |
| 服务器 B 满足要求吗？ | ⚠️ **配置全满足，唯一缺口 = llama.cpp 升级**（+无 python 需 curl 下载） |
| 值得部署吗？ | 写作向价值中等：3-bit 质量损失可感知；建议定位「独立次质量/并发测试档」，质量主线仍走 C 35B |

（后续动作待用户拍板：①升级 llama.cpp 完成试加载验证 ②或仅保留调研存档。）
