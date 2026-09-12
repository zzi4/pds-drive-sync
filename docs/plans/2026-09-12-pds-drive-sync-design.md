# PDS 云盘只读采集脚本设计

## 目标

构建一个可随时从普通终端运行的独立脚本，不依赖 Codex、Agent 或大模型服务。第一阶段使用阿里云官方 Aliyun CLI 和 PDS 插件完成 API Key 鉴权，读取当前用户能够访问的全部个人、团队和企业空间，生成可供后续增量检测复用的本地快照，并下载当前发现的文件。

第一阶段只执行读取操作，不上传、修改、移动或删除云盘内容。

## 选定方案

采用“Python 命令行程序 + 官方 Aliyun CLI/PDS 插件”的方式。

- Python 负责配置、命令编排、分页、结果校验、统计和本地快照。
- Aliyun CLI/PDS 插件负责 API Key 鉴权及 PDS API 调用，避免自行实现和维护私有鉴权协议。
- 项目提供统一入口 `./pds-sync`，后续运行不需要 Agent。
- Aliyun CLI 优先使用系统已有版本；不存在时安装项目私有副本，避免要求 `sudo`。

未采用的方案：

- 纯 Python 直接请求 REST API：新型用户 API Key 的公开底层鉴权细节不足，兼容风险较高。
- 复用桌面客户端 Cookie 或登录缓存：凭据耦合、易失效，也不适合定时运行。

## 运行链路

```text
用户终端
  -> ./pds-sync setup       一次性配置
  -> ./pds-sync status      验证身份并查看空间
  -> ./pds-sync inventory   获取全部空间文件元数据
  -> ./pds-sync fetch       盘点并下载当前全部文件
       -> Aliyun CLI + PDS 插件
       -> 阿里云 PDS
       -> snapshots/ 快照与统计
       -> downloads/ 云盘文件副本
```

每次执行生成一个随机的 32 位十六进制会话 ID，并在所有 `aliyun pds` 调用中附加官方 Skill 要求的 User-Agent，便于服务端审计同一次运行中的请求。

## 命令设计

### `./pds-sync setup`

1. 检查 Python、CPU 架构和 Aliyun CLI 版本。
2. 若 CLI 不存在，安装项目私有版本，并启用插件自动安装。
3. 检查 PDS 插件版本，要求不低于官方 Skill 指定版本。
4. 默认使用从本机企业文件管理客户端识别到的域 `bj39311`，同时允许通过参数或配置修改。
5. 通过隐藏输入读取 API Key，调用官方 `aliyun pds config` 完成配置。
6. 调用 `get-user` 验证，只显示非敏感的 `domain_id`、`user_id` 和昵称。

API Key 不写入项目配置、源码、快照或日志。由于官方 CLI 的配置接口要求通过命令参数接收 Key，初始化期间它会短暂存在于子进程参数中；不会进入 shell 历史。后续运行复用官方 CLI 保存的本机凭据。

### `./pds-sync status`

1. 调用 `get-user` 检查认证状态。
2. 优先调用 `list-all-drives` 获取全部可访问空间。
3. 若该调用因权限返回 403，按官方 Skill 规则回退到 `list-my-drives` 与 `list-my-group-drive`。
4. 以表格显示空间类型、名称、已用容量和总容量。

### `./pds-sync inventory`

1. 复用一次空间查询得到的稳定 `drive_id` 集合。
2. 对全部空间递归获取文件和文件夹元数据，并处理分页。
3. 每个条目仅保留后续同步需要的字段：空间 ID、文件 ID、父目录 ID、名称、类型、大小、分类、扩展名、创建时间、更新时间和内容哈希。
4. 原子写入快照，任何空间失败时返回非零退出码，并在结果中标明失败空间；不把不完整结果伪装成成功。

### `./pds-sync fetch`

1. 先执行与 `inventory` 相同的完整盘点，得到确定的 `drive_id` 和 `file_id`。
2. 对本次盘点中的每个文件调用官方 `download-to-local`，由 PDS 插件获取签名 URL、下载并校验服务端文件大小。
3. 本地路径使用 `downloads/<space_type>-<drive_id>/<云端相对路径>`，避免不同空间中的同名文件互相覆盖。
4. 文件先下载为同目录临时文件；校验成功后再原子改名为正式文件。
5. 本地已有且元数据一致的文件直接跳过；不一致时保留原文件并将新内容保存为冲突副本，不静默覆盖用户本地数据。
6. 某个文件失败时继续处理其他文件，最终返回非零退出码，并在下载报告中列出失败项。

## 本地文件结构

```text
pds-drive-sync/
  pds-sync                 可执行入口
  src/                     Python 实现
  config.example.toml      可修改的非敏感设置示例
  config.toml              本机设置，不保存 API Key
  snapshots/               运行生成的数据，默认不纳入 Git
  downloads/               按空间和云端路径保存下载文件
  tests/                   不访问真实云盘的自动化测试
  docs/plans/              设计与实施计划
```

单次盘点预计生成：

- `snapshots/<timestamp>/user.json`
- `snapshots/<timestamp>/drives.json`
- `snapshots/<timestamp>/files.ndjson`
- `snapshots/<timestamp>/summary.json`
- `snapshots/<timestamp>/downloads.json`
- `snapshots/latest.json`，指向最近一次成功快照的相对路径与时间

## 配置

`config.toml` 只包含可公开设置：

- `domain_id`
- `spaces = ["personal", "team", "enterprise"]`
- `page_size`
- `snapshot_dir`
- `download_dir`
- `aliyun_cli_path`（可选）
- 请求超时与只读重试次数

API Key 不在该文件中。将来如需无人值守定时运行，继续复用 Aliyun CLI 的本机凭据配置。

## 错误处理与安全边界

- 所有输出和异常统一脱敏，不打印 API Key 或完整认证参数。
- 默认不启用 CLI debug 日志。
- 网络超时、限流只重试只读请求；认证失败和权限失败立即报告。
- `inventory` 只采集元数据；`fetch` 明确下载当前文件。新增数据检测和定时任务属于后续阶段。
- 项目第一阶段不暴露任何上传、移动、修改或删除命令。

## 验证标准

- 单元测试使用假的 Aliyun CLI，不需要真实 API Key 或网络。
- 验证 CLI 命令构造、凭据脱敏、403 回退、分页、汇总和原子写入。
- 完成后由用户在本机隐藏输入 API Key，依次运行 `setup`、`status` 和 `inventory` 做真实只读验收。
- 验收成功标准：能够识别当前用户，列出全部可访问空间，生成包含云盘文件元数据和汇总数字的完整快照，并把当前云端文件完整下载到 `downloads/`。

## 后续扩展

后续阶段将在不改变当前命令接口的前提下增加：

- 对比相邻快照，检测新增、修改、移动和删除记录。
- 按空间、目录、文件类型和大小规则自动下载新增文件。
- SQLite 状态库、断点续传、哈希校验和定时任务。
- 历史容量、文件类型、增长趋势统计。
