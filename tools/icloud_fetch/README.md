# iCloud 卸载文件实体化工具

## 用途

macOS 的 iCloud「优化存储」会把 `~/Documents` 下的文件卸载为占位符（`dataless`），
本地读不到内容，报错为 `errno 11 Resource deadlock avoided`（瞬间失败，不是网络慢）。

典型症状：
- `git log` / `git status` 报 `mmap failed: Resource deadlock avoided`
- Python 虚拟环境不可用，连 `pytest` 都无法导入
- 程序启动时报文件读不到

**`brctl download` 对此基本无效，重启 `bird`/`fileproviderd` 也无效。**
必须走 GUI / FileProvider 协调上下文才能触发下载。

## 用法

```bash
# 1. 扫描待处理文件
find /path/to/dir -flags +dataless -type f > /tmp/list.txt

# 2. 分批发出下载请求（每批 2000 个）
split -l 2000 /tmp/list.txt /tmp/batch_
for f in /tmp/batch_*; do ./ifetch < "$f"; done

# 3. iCloud 下载有排队延迟，需重复「扫描剩余 + 再请求」直到清零
for i in 1 2 3 4 5 6 7 8; do
  N=$(find /path/to/dir -flags +dataless -type f | wc -l)
  echo "第 $i 轮，剩余 $N"
  [ "$N" -eq 0 ] && break
  find /path/to/dir -flags +dataless -type f > /tmp/r.txt
  split -l 2000 /tmp/r.txt /tmp/rr_
  for f in /tmp/rr_*; do ./ifetch < "$f"; done
  sleep 60
done
```

实测约需 7~8 轮才能清零（每轮 iCloud 只完成一部分）。

## 重新编译

```bash
swiftc -O main.swift -o ifetch
```

## 注意

- 每个文件的内容约 1 GB 级别会占用本地磁盘，请确认空间充足。
- 处理后文件会重新占用本地空间（等于取消这部分卸载）。
- 若 iCloud 日后再次优化存储，同一问题可能复现，重跑本工具即可。
