# dat_compressor

批量压缩 maimai DX 街机游戏的 `.dat` 视频文件（CRI USM 格式）。

通过解密提取 → VP9 重编码 → 重新加密封装的流程，在不改变分辨率的前提下缩小 MovieData 视频文件体积。

## 依赖

- Python 3.9+
- ffmpeg（`brew install ffmpeg`）

无需安装任何 pip 包。

## 使用方法

```bash
cd dat_compressor文件夹所在目录
```

### 预览模式

先处理单个文件，观察压缩效果，决定合适的 quality 值：

```bash
python3 -m dat_compressor preview -i /path/to/MovieData/000001.dat -q 30
```

输出为同目录下的 `000001.mp4`，可直接播放查看画质。

指定输出路径：

```bash
python3 -m dat_compressor preview -i 000001.dat -o /tmp/test.mp4 -q 30
```

### 批量模式

```bash
python3 -m dat_compressor batch -i /path/to/MovieData -o /path/to/output -q 30
```

参数说明：

| 参数 | 说明 |
|------|------|
| `-i` / `--input` | 输入目录（包含 .dat 文件） |
| `-o` / `--output` | 输出目录（自动创建） |
| `-q` / `--quality` | 质量 1-100，体积越小画质越差 |
| `-w` / `--workers` | 并行进程数（默认 CPU 核心数 ÷ 3） |
| `--force` | 覆盖已存在的输出文件 |

### 断点续传

中断后重新运行相同命令，已完成的文件会自动跳过：

```bash
python3 -m dat_compressor batch -i /path/to/MovieData -o /path/to/output -q 30
# Ctrl+C 中断...
python3 -m dat_compressor batch -i /path/to/MovieData -o /path/to/output -q 30
# 自动跳过已处理文件，继续剩余部分
```

## quality 参数参考

| quality | VP9 CRF | 效果 |
|---------|---------|------|
| 100 | — | 不压缩（仅解密后重封装） |
| 90 | 6 | 几乎无损 |
| 70 | 19 | 轻微压缩 |
| 50 | 32 | 中度压缩 |
| 30 | 45 | 重度压缩 |
| 10 | 57 | 极限压缩 |
| 1 | 63 | 最大压缩（VP9 上限） |

建议先用 `preview` 模式在不同 quality 值下对比画质，找到自己能接受的平衡点。

## 处理规则

- 文件名为六位数字（如 `000001.dat`）且大小 ≥ 1MB → 压缩处理
- 文件名为六位数字但大小 < 1MB → 直接复制
- 其他文件名 → 直接复制

输出目录最终包含所有文件（处理过的 + 直接复制的），可直接替换原 MovieData 目录。

## 运行时显示

批量处理时实时显示多行进度：

```
  Progress: 42/1157 completed, 1115 remaining
  [000043.dat] ██████████████░░░░░░░░░░░ 56%
  [000044.dat] ████████░░░░░░░░░░░░░░░░░ 34%
  [000045.dat] decrypting...
```
