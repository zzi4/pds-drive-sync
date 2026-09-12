# PDS 云盘盘点与下载脚本

这是一个独立运行的命令行工具。运行时只调用本机 Aliyun CLI 和阿里云 PDS，不调用 Codex、Agent 或大模型。

当前版本只执行云端读取操作，支持：

- 验证 PDS API Key
- 查看全部可访问的个人、团队和企业空间
- 保存全部文件元数据与容量统计快照
- 下载当前空间中的文件，并保留云端目录结构

## 首次配置

进入项目目录：

```bash
cd ~/Downloads/pds-drive-sync
```

执行初始化：

```bash
./pds-sync setup
```

脚本会检查项目私有的 Aliyun CLI 和 PDS 插件，然后以隐藏输入方式读取一次 API Key。Key 保存到 `~/.config/pds-sync/api_key`，文件权限固定为 `600`；以后再次运行 `setup` 会自动读取，不再提示。Key 不会保存到项目源码、Git、`config.toml`、快照或日志；鉴权配置同时由 Aliyun CLI 保存在当前 Linux 用户的配置目录中。

默认 PDS 域为 `bj39311`，企业云盘端点为
`https://bj39311.api.aliyunfile.com`。如需修改：

```bash
./pds-sync setup --domain-id <新的域ID> \
  --pds-endpoint https://<新的域ID>.api.aliyunfile.com
```

`setup` 的认证请求会自动绕过系统 HTTP/HTTPS 代理，避免部分 Go
客户端代理链路导致的认证失败；不会修改终端或系统的代理配置。

也可以复制 `config.example.toml` 为 `config.toml`，修改非敏感设置。

## 日常使用

查看当前用户与所有空间：

```bash
./pds-sync status
```

只获取文件清单和统计：

```bash
./pds-sync inventory
```

重新盘点并下载当前全部文件：

```bash
./pds-sync fetch
```

下载文件位于：

```text
downloads/<空间类型>-<drive_id>/<云端路径>
```

每次盘点的元数据位于：

```text
snapshots/<UTC时间>/
  user.json
  drives.json
  files.ndjson
  summary.json
  downloads.json    # fetch 时生成
```

如果目标位置已经有文件，仅在云端提供 SHA-1/SHA-256 且本地哈希完全一致时跳过；无法确认相同时保存为冲突副本，不会静默覆盖本地文件。

## 配置字段

`config.toml` 支持：

```toml
domain_id = "bj39311"
pds_endpoint = "https://bj39311.api.aliyunfile.com"
spaces = ["personal", "team", "enterprise"]
page_size = 100
snapshot_dir = "snapshots"
download_dir = "downloads"
timeout_seconds = 60
read_retries = 2
```

不要在该文件中添加 API Key；程序会主动拒绝 `api_key` 配置字段。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pds_sync tests
bash -n pds-sync
```
